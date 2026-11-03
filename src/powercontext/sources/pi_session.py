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

"""Pi session branch harvesting, event source definitions, and incremental connector."""

# Domain validation errors use standard ValueError across all archive and parsing boundaries.
# ruff: noqa: TRY003, TRY004

from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

# =====================================================================
# 1. Identity and fingerprint helpers
# =====================================================================


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Generate stable identity: pi:{percent-encoded session_id}:{percent-encoded event_id}."""
    if not isinstance(session_id, str) or not session_id or session_id != session_id.strip():
        raise ValueError("session_id must be non-empty and trimmed")
    if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
        raise ValueError("event_id must be non-empty and trimmed")

    encoded_session = urllib.parse.quote(session_id, safe="")
    encoded_event = urllib.parse.quote(event_id, safe="")
    source_id = f"pi:{encoded_session}:{encoded_event}"
    if len(source_id) > 256:
        raise ValueError(f"Generated source_id exceeds 256 characters ({len(source_id)} > 256)")
    return source_id


def _record_fingerprint(record: dict[str, Any]) -> str:
    serialized = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# =====================================================================
# 2. Data models
# =====================================================================


class PiSessionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    definition_version: str = "1"
    description: str | None = None
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _populate_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict):
            patched = dict(data)
            sess_id = patched.get("session_id")
            evt_id = patched.get("event_id")
            name = patched.get("name")
            if not name and isinstance(sess_id, str) and isinstance(evt_id, str) and sess_id.strip() and evt_id.strip():
                patched["name"] = pi_event_source_id(sess_id, evt_id)
            if "materialization" not in patched or patched["materialization"] is None:
                patched["materialization"] = SourceMaterialization.CAPTURED
            if "definition_version" not in patched or patched["definition_version"] is None:
                patched["definition_version"] = "1"
            return patched
        return data

    @model_validator(mode="after")
    def _validate_source(self) -> PiEventSource:
        expected_name = pi_event_source_id(self.session_id, self.event_id)
        if self.name != expected_name:
            raise ValueError(f"name must match expected identity: {expected_name!r}")
        if self.materialization != SourceMaterialization.CAPTURED:
            raise ValueError("PiEventSource materialization must be CAPTURED")
        if self.definition_version != "1":
            raise ValueError("definition_version must be '1'")
        return self


# =====================================================================
# 3. Visible content extraction
# =====================================================================


def _extract_text_or_blocks(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts: list[str] = []
        for item in val:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _extract_message_content(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts: list[str] = []
        for item in val:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif item_type == "toolCall":
                name = item.get("name", "")
                call_id = item.get("id", "")
                args = item.get("arguments", {})
                args_json = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                parts.append(f"[tool_call {name} {call_id}] {args_json}")
            # thinking, image, etc. are ignored
        return "\n".join(parts)
    return ""


def _extract_message_event(
    session_id: str,
    node_id: str,
    parent_id: str | None,
    record: dict[str, Any],
) -> PiEventInput | None:
    msg = record.get("message")
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    if not isinstance(role, str) or not role:
        return None

    if role == "bashExecution":
        cmd = msg.get("command", "")
        out = msg.get("output", "")
        return PiEventInput(
            session_id=session_id,
            event_id=node_id,
            parent_id=parent_id,
            event_type="message",
            role=role,
            content=f"bashExecution: $ {cmd}\n{out}",
            record=record,
        )

    if role == "toolResult":
        tool_name = msg.get("toolName", "")
        call_id = msg.get("toolCallId", "")
        text = _extract_text_or_blocks(msg.get("content"))
        body = f"[tool_result {tool_name} {call_id}] {text}" if text else f"[tool_result {tool_name} {call_id}]"
        return PiEventInput(
            session_id=session_id,
            event_id=node_id,
            parent_id=parent_id,
            event_type="message",
            role=role,
            content=f"toolResult: {body}",
            record=record,
        )

    body = _extract_message_content(msg.get("content"))
    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=node_id,
        parent_id=parent_id,
        event_type="message",
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _extract_visible_event(session_id: str, record: dict[str, Any]) -> PiEventInput | None:
    node_id = record["id"]
    parent_id = record.get("parentId")
    event_type = record.get("type", "")

    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return None
        return PiEventInput(
            session_id=session_id,
            event_id=node_id,
            parent_id=parent_id,
            event_type=event_type,
            role=event_type,
            content=f"{event_type}: {summary}",
            record=record,
        )

    if event_type == "custom_message":
        body = _extract_text_or_blocks(record.get("content"))
        if not body:
            return None
        return PiEventInput(
            session_id=session_id,
            event_id=node_id,
            parent_id=parent_id,
            event_type=event_type,
            role="custom",
            content=f"custom: {body}",
            record=record,
        )

    if event_type == "message":
        return _extract_message_event(session_id, node_id, parent_id, record)

    return None


# =====================================================================
# 4. JSONL parsing and graph validation
# =====================================================================


def _decode_jsonl_lines(content: bytes) -> list[dict[str, Any]]:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ValueError("content must be bytes")
    raw = bytes(content)
    if not raw:
        raise ValueError("Session archive content cannot be empty")

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Session content must be valid UTF-8") from exc

    if text.startswith("\ufeff"):
        text = text[1:]

    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        raise ValueError("Session archive contains no lines")

    records: list[dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        try:
            parsed = json.loads(line)
        except Exception as exc:
            raise ValueError(f"Malformed JSON on line {idx}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"Record on line {idx} must be a JSON object")
        records.append(parsed)
    return records


def _validate_header(header: dict[str, Any]) -> str:
    if header.get("type") != "session":
        raise ValueError("First record must have type 'session'")
    if header.get("version") != 3:
        raise ValueError(f"Expected session version 3, got {header.get('version')!r}")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Session header id must be a non-empty string")
    return session_id


def _index_nodes(node_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    seen_ids: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}

    for idx, rec in enumerate(node_records, start=2):
        node_id = rec.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(f"Record on line {idx} must have non-empty string id")
        if "parentId" not in rec:
            raise ValueError(f"Record on line {idx} must specify 'parentId'")
        parent_id = rec["parentId"]
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError(f"Record on line {idx} parentId must be string or null")

        if node_id in seen_ids:
            raise ValueError(f"Duplicate node id: {node_id!r}")
        if parent_id is not None and parent_id not in seen_ids:
            raise ValueError(f"Node {node_id!r} has unknown or forward parentId {parent_id!r}")

        seen_ids.add(node_id)
        by_id[node_id] = rec
    return by_id


def _trace_branch(by_id: dict[str, dict[str, Any]], target_leaf: str) -> list[dict[str, Any]]:
    branch: list[dict[str, Any]] = []
    curr: str | None = target_leaf
    visited: set[str] = set()

    while curr is not None:
        if curr in visited:
            raise ValueError(f"Cycle detected at node {curr!r}")
        visited.add(curr)
        node = by_id[curr]
        branch.append(node)
        curr = node.get("parentId")

    branch.reverse()
    return branch


def _validate_tree_and_get_branch(
    records: list[dict[str, Any]],
    leaf_id: str | None,
) -> tuple[str, list[dict[str, Any]], dict[str, dict[str, Any]]]:
    session_id = _validate_header(records[0])
    node_records = records[1:]
    if not node_records:
        if leaf_id is not None:
            raise ValueError(f"leaf_id {leaf_id!r} not found (session has no events)")
        return session_id, [], {}

    by_id = _index_nodes(node_records)
    target_leaf = leaf_id if leaf_id is not None else node_records[-1]["id"]
    if target_leaf not in by_id:
        raise ValueError(f"leaf_id {target_leaf!r} not found in session events")

    branch = _trace_branch(by_id, target_leaf)
    return session_id, branch, by_id


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    if not isinstance(value, PiSessionInput):
        raise ValueError("Input must be a PiSessionInput instance")

    records = _decode_jsonl_lines(value.content)
    session_id, branch_nodes, _ = _validate_tree_and_get_branch(records, value.leaf_id)

    visible: list[PiEventInput] = []
    for node in branch_nodes:
        evt = _extract_visible_event(session_id, node)
        if evt is not None:
            visible.append(evt)

    return tuple(visible)


# =====================================================================
# 5. Projection & Source Definition
# =====================================================================


class PiEventTextEvidenceProjection:
    name: str = TEXT_EVIDENCE_PROJECTION_KEY.name
    version: str = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PiEventSource] = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        metadata: dict[str, JsonValue] = {
            "session_id": source.session_id,
            "event_id": source.event_id,
            "parent_id": source.parent_id,
            "event_type": source.event_type,
            "role": source.role,
        }
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=source.content,
            metadata=metadata,
        )


class PiEventSourceAdapter:
    input_class: type[PiEventInput] = PiEventInput
    name: str = "pi-event"
    source_class: type[PiEventSource] = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        if not isinstance(value, PiEventInput):
            value = PiEventInput.model_validate(value)
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            materialization=SourceMaterialization.CAPTURED,
            definition_version="1",
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        if not isinstance(source, PiEventSource):
            source = PiEventSource.model_validate(source)
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    adapter=PiEventSourceAdapter(),
    version="1",
    projections=(PiEventTextEvidenceProjection(),),
)


# =====================================================================
# 6. Incremental Connector
# =====================================================================


class PiSessionConnector:
    name: str = "pi-session-history"
    version: str = "1"
    source_definitions: frozenset[str] = frozenset({"pi-event"})

    def __init__(self, path: Path | str, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        raw = self.path.read_bytes()
        records = _decode_jsonl_lines(raw)
        session_id, branch_nodes, all_nodes = _validate_tree_and_get_branch(records, self.leaf_id)

        branch_events: list[PiEventInput] = []
        for node in branch_nodes:
            evt = _extract_visible_event(session_id, node)
            if evt is not None:
                branch_events.append(evt)

        imported: dict[str, str] = {}
        if isinstance(session.checkpoint, dict):
            stored = session.checkpoint.get("imported")
            if isinstance(stored, dict):
                imported = {str(k): str(v) for k, v in stored.items()}

        for imp_id, expected_fp in imported.items():
            if imp_id in all_nodes:
                actual_fp = _record_fingerprint(all_nodes[imp_id])
                if actual_fp != expected_fp:
                    raise ValueError(f"Imported event {imp_id!r} was mutated in archive")

        new_imported = dict(imported)
        for event in branch_events:
            if event.event_id not in imported:
                await session.submit(
                    event.event_id,
                    "pi-event",
                    event,
                )
                new_imported[event.event_id] = _record_fingerprint(event.record)

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"imported": new_imported},
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
