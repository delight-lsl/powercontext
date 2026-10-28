"""Extract specification, code and test records from a project tree.

The parsers are intentionally deterministic and dependency-free: every record
carries the verbatim source text plus the exact line range it came from, so a
retrieved context pack can always be traced back to the file it claims to come
from.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from .models import Record, sha256_bytes

__all__ = [
    "REGRESSION_PREFIX",
    "parse_code_file",
    "parse_spec_file",
    "parse_test_file",
    "scan_project",
]

REGRESSION_PREFIX = "test_regression_"

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _module_name(root: Path, path: Path) -> str:
    """Derive the dotted module name of a source file.

    A conventional ``src`` layout is stripped so that ``src/billing/pricing.py``
    becomes the module ``billing.pricing``.  Specifications address symbols by
    that dotted name.
    """
    parts = list(path.resolve().relative_to(root.resolve()).parts[:-1])
    if parts and parts[0] in {"src", "lib"}:
        parts = parts[1:]
    return ".".join(parts) if parts else path.stem


def _parse_front_matter(text: str) -> dict:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    loaded = yaml.safe_load(match.group(1))
    return loaded if isinstance(loaded, dict) else {}


def parse_spec_file(root: Path, path: Path) -> list[Record]:
    """Split one specification document into per-section records."""
    raw = path.read_text(encoding="utf-8")
    file_sha = sha256_bytes(path.read_bytes())
    relative = _relative(root, path)
    front_matter = _parse_front_matter(raw)
    spec_id = str(front_matter.get("spec_id") or path.stem)
    spec_title = str(front_matter.get("title") or spec_id)
    keywords = [str(item) for item in (front_matter.get("keywords") or [])]
    applies_to = [str(item) for item in (front_matter.get("applies_to") or [])]
    base_meta = {
        "spec_id": spec_id,
        "spec_title": spec_title,
        "spec_version": front_matter.get("version"),
        "spec_status": front_matter.get("status"),
        "keywords": keywords,
        "applies_to": applies_to,
    }

    lines = raw.splitlines()
    headings: list[tuple[int, int, str]] = []
    for number, line in enumerate(lines, start=1):
        match = _HEADING.match(line)
        if match:
            headings.append((number, len(match.group(1)), match.group(2)))
    if not headings:
        return []

    records: list[Record] = []
    for index, (start, level, title) in enumerate(headings):
        end = headings[index + 1][0] - 1 if index + 1 < len(headings) else len(lines)
        if end < start:
            end = start
        body = "\n".join(lines[start:end]).rstrip()
        if not body.strip():
            continue
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", title).strip("-").lower()
        meta = {
            **base_meta,
            "title": f"{spec_id} {title}",
            "section_title": title,
            "section_slug": slug,
            "heading_level": level,
        }
        records.append(
            Record(
                record_id=f"{spec_id}#{slug}",
                kind="spec_section",
                file=relative,
                lines=(start, end),
                sha256=file_sha,
                text=body,
                meta=meta,
            )
        )
    return records


def _ast_units(tree: ast.Module) -> list[tuple[str, ast.AST, int, int]]:
    """Return (name, node, start_line, end_line) for module and class level definitions."""
    units: list[tuple[str, ast.AST, int, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            units.append((node.name, node, node.lineno, node.end_lineno or node.lineno))
    return units


def parse_code_file(root: Path, path: Path) -> list[Record]:
    """Extract module level symbols from one source file."""
    raw = path.read_text(encoding="utf-8")
    file_sha = sha256_bytes(path.read_bytes())
    relative = _relative(root, path)
    tree = ast.parse(raw)
    lines = raw.splitlines()
    module_name = _module_name(root, path)

    records: list[Record] = []
    for name, node, start, end in _ast_units(tree):
        body = "\n".join(lines[start - 1 : end]).rstrip()
        if isinstance(node, ast.ClassDef):
            qualified = f"{module_name}.{name}"
            symbol_type = "class"
        else:
            qualified = f"{module_name}.{name}"
            symbol_type = "function"
        records.append(
            Record(
                record_id=qualified,
                kind="code_symbol",
                file=relative,
                lines=(start, end),
                sha256=file_sha,
                text=body,
                meta={
                    "symbol": name,
                    "qualified_name": qualified,
                    "symbol_type": symbol_type,
                    "module": module_name,
                    "file": relative,
                },
            )
        )
    return records


def parse_test_file(root: Path, path: Path) -> list[Record]:
    """Extract test cases from one test file and classify them by name prefix."""
    raw = path.read_text(encoding="utf-8")
    file_sha = sha256_bytes(path.read_bytes())
    relative = _relative(root, path)
    tree = ast.parse(raw)
    lines = raw.splitlines()

    records: list[Record] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        start = node.lineno
        end = node.end_lineno or node.lineno
        body = "\n".join(lines[start - 1 : end]).rstrip()
        category = "regression" if node.name.startswith(REGRESSION_PREFIX) else "functional"
        records.append(
            Record(
                record_id=f"{relative}::{node.name}",
                kind="test_case",
                file=relative,
                lines=(start, end),
                sha256=file_sha,
                text=body,
                meta={
                    "test_name": node.name,
                    "qualified_name": node.name,
                    "category": category,
                    "file": relative,
                },
            )
        )
    return records


def scan_project(root: Path) -> list[Record]:
    """Collect every specification, code and test record of a project tree."""
    root = root.resolve()
    records: list[Record] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = _relative(root, path)
        if relative.startswith("."):
            continue
        if "specs" in path.parts and path.suffix == ".md":
            records.extend(parse_spec_file(root, path))
        elif path.suffix == ".py":
            if "tests" in path.parts or path.name.startswith("test_"):
                records.extend(parse_test_file(root, path))
            else:
                records.extend(parse_code_file(root, path))
    return records
