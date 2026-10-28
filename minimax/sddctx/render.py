"""Render a context pack into a model prompt and into an auditable report.

The prompt is what the coding model receives.  The report is what a reviewer
reads: every section keeps the provenance of the record it was built from.
"""

from __future__ import annotations

from .retrieval import ContextItem, ContextPack

__all__ = ["SYSTEM_PROMPT", "render_prompt", "render_report"]

SYSTEM_PROMPT = """\
You are a careful software engineer working inside an existing repository.

You receive a development context that was retrieved from a specification-first
development (SDD) context store.  The context is authoritative:

- The applicable specification sections define the required NEW behaviour.
- The listed code symbols are the ones that must change.  Do not modify code
  that is not listed.
- The functional tests are the acceptance tests of the new behaviour.
- The regression tests encode behaviour that already exists and MUST NOT change.

Rules:
1. Implement only what the specification requires, inside the listed symbols.
2. Preserve every behaviour covered by the regression tests.
3. Follow the existing code style of the repository.
4. Reply with the complete updated content of every file you changed, each
   preceded by a line of the form:
   === FILE: <path relative to the repository root> ===
   Do not add commentary before or after the file blocks.
"""


def _provenance(item: ContextItem) -> str:
    return (
        f"[source_id={item.source_id} db_position={item.db_position} "
        f"sha256={item.sha256[:12]} server_digest={item.server_content_digest[:19]}…]"
    )


def render_prompt(pack: ContextPack, task: str) -> str:
    """Build the prompt sent to the coding model."""
    blocks: list[str] = [f"# Task\n\n{task}\n"]

    if pack.applicable_specs:
        blocks.append("\n# Applicable specification\n")
        for item in pack.applicable_specs:
            blocks.append(
                f"## {item.meta.get('title', item.record_id)} "
                f"({item.file}:{item.lines[0]}-{item.lines[1]}) {_provenance(item)}\n\n{item.text}\n"
            )

    if pack.code_to_modify:
        blocks.append("\n# Code to modify\n")
        for item in pack.code_to_modify:
            blocks.append(
                f"## {item.record_id} ({item.file}:{item.lines[0]}-{item.lines[1]}) {_provenance(item)}\n\n"
                f"```python\n{item.text}\n```\n"
            )

    if pack.functional_tests:
        blocks.append("\n# Functional tests (must pass after the change)\n")
        for item in pack.functional_tests:
            blocks.append(
                f"## {item.meta.get('test_name')} ({item.file}:{item.lines[0]}-{item.lines[1]}) "
                f"{_provenance(item)}\n\n```python\n{item.text}\n```\n"
            )

    if pack.regression_tests:
        blocks.append("\n# Regression tests (must keep passing)\n")
        for item in pack.regression_tests:
            blocks.append(
                f"## {item.meta.get('test_name')} ({item.file}:{item.lines[0]}-{item.lines[1]}) "
                f"{_provenance(item)}\n\n```python\n{item.text}\n```\n"
            )

    if pack.prior_progress:
        blocks.append("\n# Progress recorded in the context store\n")
        for entry in pack.prior_progress:
            blocks.append(f"- [{entry['kind']}] {entry['text']}\n")

    return "\n".join(blocks)


def render_report(pack: ContextPack) -> str:
    """Render the human readable provenance report of a context pack."""
    lines: list[str] = []
    lines.append(f"Task: {pack.task}")
    lines.append(f"Scope: {pack.scope_id}")
    lines.append(f"Corpus records considered: {pack.corpus_size}")
    lines.append(f"Files to modify: {', '.join(pack.files_to_modify) or '(none)'}")

    def section(title: str, items: list[ContextItem]) -> None:
        lines.append("")
        lines.append(f"## {title} ({len(items)})")
        for item in items:
            meta = item.meta
            label = (
                meta.get("title")
                or meta.get("qualified_name")
                or meta.get("test_name")
                or item.record_id
            )
            lines.append(
                f"- {label} | {item.file}:{item.lines[0]}-{item.lines[1]} | "
                f"score={item.score:.4f} | {item.source_id} | db_position={item.db_position} | "
                f"file_sha256={item.sha256} | server_digest={item.server_content_digest}"
            )

    section("Applicable specifications", pack.applicable_specs)
    section("Code to modify", pack.code_to_modify)
    section("Functional tests", pack.functional_tests)
    section("Regression tests", pack.regression_tests)

    if pack.prior_progress:
        lines.append("")
        lines.append(f"## Progress reused from the context store ({len(pack.prior_progress)})")
        for entry in pack.prior_progress:
            lines.append(
                f"- [{entry['kind']}] {entry['text']} (artifact={entry['artifact_id']}, state={entry['state']})"
            )
    return "\n".join(lines)
