# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Source lifecycle management, DAG dependency tracking, and cascading deactivation."""

# ruff: noqa: TRY003, TRY004, TRY301

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation
from powercontext.builtin.artifacts.topic_memory import (
    TopicMemoryCurrentItem,
    TopicMemorySearchResult,
)
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.builtin.statistics import ModelUsagePurpose
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """Raised when a lifecycle operation fails or violates DAG/sequence constraints."""


class SourceLifecycleEvent(BaseModel):
    """Event representing availability changes for a registered Source."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    revision: int
    sequence: int
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def _validate_str(cls, value: str, info: ValidationInfo) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value

    @field_validator("revision", "sequence")
    @classmethod
    def _validate_int(cls, value: int, info: ValidationInfo) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{info.field_name} must be a strictly positive integer")
        return value

    @field_validator("available")
    @classmethod
    def _validate_bool(cls, value: bool) -> bool:
        if not isinstance(value, bool):
            raise ValueError("available must be a boolean")
        return value


async def ensure_source_lifecycle_schema(connection: AsyncConnection, /) -> None:
    """Create lifecycle tables if they do not exist."""
    await connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS pc_lifecycle_nodes (
                scope_id VARCHAR(64) NOT NULL,
                node_id VARCHAR(128) NOT NULL,
                kind VARCHAR(16) NOT NULL,
                ref_data TEXT NOT NULL,
                revision INTEGER NOT NULL,
                policy VARCHAR(8),
                available INTEGER NOT NULL,
                last_sequence INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope_id, node_id)
            )
            """
        )
    )
    await connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS pc_lifecycle_edges (
                scope_id VARCHAR(64) NOT NULL,
                parent_id VARCHAR(128) NOT NULL,
                child_id VARCHAR(128) NOT NULL,
                PRIMARY KEY (scope_id, parent_id, child_id)
            )
            """
        )
    )
    await connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS pc_lifecycle_events (
                scope_id VARCHAR(64) NOT NULL,
                node_id VARCHAR(128) NOT NULL,
                sequence INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                available INTEGER NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (scope_id, node_id, sequence)
            )
            """
        )
    )


def _validate_derived_args(node_id: str, parents: Sequence[str], policy: str) -> None:
    if not isinstance(node_id, str) or not node_id.strip():
        raise SourceLifecycleError("node_id must be a non-empty string")
    if policy not in ("all", "any"):
        raise SourceLifecycleError("policy must be 'all' or 'any'")
    if not parents:
        raise SourceLifecycleError("parents cannot be empty")
    if len(parents) != len(set(parents)):
        raise SourceLifecycleError("parents cannot contain duplicates")
    if node_id in parents:
        raise SourceLifecycleError("Self-cycle detected")


def _compute_dag_availability(
    nodes: Sequence[Any],
    edges: Sequence[Any],
    event: SourceLifecycleEvent,
) -> dict[str, bool]:
    parents_map: dict[str, list[str]] = {r[0]: [] for r in nodes}
    for p, c in edges:
        if c in parents_map:
            parents_map[c].append(p)

    nodes_info = {r[0]: {"kind": r[1], "policy": r[3], "available": bool(r[4])} for r in nodes}
    nodes_info[event.node_id]["available"] = event.available

    new_avail: dict[str, bool] = {}

    def get_avail(nid: str) -> bool:
        if nid in new_avail:
            return new_avail[nid]
        info = nodes_info[nid]
        if info["kind"] == "source":
            res = info["available"]
        else:
            p_avails = [get_avail(p) for p in parents_map[nid]]
            pol = info["policy"]
            res = all(p_avails) if pol == "all" else any(p_avails)
        new_avail[nid] = res
        return res

    for nid in nodes_info:
        get_avail(nid)

    return new_avail


