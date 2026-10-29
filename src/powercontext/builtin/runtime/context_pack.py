# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Incremental Context Pack serialization, delta computation, and verified reconstruction."""

# ruff: noqa: TRY003

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field

__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]

_SCHEMA_PACK = "powercontext.context-pack.v1"
_SCHEMA_DELTA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """Raised when a ContextPack or ContextPackDelta wire payload is invalid."""


def _encode_json(obj: object) -> bytes:
    """Compact, deterministic JSON encoding."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(chunk).decode("ascii") for chunk in items]


def _compute_digest(items_b64: list[str]) -> str:
    """SHA-256 digest over the canonical {schema, items} representation."""
    unsigned = {"items": items_b64, "schema": _SCHEMA_PACK}
    return "sha256:" + hashlib.sha256(_encode_json(unsigned)).hexdigest()


def _solve_pack_bytes(items_b64: list[str], digest: str) -> bytes:
    """Return the final canonical serialisation with the correct self-referential byte_count.

    The byte count field itself contributes to the length, so we iterate until stable
    (typically converges in at most 2 iterations).
    """
    payload: dict[str, object] = {
        "byte_count": 0,
        "digest": digest,
        "items": items_b64,
        "schema": _SCHEMA_PACK,
    }
    encoded = _encode_json(payload)
    c = len(encoded)
    payload["byte_count"] = c
    encoded = _encode_json(payload)
    while len(encoded) != c:
        c = len(encoded)
        payload["byte_count"] = c
        encoded = _encode_json(payload)
    return encoded


def _chunk_content(content: str, chunk_bytes: int) -> tuple[bytes, ...]:
    """Split *content* into UTF-8 byte chunks of at most *chunk_bytes* bytes.

    Strategy: split on line boundaries first to maximise common prefix between
    consecutive context packs, then split long lines on character boundaries.
    """
    lines: list[bytes] = [line.encode("utf-8") for line in content.splitlines(keepends=True)]
    chunks: list[bytes] = []
    current = bytearray()

    for line in lines:
        if len(line) > chunk_bytes:
            # Flush current accumulation before splitting the oversized line.
            if current:
                chunks.append(bytes(current))
                current = bytearray()
            # Split the line character by character (decoded from UTF-8).
            line_str = line.decode("utf-8")
            for char in line_str:
                char_bytes = char.encode("utf-8")
                if len(char_bytes) > chunk_bytes:
                    raise ContextPackError(
                        f"Single character occupies {len(char_bytes)} bytes, exceeding chunk_bytes={chunk_bytes}"
                    )
                if len(current) + len(char_bytes) > chunk_bytes:
                    chunks.append(bytes(current))
                    current = bytearray()
                current.extend(char_bytes)
            if current:
                chunks.append(bytes(current))
                current = bytearray()
        else:
            if len(current) + len(line) > chunk_bytes:
                chunks.append(bytes(current))
                current = bytearray()
            current.extend(line)

    if current:
        chunks.append(bytes(current))

    return tuple(chunks)


@dataclass(frozen=True, eq=True)
class ContextPack:
    """A complete context payload split into verifiable byte chunks.

    Wire format (JSON, compact, sorted keys)::

        {
          "byte_count": <int>,
          "digest": "sha256:<hex>",
          "items": ["<base64>", ...],
          "schema": "powercontext.context-pack.v1"
        }

    ``byte_count`` is self-referential — its value equals ``len(to_bytes())``.
    ``digest`` covers only the ``{schema, items}`` subset.
    """

    items: tuple[bytes, ...]
    digest: str
    byte_count: int
    # Internal: precomputed serialisation, not part of equality / hash.
    _cached_bytes: bytes = field(default=b"", repr=False, compare=False)

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, items: tuple[bytes, ...] | list[bytes]) -> ContextPack:
        """Build a ContextPack from raw byte chunks."""
        items_tuple = tuple(items)
        items_b64 = _b64_items(items_tuple)
        digest = _compute_digest(items_b64)
        encoded = _solve_pack_bytes(items_b64, digest)
        byte_count = len(encoded)
        obj = object.__new__(cls)
        object.__setattr__(obj, "items", items_tuple)
        object.__setattr__(obj, "digest", digest)
        object.__setattr__(obj, "byte_count", byte_count)
        object.__setattr__(obj, "_cached_bytes", encoded)
        return obj

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> ContextPack:
        """Chunk *content* into UTF-8 byte chunks and build a ContextPack.

        Raises :class:`ContextPackError` if a single character cannot fit in *chunk_bytes*.
        """
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > 0 and chunk_bytes > 0:
            # Validate that every character individually can fit.
            for char in content:
                cb = char.encode("utf-8")
                if len(cb) > chunk_bytes:
                    raise ContextPackError(
                        f"Character U+{ord(char):04X} requires {len(cb)} bytes but chunk_bytes={chunk_bytes}"
                    )
        chunks = _chunk_content(content, chunk_bytes)
        return cls.build(chunks)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Deserialise and verify a wire-format ContextPack.

        Raises :class:`ContextPackError` on any structural or digest mismatch.
        """
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ContextPackError(f"Invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ContextPackError("Payload must be a JSON object")
        required = {"schema", "items", "digest", "byte_count"}
        missing = required - payload.keys()
        if missing:
            raise ContextPackError(f"Missing fields: {missing}")
        if payload["schema"] != _SCHEMA_PACK:
            raise ContextPackError(f"Unknown schema: {payload['schema']!r}")
        try:
            items_b64: list[str] = list(payload["items"])
            items_tuple = tuple(base64.b64decode(b) for b in items_b64)
        except Exception as exc:
            raise ContextPackError(f"Invalid items encoding: {exc}") from exc
        expected_digest = _compute_digest(list(items_b64))
        if payload["digest"] != expected_digest:
            raise ContextPackError(f"Digest mismatch: got {payload['digest']!r}, expected {expected_digest!r}")
        pack = cls.build(items_tuple)
        if pack.byte_count != payload.get("byte_count"):
            raise ContextPackError("byte_count mismatch")
        return pack

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Return the canonical wire-format serialisation."""
        if self._cached_bytes:
            return self._cached_bytes
        items_b64 = _b64_items(self.items)
        encoded = _solve_pack_bytes(items_b64, self.digest)
        return encoded


@dataclass(frozen=True, eq=True)
class ContextPackDelta:
    """An incremental update from one ContextPack to another.

    Wire format (JSON, compact, sorted keys)::

        {
          "appended": ["<base64>", ...],
          "base_digest": "sha256:<hex>",
          "prefix_items": <int>,
          "schema": "powercontext.context-pack-delta.v1",
          "target_digest": "sha256:<hex>"
        }

    ``byte_count`` is a **computed property** — it is NOT part of the wire format.
    """

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def byte_count(self) -> int:
        """Number of bytes in the wire representation (not a wire field)."""
        return len(self.to_bytes())

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Compute the minimal delta from *base* to *target* using longest common prefix."""
        prefix = 0
        for b_chunk, t_chunk in zip(base.items, target.items, strict=False):
            if b_chunk == t_chunk:
                prefix += 1
            else:
                break
        appended = target.items[prefix:]
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=appended,
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Deserialise and validate a wire-format ContextPackDelta.

        Raises :class:`ContextPackError` on structural errors.
        """
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ContextPackError(f"Invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ContextPackError("Payload must be a JSON object")
        required = {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
        missing = required - payload.keys()
        if missing:
            raise ContextPackError(f"Missing fields: {missing}")
        if payload["schema"] != _SCHEMA_DELTA:
            raise ContextPackError(f"Unknown schema: {payload['schema']!r}")
        prefix = payload["prefix_items"]
        # Reject bool (True/False) and non-integer types like float.
        if type(prefix) is not int:
            raise ContextPackError(f"prefix_items must be a plain integer, got {type(prefix).__name__}")
        if prefix < 0:
            raise ContextPackError(f"prefix_items must be non-negative, got {prefix}")
        try:
            appended = tuple(base64.b64decode(b) for b in payload["appended"])
        except Exception as exc:
            raise ContextPackError(f"Invalid appended encoding: {exc}") from exc
        return cls(
            base_digest=payload["base_digest"],
            target_digest=payload["target_digest"],
            prefix_items=prefix,
            appended=appended,
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Return the canonical wire-format serialisation."""
        payload = {
            "appended": _b64_items(self.appended),
            "base_digest": self.base_digest,
            "prefix_items": self.prefix_items,
            "schema": _SCHEMA_DELTA,
            "target_digest": self.target_digest,
        }
        return _encode_json(payload)


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Reconstruct the target ContextPack from *base* and *delta*.

    Raises :class:`ContextPackError` if:
    - ``delta.base_digest`` does not match ``base.digest``,
    - ``delta.prefix_items`` exceeds the number of base items, or
    - the reconstructed target digest does not match ``delta.target_digest``.
    """
    if delta.base_digest != base.digest:
        raise ContextPackError(f"Base digest mismatch: delta expects {delta.base_digest!r}, base has {base.digest!r}")
    if delta.prefix_items > len(base.items):
        raise ContextPackError(f"prefix_items={delta.prefix_items} exceeds base length {len(base.items)}")
    target_items = base.items[: delta.prefix_items] + delta.appended
    target = ContextPack.build(target_items)
    if target.digest != delta.target_digest:
        raise ContextPackError(f"Target digest mismatch: expected {delta.target_digest!r}, got {target.digest!r}")
    return target


# Convenience union type for callers returning either a full pack or a delta.
ContextPackOrDelta = ContextPack | ContextPackDelta
