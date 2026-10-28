"""Task driven retrieval of development context.

The retriever is deliberately explainable.  It combines

* a bilingual lexical model (Latin word tokens plus CJK character bigrams) with
  an inverse document frequency weight computed over the indexed corpus, and
* the explicit ``applies_to`` links declared by the specifications, which turn a
  specification section into an authoritative pointer at a code symbol or a test
  file.

Every returned item carries the provenance of the record it came from, so the
caller can always check the context against the repository.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import Record
from .store import CorpusEntry, ProgressEntry

__all__ = ["ContextPack", "ContextItem", "build_context_pack"]

_LATIN = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "must",
        "should",
        "when",
        "add",
        "use",
        "code",
        "test",
        "tests",
        "file",
        "function",
        "class",
        "new",
        "please",
        "implement",
        "support",
    }
)


def _features(text: str) -> set[str]:
    """Split ``text`` into comparable features (Latin words and CJK bigrams)."""
    lowered = text.lower()
    features: set[str] = set()
    for match in _LATIN.finditer(lowered):
        word = match.group(0)
        if len(word) >= 2 and word not in _STOPWORDS:
            features.add(word)
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            features.add(run)
            continue
        for index in range(len(run) - 1):
            features.add(run[index : index + 2])
    return features


@dataclass
class ContextItem:
    """One retrieved item together with the provenance of its source."""

    record_id: str
    kind: str
    file: str
    lines: tuple[int, int]
    sha256: str
    source_id: str
    db_position: int
    server_content_digest: str
    score: float
    matched_terms: list[str]
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "file": self.file,
            "lines": list(self.lines),
            "file_sha256": self.sha256,
            "source_id": self.source_id,
            "db_position": self.db_position,
            "server_content_digest": self.server_content_digest,
            "score": round(self.score, 6),
            "matched_terms": self.matched_terms,
            "meta": self.meta,
            "text": self.text,
        }


@dataclass
class ContextPack:
    """The complete development context assembled for one task."""

    task: str
    scope_id: str
    applicable_specs: list[ContextItem] = field(default_factory=list)
    code_to_modify: list[ContextItem] = field(default_factory=list)
    functional_tests: list[ContextItem] = field(default_factory=list)
    regression_tests: list[ContextItem] = field(default_factory=list)
    prior_progress: list[dict[str, Any]] = field(default_factory=list)
    corpus_size: int = 0

    @property
    def files_to_modify(self) -> list[str]:
        """Files that the change surface touches.

        A file qualifies when the primary specification declares it, or when a
        symbol in it was matched by the lexical model.
        """
        seen: list[str] = []
        for item in self.code_to_modify:
            if item.meta.get("selected_by") == "lexical" or item.meta.get("linked_by_primary_spec"):
                if item.file not in seen:
                    seen.append(item.file)
        return seen

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": "sddctx.context-pack.v1",
            "task": self.task,
            "scope_id": self.scope_id,
            "corpus_size": self.corpus_size,
            "files_to_modify": self.files_to_modify,
            "applicable_specs": [item.to_json() for item in self.applicable_specs],
            "code_to_modify": [item.to_json() for item in self.code_to_modify],
            "functional_tests": [item.to_json() for item in self.functional_tests],
            "regression_tests": [item.to_json() for item in self.regression_tests],
            "prior_progress": self.prior_progress,
        }


def _rank(
    entries: Iterable[CorpusEntry],
    query_features: set[str],
    idf: dict[str, float],
    total_idf: float,
) -> list[tuple[CorpusEntry, float, list[str]]]:
    scored: list[tuple[CorpusEntry, float, list[str]]] = []
    for entry in entries:
        features = _features(entry.record.search_text())
        overlap = query_features & features
        if not overlap or total_idf <= 0:
            scored.append((entry, 0.0, []))
            continue
        weight = sum(idf.get(term, 0.0) for term in overlap)
        matched = sorted(overlap, key=lambda term: (-idf.get(term, 0.0), term))
        scored.append((entry, weight / total_idf, matched))
    scored.sort(key=lambda item: (-item[1], item[0].record.record_id))
    return scored


def _applies_to_targets(record: Record, prefix: str) -> list[str]:
    targets: list[str] = []
    for item in record.meta.get("applies_to") or []:
        text = str(item)
        if text.startswith(prefix):
            targets.append(text[len(prefix) :].strip())
    return targets


def _specs_of(entry: CorpusEntry) -> list[str]:
    applies_to = entry.record.meta.get("applies_to") or []
    return [str(item) for item in applies_to]


def _latest_per_record(corpus: list[CorpusEntry]) -> list[CorpusEntry]:
    """Keep only the newest stored version of every record.

    Sources are content addressed, so an edited specification produces a new
    source version instead of overwriting the old one.  The newest version is
    the one with the highest journal position.
    """
    newest: dict[str, CorpusEntry] = {}
    for entry in corpus:
        current = newest.get(entry.record.record_id)
        if current is None or entry.position > current.position:
            newest[entry.record.record_id] = entry
    return list(newest.values())


def build_context_pack(
    task: str,
    corpus: list[CorpusEntry],
    *,
    scope_id: str,
    progress: list[ProgressEntry] | None = None,
    max_specs: int = 2,
    max_sections_per_spec: int = 12,
    max_symbols: int = 24,
    max_tests: int = 40,
) -> ContextPack:
    """Assemble the development context for ``task`` from the indexed corpus."""
    corpus = _latest_per_record(corpus)
    query_features = _features(task)
    document_frequency: dict[str, int] = {}
    for entry in corpus:
        for term in _features(entry.record.search_text()):
            document_frequency[term] = document_frequency.get(term, 0) + 1
    total = len(corpus) or 1
    idf = {term: math.log(1 + total / (1 + count)) for term, count in document_frequency.items()}
    total_idf = sum(idf.get(term, 0.0) for term in query_features)

    spec_entries = [entry for entry in corpus if entry.record.kind == "spec_section"]
    code_entries = [entry for entry in corpus if entry.record.kind == "code_symbol"]
    test_entries = [entry for entry in corpus if entry.record.kind == "test_case"]

    ranked_specs = _rank(spec_entries, query_features, idf, total_idf)
    ranked_code = _rank(code_entries, query_features, idf, total_idf)
    ranked_tests = _rank(test_entries, query_features, idf, total_idf)

    def to_item(entry: CorpusEntry, score: float, matched: list[str]) -> ContextItem:
        record = entry.record
        return ContextItem(
            record_id=record.record_id,
            kind=record.kind,
            file=record.file,
            lines=record.lines,
            sha256=record.sha256,
            source_id=entry.source_id,
            db_position=entry.position,
            server_content_digest=entry.content_digest,
            score=score,
            matched_terms=matched[:12],
            text=record.text,
            meta=record.meta,
        )

    # -- applicable specifications -----------------------------------------
    by_spec: dict[str, list[tuple[CorpusEntry, float, list[str]]]] = {}
    for item in ranked_specs:
        by_spec.setdefault(item[0].record.meta.get("spec_id", ""), []).append(item)

    spec_scores: list[tuple[str, float, list[tuple[CorpusEntry, float, list[str]]]]] = []
    for spec_id, sections in by_spec.items():
        best = max(sections, key=lambda item: item[1])
        spec_scores.append((spec_id, best[1], sections))
    spec_scores.sort(key=lambda item: (-item[1], item[0]))

    selected_specs = [entry for entry in spec_scores if entry[1] > 0][:max_specs]
    applicable_specs: list[ContextItem] = []
    spec_links: dict[str, dict[str, list[str]]] = {}
    for spec_id, _score, sections in selected_specs:
        links: dict[str, list[str]] = {"symbol": [], "file": [], "test": []}
        for section in sections[:max_sections_per_spec]:
            record = section[0].record
            item = to_item(*section)
            item.meta = {**item.meta, "linked_by": [spec_id]}
            applicable_specs.append(item)
            for target in _applies_to_targets(record, "symbol:"):
                links["symbol"].append(target)
            for target in _applies_to_targets(record, "file:"):
                links["file"].append(target)
            for target in _applies_to_targets(record, "test:"):
                links["test"].append(target)
        spec_links[spec_id] = links

    primary_spec_id = selected_specs[0][0] if selected_specs else ""
    primary_links = spec_links.get(primary_spec_id, {"symbol": [], "file": [], "test": []})
    other_links: dict[str, list[str]] = {"symbol": [], "file": [], "test": []}
    for spec_id, links in spec_links.items():
        if spec_id == primary_spec_id:
            continue
        for key in other_links:
            other_links[key].extend(links[key])

    # -- code to modify ----------------------------------------------------
    # The change surface is what the primary (new behaviour) specification
    # declares.  Symbols found by the lexical model are added on top of it.
    change_symbols = set(primary_links["symbol"])
    change_files = set(primary_links["file"])
    code_items: list[ContextItem] = []
    seen_records: set[str] = set()

    for entry, score, matched in ranked_code:
        record = entry.record
        linked_by = [
            spec_id
            for spec_id, links in spec_links.items()
            if record.record_id in links["symbol"]
            or record.meta.get("qualified_name") in links["symbol"]
            or record.file in links["file"]
        ]
        if not linked_by and score <= 0:
            continue
        if record.record_id in seen_records:
            continue
        seen_records.add(record.record_id)
        item = to_item(entry, score, matched)
        item.meta = {
            **item.meta,
            "linked_by": linked_by,
            "selected_by": "spec-link" if linked_by else "lexical",
            "linked_by_primary_spec": primary_spec_id in linked_by,
        }
        code_items.append(item)
        if len(code_items) >= max_symbols:
            break

    # -- tests -------------------------------------------------------------
    primary_test_files = set(primary_links["test"])
    all_test_files = set(primary_test_files) | set(other_links["test"])
    functional: list[ContextItem] = []
    regression: list[ContextItem] = []
    seen_tests: set[str] = set()
    for entry, score, matched in ranked_tests:
        record = entry.record
        category = record.meta.get("category")
        in_primary = any(record.file.endswith(target) for target in primary_test_files)
        in_any = any(record.file.endswith(target) for target in all_test_files)
        if not in_any and score <= 0:
            continue
        if record.record_id in seen_tests:
            continue
        seen_tests.add(record.record_id)
        item = to_item(entry, score, matched)
        selection = "spec-link" if in_any else "lexical"
        item.meta = {
            **item.meta,
            "selected_by": selection,
            "linked_by_primary_spec": in_primary,
        }
        if category == "regression":
            regression.append(item)
        else:
            functional.append(item)
        if len(functional) + len(regression) >= max_tests * 4:
            break

    pack = ContextPack(
        task=task,
        scope_id=scope_id,
        applicable_specs=applicable_specs,
        code_to_modify=code_items[:max_symbols],
        functional_tests=functional[:max_tests],
        regression_tests=regression[:max_tests],
        corpus_size=len(corpus),
    )
    pack.prior_progress = [
        {
            "artifact_id": entry.artifact_id,
            "revision": entry.revision,
            "kind": entry.kind,
            "text": entry.text,
            "state": entry.state,
        }
        for entry in (progress or [])
    ]
    return pack