class ScopedLifecycleApplication:
    """Scoped service managing source lifecycle DAG and deactivation for one scope."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        revision: int = 1,
    ) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("node_id must be a non-empty string")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise SourceLifecycleError("revision must be a positive integer")
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source_ref must be a SourceRef instance")

        try:
            source_item = await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type, source_ref.source_id
            )
            if source_item is None:
                raise SourceLifecycleError(f"source_ref {source_ref} not found")
        except Exception as error:
            raise SourceLifecycleError(f"Cannot read source_ref {source_ref}: {error}") from error

        async with self._runtime.database.transaction() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT kind, ref_data, revision FROM pc_lifecycle_nodes "
                        "WHERE scope_id = :scope_id AND node_id = :node_id"
                    ),
                    {"scope_id": self.scope_id, "node_id": node_id},
                )
            ).fetchone()
            if row is not None:
                kind, ref_data, rev = row
                if kind == "source" and rev == revision and json.loads(ref_data) == source_ref.model_dump(mode="json"):
                    return
                raise SourceLifecycleError(f"Node '{node_id}' already registered with different binding")

            await conn.execute(
                text(
                    """
                    INSERT INTO pc_lifecycle_nodes
                        (scope_id, node_id, kind, ref_data, revision, policy, available, last_sequence)
                    VALUES
                        (:scope_id, :node_id, 'source', :ref_data, :revision, NULL, 1, 0)
                    """
                ),
                {
                    "scope_id": self.scope_id,
                    "node_id": node_id,
                    "ref_data": source_ref.model_dump_json(),
                    "revision": revision,
                },
            )

    async def _check_and_insert_derived(
        self,
        conn: AsyncConnection,
        *,
        node_id: str,
        kind: str,
        parents: Sequence[str],
        policy: Literal["all", "any"],
        ref_dump: dict[str, Any],
        ref_json: str,
        revision: int,
    ) -> bool:
        all_nodes = (
            await conn.execute(
                text(
                    "SELECT node_id, available, kind, ref_data, policy FROM pc_lifecycle_nodes "
                    "WHERE scope_id = :scope_id"
                ),
                {"scope_id": self.scope_id},
            )
        ).fetchall()
        node_map = {r[0]: {"available": bool(r[1]), "kind": r[2], "ref_data": r[3], "policy": r[4]} for r in all_nodes}
        for p in parents:
            if p not in node_map:
                raise SourceLifecycleError(f"Parent '{p}' is not registered")

        if await self._would_create_cycle(conn, node_id, parents):
            raise SourceLifecycleError("Cycle detected in DAG")

        if node_id in node_map:
            existing = node_map[node_id]
            existing_parents = tuple(
                r[0]
                for r in (
                    await conn.execute(
                        text(
                            "SELECT parent_id FROM pc_lifecycle_edges "
                            "WHERE scope_id = :scope_id AND child_id = :node_id ORDER BY parent_id"
                        ),
                        {"scope_id": self.scope_id, "node_id": node_id},
                    )
                ).fetchall()
            )
            if (
                existing["kind"] == kind
                and existing["policy"] == policy
                and json.loads(existing["ref_data"]) == ref_dump
                and existing_parents == tuple(sorted(parents))
            ):
                return existing["available"]
            raise SourceLifecycleError(f"Node '{node_id}' already registered with different binding")

        parent_avails = [node_map[p]["available"] for p in parents]
        is_available = all(parent_avails) if policy == "all" else any(parent_avails)

        await conn.execute(
            text(
                """
                INSERT INTO pc_lifecycle_nodes
                    (scope_id, node_id, kind, ref_data, revision, policy, available, last_sequence)
                VALUES
                    (:scope_id, :node_id, :kind, :ref_data, :revision, :policy, :available, 0)
                """
            ),
            {
                "scope_id": self.scope_id,
                "node_id": node_id,
                "kind": kind,
                "ref_data": ref_json,
                "revision": revision,
                "policy": policy,
                "available": 1 if is_available else 0,
            },
        )
        for p in parents:
            await conn.execute(
                text(
                    "INSERT INTO pc_lifecycle_edges (scope_id, parent_id, child_id) "
                    "VALUES (:scope_id, :parent_id, :child_id)"
                ),
                {"scope_id": self.scope_id, "parent_id": p, "child_id": node_id},
            )
        return is_available

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        parents: Sequence[str],
        policy: Literal["all", "any"] = "all",
    ) -> None:
        _validate_derived_args(node_id, parents, policy)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("citation must be a MemoryCitation instance")

        try:
            mem_entry = await self._runtime.memory.for_scope(self.scope_id).get(
                GetMemoryEntryRequest(citation=citation)
            )
            if mem_entry is None:
                raise SourceLifecycleError(f"citation {citation} not found")
        except Exception as error:
            raise SourceLifecycleError(f"Cannot read citation {citation}: {error}") from error

        async with self._runtime.database.transaction() as conn:
            is_available = await self._check_and_insert_derived(
                conn,
                node_id=node_id,
                kind="memory",
                parents=parents,
                policy=policy,
                ref_dump=citation.model_dump(mode="json"),
                ref_json=citation.model_dump_json(),
                revision=citation.memory_ref.revision,
            )

        if not is_available:
            await self._set_memories_state([(citation, "inactive")], reason="Initial lifecycle state")

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        parents: Sequence[str],
        policy: Literal["all", "any"] = "all",
    ) -> None:
        _validate_derived_args(node_id, parents, policy)
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("artifact_ref must be an ArtifactRef instance")

        try:
            record = await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision
            )
            if record is None:
                raise SourceLifecycleError(f"artifact_ref {artifact_ref} not found")
        except Exception as error:
            raise SourceLifecycleError(f"Cannot read artifact_ref {artifact_ref}: {error}") from error

        async with self._runtime.database.transaction() as conn:
            await self._check_and_insert_derived(
                conn,
                node_id=node_id,
                kind="topic",
                parents=parents,
                policy=policy,
                ref_dump=artifact_ref.model_dump(mode="json"),
                ref_json=artifact_ref.model_dump_json(),
                revision=artifact_ref.revision,
            )

    async def _load_event_context(
        self,
        event: SourceLifecycleEvent,
    ) -> tuple[Sequence[Any], Sequence[Any], list[str] | None]:
        async with self._runtime.database.transaction() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT kind, revision, last_sequence FROM pc_lifecycle_nodes "
                        "WHERE scope_id = :scope_id AND node_id = :node_id"
                    ),
                    {"scope_id": self.scope_id, "node_id": event.node_id},
                )
            ).fetchone()
            if row is None:
                raise SourceLifecycleError(f"Node '{event.node_id}' not registered")
            kind, rev, last_seq = row
            if kind != "source":
                raise SourceLifecycleError(f"Node '{event.node_id}' is not a source node")
            if rev != event.revision:
                raise SourceLifecycleError(
                    f"Revision mismatch for node '{event.node_id}': expected {rev}, got {event.revision}"
                )

            if event.sequence < last_seq:
                raise SourceLifecycleError(f"Sequence {event.sequence} is lower than last sequence {last_seq}")
            if event.sequence == last_seq:
                ev_row = (
                    await conn.execute(
                        text(
                            "SELECT sequence, revision, available, reason FROM pc_lifecycle_events "
                            "WHERE scope_id = :scope_id AND node_id = :node_id AND sequence = :sequence"
                        ),
                        {"scope_id": self.scope_id, "node_id": event.node_id, "sequence": event.sequence},
                    )
                ).fetchone()
                if ev_row is not None:
                    _, e_rev, e_avail, e_reason = ev_row
                    if e_rev == event.revision and bool(e_avail) == event.available and e_reason == event.reason:
                        active_nodes = (
                            await conn.execute(
                                text(
                                    "SELECT node_id FROM pc_lifecycle_nodes "
                                    "WHERE scope_id = :scope_id AND available = 1 ORDER BY node_id"
                                ),
                                {"scope_id": self.scope_id},
                            )
                        ).fetchall()
                        return (), (), [r[0] for r in active_nodes]
                raise SourceLifecycleError(f"Conflicting event replay for sequence {event.sequence}")

            nodes = (
                await conn.execute(
                    text(
                        "SELECT node_id, kind, ref_data, policy, available FROM pc_lifecycle_nodes "
                        "WHERE scope_id = :scope_id"
                    ),
                    {"scope_id": self.scope_id},
                )
            ).fetchall()
            edges = (
                await conn.execute(
                    text("SELECT parent_id, child_id FROM pc_lifecycle_edges WHERE scope_id = :scope_id"),
                    {"scope_id": self.scope_id},
                )
            ).fetchall()
            return nodes, edges, None

    async def _persist_event_and_availabilities(
        self,
        event: SourceLifecycleEvent,
        new_avail: dict[str, bool],
    ) -> None:
        async with self._runtime.database.transaction() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO pc_lifecycle_events
                        (scope_id, node_id, sequence, revision, available, reason)
                    VALUES
                        (:scope_id, :node_id, :sequence, :revision, :available, :reason)
                    """
                ),
                {
                    "scope_id": self.scope_id,
                    "node_id": event.node_id,
                    "sequence": event.sequence,
                    "revision": event.revision,
                    "available": 1 if event.available else 0,
                    "reason": event.reason,
                },
            )
            await conn.execute(
                text(
                    "UPDATE pc_lifecycle_nodes SET last_sequence = :seq "
                    "WHERE scope_id = :scope_id AND node_id = :node_id"
                ),
                {"seq": event.sequence, "scope_id": self.scope_id, "node_id": event.node_id},
            )
            for nid, av in new_avail.items():
                await conn.execute(
                    text(
                        "UPDATE pc_lifecycle_nodes SET available = :av "
                        "WHERE scope_id = :scope_id AND node_id = :node_id"
                    ),
                    {"av": 1 if av else 0, "scope_id": self.scope_id, "node_id": nid},
                )

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent instance")

        nodes, edges, replay_active = await self._load_event_context(event)
        if replay_active is not None:
            return replay_active

        new_avail = _compute_dag_availability(nodes, edges, event)

        memory_changes: list[tuple[MemoryCitation, Literal["active", "inactive"]]] = []
        for r in nodes:
            nid, kind, ref_data = r[0], r[1], r[2]
            if kind == "memory":
                target = "active" if new_avail[nid] else "inactive"
                citation = MemoryCitation.model_validate_json(ref_data)
                memory_changes.append((citation, target))

        await self._set_memories_state(memory_changes, reason=event.reason)
        await self._persist_event_and_availabilities(event, new_avail)

        return sorted([nid for nid, av in new_avail.items() if av])

    async def resolve(
        self,
        node_id: str,
        purpose: str = "generation",
    ) -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError(f"Invalid purpose '{purpose}'")

        async with self._runtime.database.transaction() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT kind, ref_data, available FROM pc_lifecycle_nodes "
                        "WHERE scope_id = :scope_id AND node_id = :node_id"
                    ),
                    {"scope_id": self.scope_id, "node_id": node_id},
                )
            ).fetchone()
            if row is None:
                raise SourceLifecycleError(f"Node '{node_id}' not registered")

            kind, ref_data, available = row
            is_avail = bool(available)

            if purpose == "generation" and not is_avail:
                raise SourceLifecycleError(f"Node '{node_id}' is inactive")

            if kind == "source":
                return SourceRef.model_validate_json(ref_data)
            elif kind == "memory":
                return MemoryCitation.model_validate_json(ref_data)
            elif kind == "topic":
                return ArtifactRef.model_validate_json(ref_data)
            else:
                raise SourceLifecycleError(f"Unknown kind '{kind}'")

    async def filter_topic_search_result(
        self,
        result: TopicMemorySearchResult,
    ) -> TopicMemorySearchResult:
        if not result.hits:
            return result
        async with self._runtime.database.transaction() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT ref_data, available FROM pc_lifecycle_nodes "
                        "WHERE scope_id = :scope_id AND kind = 'topic'"
                    ),
                    {"scope_id": self.scope_id},
                )
            ).fetchall()
        if not rows:
            return result

        registered_topics: dict[str, bool] = {}
        for ref_data, available in rows:
            data = json.loads(ref_data)
            artifact_id = data.get("artifact_id")
            if artifact_id:
                registered_topics[artifact_id] = bool(available)

        filtered_hits = tuple(
            hit
            for hit in result.hits
            if hit.artifact_ref.artifact_id not in registered_topics or registered_topics[hit.artifact_ref.artifact_id]
        )
        return result.model_copy(update={"hits": filtered_hits})

    async def filter_topic_browse_items(
        self,
        items: tuple[TopicMemoryCurrentItem, ...],
    ) -> tuple[TopicMemoryCurrentItem, ...]:
        if not items:
            return items
        async with self._runtime.database.transaction() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT ref_data, available FROM pc_lifecycle_nodes "
                        "WHERE scope_id = :scope_id AND kind = 'topic'"
                    ),
                    {"scope_id": self.scope_id},
                )
            ).fetchall()
        if not rows:
            return items

        registered_topics: dict[str, bool] = {}
        for ref_data, available in rows:
            data = json.loads(ref_data)
            artifact_id = data.get("artifact_id")
            if artifact_id:
                registered_topics[artifact_id] = bool(available)

        return tuple(
            item
            for item in items
            if item.artifact_ref.artifact_id not in registered_topics
            or registered_topics[item.artifact_ref.artifact_id]
        )

    async def _would_create_cycle(
        self,
        conn: AsyncConnection,
        node_id: str,
        parents: Sequence[str],
    ) -> bool:
        if node_id in parents:
            return True
        edges = (
            await conn.execute(
                text("SELECT parent_id, child_id FROM pc_lifecycle_edges WHERE scope_id = :scope_id"),
                {"scope_id": self.scope_id},
            )
        ).fetchall()
        adj: dict[str, list[str]] = {}
        for p, c in edges:
            adj.setdefault(p, []).append(c)

        visited = set()
        queue = [node_id]
        while queue:
            curr = queue.pop()
            if curr in visited:
                continue
            visited.add(curr)
            for neighbor in adj.get(curr, []):
                if neighbor in parents:
                    return True
                queue.append(neighbor)
        return False

    async def _set_memories_state(
        self,
        items: Sequence[tuple[MemoryCitation, Literal["active", "inactive"]]],
        reason: str,
    ) -> None:
        if not items:
            return
        async with (
            self._runtime._context(
                self.scope_id,
                embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING,
            ) as context,
            self._runtime._locked(self.scope_id),
        ):
            service = context.artifacts.memory
            current = await service.head(context.artifacts.memory_artifact_id)
            current_entries = await service.entries(current)
            entries_by_id = {e.entry_id: e for e in current_entries}
            manifest_by_id = {m.entry_id: m for m in current.content.manifest.entries}

            to_forget = []
            to_reactivate = []
            for citation, target_state in items:
                entry = entries_by_id.get(citation.entry_id)
                manifest_item = manifest_by_id.get(citation.entry_id)
                if entry is None or manifest_item is None:
                    continue
                if manifest_item.state == target_state:
                    continue
                if target_state == "inactive":
                    to_forget.append(entry)
                else:
                    to_reactivate.append(entry)

            if to_forget:
                current = await service.forget(current, entries=to_forget, reason=reason)
            if to_reactivate:
                current = await service.reactivate(current, entries=to_reactivate, reason=reason)


class LifecycleApplication:
    """Entry point for scoped lifecycle services."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedLifecycleApplication:
        return ScopedLifecycleApplication(self._runtime, scope_id)


__all__ = [
    "LifecycleApplication",
    "ScopedLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "ensure_source_lifecycle_schema",
]
