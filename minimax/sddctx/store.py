"""Persistence of the SDD corpus and of task progress through PowerContext.

Everything the context manager needs is stored in PowerContext itself:

* project knowledge (specifications, code symbols, test cases) is captured as
  durable Content Sources, so it lands in the ``pc_sources`` table of the
  configured OceanBase / seekdb database;
* task progress and verification results are stored as Atomic Memory entries,
  which makes them durable across a server restart and searchable afterwards.

The module only depends on the public PowerContext Python client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from powercontext.client import PowerContextClient
from powercontext.http._generated.models import (
    CaptureContentSourceRequest,
    CreateScopeRequest,
    ListMemoryEntriesRequest,
    ListSourcesRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
)

from .models import Record

__all__ = ["CorpusEntry", "ProgressEntry", "PowerContextStore"]


@dataclass(frozen=True)
class CorpusEntry:
    """One record as it exists in the database."""

    record: Record
    source_id: str
    position: int
    content_digest: str


@dataclass(frozen=True)
class ProgressEntry:
    """One Atomic Memory entry holding task progress or a verification result."""

    artifact_id: str
    revision: int
    kind: str
    text: str
    state: str


class PowerContextStore:
    """Thin, explicit facade over the PowerContext HTTP API."""

    def __init__(self, client: PowerContextClient, scope_id: str) -> None:
        self._client = client
        self._scope_id = scope_id

    @property
    def scope_id(self) -> str:
        return self._scope_id

    # -- scopes ------------------------------------------------------------

    async def ensure_scope(self, title: str, summary: str) -> str:
        """Return the scope with ``title``, creating it when it does not exist."""
        page = await self._client.list_scopes()
        for scope in page.items:
            if scope.title == title:
                return scope.scope_id
        created = await self._client.create_scope(
            CreateScopeRequest(title=title, summary=summary, idempotency_key=f"sddctx:{title}")
        )
        return created.scope_id

    # -- corpus ------------------------------------------------------------

    async def put_records(self, records: list[Record]) -> list[CorpusEntry]:
        """Capture every record as a durable Content Source.

        Re-importing unchanged content is idempotent because the source identity
        contains the digest of the unit text.  Edited content produces a new
        source version instead of a conflict.
        """
        entries: list[CorpusEntry] = []
        for record in records:
            response = await self._client.capture_content_source(
                CaptureContentSourceRequest(
                    scope_id=self._scope_id,
                    source_id=record.source_id,
                    content=record.envelope(),
                    metadata={
                        "kind": record.kind,
                        "record_id": record.record_id,
                        "file": record.file,
                        "sha256": record.sha256,
                    },
                )
            )
            source = await self._client.get_source(self._scope_id, "content", record.source_id)
            entries.append(
                CorpusEntry(
                    record=record,
                    source_id=record.source_id,
                    position=response.position,
                    content_digest=source.content_digest,
                )
            )
        return entries

    async def list_corpus(self) -> list[CorpusEntry]:
        """Read every captured corpus source back from the database."""
        entries: list[CorpusEntry] = []
        cursor: str | None = None
        while True:
            request = ListSourcesRequest(limit=100, cursor=cursor)
            page = await self._client.list_sources(self._scope_id, request)
            for item in page.items:
                if not item.source_id.startswith("sddctx:v1:"):
                    continue
                try:
                    record = Record.from_envelope(item.content)
                except (ValueError, KeyError, TypeError):
                    continue
                entries.append(
                    CorpusEntry(
                        record=record,
                        source_id=item.source_id,
                        position=item.position,
                        content_digest=item.content_digest,
                    )
                )
            cursor = page.next_cursor
            if not cursor:
                break
        return entries

    # -- progress ----------------------------------------------------------

    async def remember_progress(self, kind: str, text: str, reason: str | None = None) -> ProgressEntry:
        """Store one durable progress or verification record as Atomic Memory."""
        response = await self._client.remember_memory(
            RememberMemoryRequest(scope_id=self._scope_id, kind=kind, text=text, reason=reason)
        )
        record = response.records[0]
        return ProgressEntry(
            artifact_id=record.artifact.artifact_id,
            revision=record.artifact.revision,
            kind=record.kind,
            text=record.text,
            state=record.state,
        )

    async def list_progress(self, include_inactive: bool = False) -> list[ProgressEntry]:
        """Read every stored progress record back from the database."""
        entries: list[ProgressEntry] = []
        cursor: str | None = None
        while True:
            request = ListMemoryEntriesRequest(
                scope_id=self._scope_id,
                include_inactive=include_inactive,
                limit=100,
                cursor=cursor,
            )
            response = await self._client.list_memory_entries(request)
            for record in response.entries:
                entries.append(
                    ProgressEntry(
                        artifact_id=record.artifact.artifact_id,
                        revision=record.artifact.revision,
                        kind=record.kind,
                        text=record.text,
                        state=record.state,
                    )
                )
            cursor = response.next_cursor
            if not cursor:
                break
        return entries

    async def search_progress(self, query: str, limit: int = 10) -> list[ProgressEntry]:
        """Search stored progress records by text."""
        response = await self._client.search_memory(
            SearchMemoryRequest(scope_id=self._scope_id, query=query, limit=limit)
        )
        return [
            ProgressEntry(
                artifact_id=hit.memory.artifact.artifact_id,
                revision=hit.memory.artifact.revision,
                kind=hit.memory.kind,
                text=hit.memory.text,
                state=hit.memory.state,
            )
            for hit in response.hits
        ]


def build_client(base_url: str, *, token: str | None = None, timeout: float = 30.0) -> PowerContextClient:
    """Create a PowerContext HTTP client for ``base_url``."""
    return PowerContextClient(base_url=base_url, token=token, timeout=timeout)


def store_metadata(record: Record) -> dict[str, Any]:
    """Caller metadata attached to a captured source."""
    return {
        "kind": record.kind,
        "record_id": record.record_id,
        "file": record.file,
        "sha256": record.sha256,
    }
