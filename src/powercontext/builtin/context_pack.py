# ruff: noqa: TRY003
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

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

SCHEMA_PACK = "powercontext.context-pack.v1"
SCHEMA_DELTA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """Protocol or chunking validation error in context pack processing."""


def _encode_canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Encode a mapping to canonical JSON with sorted keys and no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode_json(data: Any, target_name: str) -> dict[str, Any]:
    """Validate raw bytes and parse a JSON dictionary."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ContextPackError(f"{target_name} data must be bytes, got {type(data).__name__}")
    try:
        decoded = json.loads(bytes(data).decode("utf-8"))
    except Exception as exc:
        raise ContextPackError(f"Invalid JSON for {target_name}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ContextPackError(f"{target_name} payload must be a JSON object")
    return decoded


def _validate_b64_utf8_items(items_raw: Any, field_name: str) -> tuple[bytes, ...]:
    """Validate a list of Base64 strings and return their UTF-8 decoded bytes."""
    if not isinstance(items_raw, (list, tuple)):
        raise ContextPackError(f"'{field_name}' must be a list")
    result: list[bytes] = []
    for item in items_raw:
        if not isinstance(item, str):
            raise ContextPackError(f"Each item in '{field_name}' must be a base64 string")
        try:
            raw_bytes = base64.b64decode(item, validate=True)
        except Exception as exc:
            raise ContextPackError(f"Invalid base64 in '{field_name}': {exc}") from exc
        try:
            raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContextPackError(f"Item in '{field_name}' is not valid UTF-8: {exc}") from exc
        result.append(raw_bytes)
    return tuple(result)


def _compute_pack_digest(items_b64: list[str]) -> str:
    """Compute sha256 hex digest for unsigned context pack JSON."""
    unsigned = {
        "items": items_b64,
        "schema": SCHEMA_PACK,
    }
    canonical = _encode_canonical_json(unsigned)
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _solve_pack_byte_count(items_b64: list[str], digest: str) -> tuple[int, bytes]:
    """Compute the self-referential byte_count and canonical bytes for ContextPack JSON."""
    payload = {
        "byte_count": 0,
        "digest": digest,
        "items": items_b64,
        "schema": SCHEMA_PACK,
    }
    encoded = _encode_canonical_json(payload)
    c = len(encoded)
    payload["byte_count"] = c
    encoded = _encode_canonical_json(payload)
    while len(encoded) != c:
        c = len(encoded)
        payload["byte_count"] = c
        encoded = _encode_canonical_json(payload)
    return c, encoded


def _split_long_line(line: bytes, chunk_bytes: int) -> list[bytes]:
    """Split a line exceeding chunk_bytes into UTF-8 bounded pieces."""
    pieces: list[bytes] = []
    idx = 0
    n = len(line)
    while idx < n:
        end = min(idx + chunk_bytes, n)
        if end < n:
            while end > idx and (line[end] & 0xC0) == 0x80:
                end -= 1
            if end == idx:
                raise ContextPackError(f"Cannot form valid UTF-8 chunk within {chunk_bytes} bytes")
        pieces.append(line[idx:end])
        idx = end
    return pieces


def _chunk_content(content_bytes: bytes, chunk_bytes: int) -> tuple[bytes, ...]:
    """Slice content bytes deterministically into UTF-8 bounded chunks not exceeding chunk_bytes."""
    if not content_bytes:
        return ()

    chunks: list[bytes] = []
    lines = content_bytes.splitlines(keepends=True)
    current_chunk = bytearray()

    for line in lines:
        if len(line) <= chunk_bytes:
            if len(current_chunk) + len(line) <= chunk_bytes:
                current_chunk.extend(line)
            else:
                if current_chunk:
                    chunks.append(bytes(current_chunk))
                    current_chunk = bytearray()
                current_chunk.extend(line)
        else:
            if current_chunk:
                chunks.append(bytes(current_chunk))
                current_chunk = bytearray()
            chunks.extend(_split_long_line(line, chunk_bytes))

    if current_chunk:
        chunks.append(bytes(current_chunk))

    return tuple(chunks)


def _is_valid_sha256(digest: Any) -> bool:
    """Check if digest is a canonical sha256:<hex> string."""
    if not isinstance(digest, str) or not digest.startswith("sha256:") or len(digest) != 71:
        return False
    hex_part = digest[7:]
    return all(c in "0123456789abcdefABCDEF" for c in hex_part)


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Complete, self-verifying context package with chunked contents."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int
    _cached_bytes: bytes = field(default=b"", repr=False, compare=False)

    schema: ClassVar[str] = SCHEMA_PACK

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))
        if type(self.byte_count) is not int or isinstance(self.byte_count, bool) or self.byte_count < 0:
            raise ContextPackError("byte_count must be a non-negative integer")

    @property
    def content(self) -> str:
        """Return the reconstructed full context string decoded as UTF-8."""
        return b"".join(self.items).decode("utf-8")

    @property
    def text(self) -> str:
        """Alias for content."""
        return self.content

    def to_bytes(self) -> bytes:
        """Serialize this context pack into canonical wire JSON bytes."""
        if self._cached_bytes:
            return self._cached_bytes
        payload = {
            "byte_count": self.byte_count,
            "digest": self.digest,
            "items": [base64.b64encode(b).decode("ascii") for b in self.items],
            "schema": self.schema,
        }
        encoded = _encode_canonical_json(payload)
        object.__setattr__(self, "_cached_bytes", encoded)
        return encoded

    @classmethod
    def build(cls, items: Iterable[bytes]) -> ContextPack:
        """Construct a ContextPack from an item sequence, verifying UTF-8 and computing digest."""
        validated_items: list[bytes] = []
        for item in items:
            if not isinstance(item, (bytes, bytearray, memoryview)):
                raise ContextPackError(f"All items must be bytes, got {type(item).__name__}")
            b = bytes(item)
            try:
                b.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContextPackError("Each item must be valid UTF-8") from exc
            validated_items.append(b)
        items_tuple = tuple(validated_items)
        items_b64 = [base64.b64encode(b).decode("ascii") for b in items_tuple]
        digest = _compute_pack_digest(items_b64)
        byte_count, encoded = _solve_pack_byte_count(items_b64, digest)
        return cls(items=items_tuple, digest=digest, byte_count=byte_count, _cached_bytes=encoded)

    @classmethod
    def from_content(
        cls,
        content: str | bytes | None,
        *,
        chunk_bytes: int = 512,
    ) -> ContextPack:
        """Chunk raw content deterministically into valid UTF-8 segments bounded by chunk_bytes."""
        if not isinstance(chunk_bytes, int) or isinstance(chunk_bytes, bool) or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")

        if content is None:
            return cls.build(())

        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray, memoryview)):
            content_bytes = bytes(content)
            try:
                content_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContextPackError("content must be valid UTF-8") from exc
        else:
            raise ContextPackError(f"content must be str, bytes, or None, got {type(content).__name__}")

        if not content_bytes:
            return cls.build(())

        chunks = _chunk_content(content_bytes, chunk_bytes)
        return cls.build(chunks)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Deserialize and strictly validate a ContextPack from canonical wire JSON bytes."""
        payload = _decode_json(data, "ContextPack")
        if set(payload.keys()) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError(f"ContextPack keys mismatch: {set(payload.keys())}")
        if payload["schema"] != cls.schema:
            raise ContextPackError(f"Invalid schema: {payload['schema']}")

        byte_count = payload["byte_count"]
        if type(byte_count) is not int or isinstance(byte_count, bool) or byte_count < 0:
            raise ContextPackError("byte_count must be a non-negative integer")
        if byte_count != len(data):
            raise ContextPackError(f"byte_count mismatch: payload says {byte_count}, data is {len(data)}")

        digest = payload["digest"]
        if not _is_valid_sha256(digest):
            raise ContextPackError("digest must be a sha256 string")

        items_raw = payload["items"]
        items = _validate_b64_utf8_items(items_raw, "items")

        expected_digest = _compute_pack_digest(items_raw)
        if digest != expected_digest:
            raise ContextPackError(f"digest mismatch: expected {expected_digest}, got {digest}")

        return cls(items=items, digest=digest, byte_count=byte_count, _cached_bytes=bytes(data))


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """Incremental delta reusing a base pack prefix and carrying appended items."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...] = ()
    _cached_bytes: bytes = field(default=b"", repr=False, compare=False)

    schema: ClassVar[str] = SCHEMA_DELTA

    def __post_init__(self) -> None:
        if not isinstance(self.appended, tuple):
            object.__setattr__(self, "appended", tuple(self.appended))

    @property
    def byte_count(self) -> int:
        """Return the wire serialized byte count of this delta."""
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize this delta into canonical wire JSON bytes."""
        if self._cached_bytes:
            return self._cached_bytes
        payload = {
            "appended": [base64.b64encode(b).decode("ascii") for b in self.appended],
            "base_digest": self.base_digest,
            "prefix_items": self.prefix_items,
            "schema": self.schema,
            "target_digest": self.target_digest,
        }
        encoded = _encode_canonical_json(payload)
        object.__setattr__(self, "_cached_bytes", encoded)
        return encoded

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Deserialize and strictly validate a ContextPackDelta from canonical wire JSON bytes."""
        payload = _decode_json(data, "ContextPackDelta")
        if set(payload.keys()) != {"schema", "base_digest", "target_digest", "prefix_items", "appended"}:
            raise ContextPackError(f"ContextPackDelta keys mismatch: {set(payload.keys())}")
        if payload["schema"] != cls.schema:
            raise ContextPackError(f"Invalid schema: {payload['schema']}")

        base_digest = payload["base_digest"]
        target_digest = payload["target_digest"]
        if not _is_valid_sha256(base_digest):
            raise ContextPackError("base_digest must be a sha256 string")
        if not _is_valid_sha256(target_digest):
            raise ContextPackError("target_digest must be a sha256 string")

        prefix = payload["prefix_items"]
        if type(prefix) is not int or isinstance(prefix, bool) or prefix < 0:
            raise ContextPackError(f"prefix_items must be a non-negative integer, got {prefix!r}")

        appended_raw = payload["appended"]
        appended = _validate_b64_utf8_items(appended_raw, "appended")

        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix,
            appended=appended,
            _cached_bytes=bytes(data),
        )

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Compute the longest common prefix between base and target packs."""
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("between requires ContextPack instances for base and target")

        prefix_items = 0
        for b, t in zip(base.items, target.items, strict=False):
            if b == t:
                prefix_items += 1
            else:
                break

        appended = target.items[prefix_items:]
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix_items,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Validate and apply a ContextPackDelta to base to reconstruct the target ContextPack."""
    if not isinstance(base, ContextPack):
        raise ContextPackError(f"base must be a ContextPack, got {type(base).__name__}")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError(f"delta must be a ContextPackDelta, got {type(delta).__name__}")

    if base.digest != delta.base_digest:
        raise ContextPackError(f"Base digest mismatch: expected {delta.base_digest}, got {base.digest}")

    if type(delta.prefix_items) is not int or isinstance(delta.prefix_items, bool) or delta.prefix_items < 0:
        raise ContextPackError(f"prefix_items must be a non-negative integer, got {delta.prefix_items!r}")

    if delta.prefix_items > len(base.items):
        raise ContextPackError(f"prefix_items {delta.prefix_items} exceeds base items count {len(base.items)}")

    reconstructed_items = base.items[: delta.prefix_items] + delta.appended
    target_pack = ContextPack.build(reconstructed_items)

    if target_pack.digest != delta.target_digest:
        raise ContextPackError(
            f"Target digest mismatch: expected {delta.target_digest}, reconstructed {target_pack.digest}"
        )

    return target_pack


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
