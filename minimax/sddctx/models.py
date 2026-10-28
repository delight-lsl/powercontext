"""Core data model for the SDD context manager.

Every unit of project knowledge (a specification section, a code symbol, a test
case) is represented by a :class:`Record`.  A record is the unit that gets
persisted into PowerContext and later retrieved to assemble a context pack.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

RECORD_SCHEMA = "sddctx.record.v1"

RecordKind = Literal["spec_section", "code_symbol", "test_case"]

__all__ = [
    "RECORD_SCHEMA",
    "Record",
    "RecordKind",
    "sha256_bytes",
    "sha256_text",
]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(payload: str) -> str:
    return sha256_bytes(payload.encode("utf-8"))


@dataclass(frozen=True)
class Record:
    """One retrievable unit of project knowledge.

    Attributes:
        record_id: Stable identity of the unit inside the project, for example
            ``SPEC-BILL-001#discount-rules`` or ``billing.pricing.compute_invoice_totals``.
        kind: Which kind of knowledge this record carries.
        file: Repository-relative path of the file the unit was extracted from.
        lines: Inclusive 1-based line range inside ``file``.
        sha256: SHA-256 of the whole ``file`` at ingest time.
        text: The verbatim source text of the unit.
        meta: Kind specific metadata, for example ``applies_to`` or ``category``.
    """

    record_id: str
    kind: RecordKind
    file: str
    lines: tuple[int, int]
    sha256: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def envelope(self) -> str:
        """Serialize the record into the canonical envelope stored in the database."""
        payload = {
            "schema": RECORD_SCHEMA,
            "record": {
                "record_id": self.record_id,
                "kind": self.kind,
                "file": self.file,
                "lines": list(self.lines),
                "sha256": self.sha256,
                "meta": self.meta,
                "text": self.text,
            },
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_envelope(cls, envelope: str) -> "Record":
        payload = json.loads(envelope)
        if payload.get("schema") != RECORD_SCHEMA:
            raise ValueError(f"unsupported record schema: {payload.get('schema')!r}")
        body = payload["record"]
        return cls(
            record_id=body["record_id"],
            kind=body["kind"],
            file=body["file"],
            lines=(int(body["lines"][0]), int(body["lines"][1])),
            sha256=body["sha256"],
            text=body["text"],
            meta=body.get("meta") or {},
        )

    @property
    def source_id(self) -> str:
        """Content-addressed PowerContext source identity.

        The digest covers the unit text and the digest of the file it was taken
        from, so re-importing unchanged content is idempotent while an edited
        specification produces a new source version instead of a conflict.
        """
        payload = f"{self.text}\n{self.sha256}\n{self.lines[0]}:{self.lines[1]}"
        digest = sha256_text(payload)[:12]
        return f"sddctx:v1:{self.kind}:{self.record_id}:{digest}"

    def search_text(self) -> str:
        """Text used for lexical matching against a development task."""
        parts = [self.record_id, self.file, self.text]
        for key in ("title", "spec_title", "section_title", "symbol", "qualified_name", "test_name"):
            value = self.meta.get(key)
            if isinstance(value, str):
                parts.append(value)
        keywords = self.meta.get("keywords")
        if isinstance(keywords, list):
            parts.extend(str(item) for item in keywords)
        applies_to = self.meta.get("applies_to")
        if isinstance(applies_to, list):
            parts.extend(str(item) for item in applies_to)
        return "\n".join(parts)
