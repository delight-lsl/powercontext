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

"""CSV row ingestion, row-level Source models, and TextEvidence projection."""

# Domain validation errors use standard ValueError across all CSV and type boundaries.
# ruff: noqa: TRY003, TRY004

from __future__ import annotations

import csv
import io
import json
import urllib.parse
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

# =====================================================================
# 1. Validation helpers
# =====================================================================


def _validate_document_id(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("document_id must be non-empty and contain no leading or trailing whitespace")
    return value


def _validate_primary_key(value: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError("primary_key must be non-empty and not pure whitespace")
    return value


def _validate_columns(columns: tuple[str, ...], primary_key: str) -> None:
    if not columns:
        raise ValueError("columns cannot be empty")

    for col in columns:
        if not isinstance(col, str) or not col or not col.strip():
            raise ValueError(f"Header column name must be non-empty and not pure whitespace: {col!r}")

    if len(set(columns)) != len(columns):
        raise ValueError("Header column names must be unique")

    if primary_key not in columns:
        raise ValueError(f"primary_key '{primary_key}' not found in columns")


def _validate_values(
    columns: tuple[str, ...],
    values: tuple[str, ...],
    primary_key: str,
    primary_key_value: str,
) -> None:
    if len(values) != len(columns):
        raise ValueError(f"Row field count ({len(values)}) must equal header width ({len(columns)})")

    for val in values:
        if not isinstance(val, str):
            raise ValueError("All row values must be strings")

    if not isinstance(primary_key_value, str) or not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary_key_value cannot be empty or pure whitespace")

    pk_index = columns.index(primary_key)
    if values[pk_index] != primary_key_value:
        raise ValueError("primary_key_value must match the corresponding column value in values")


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Generate fixed format identity: csv:{percent-encoded document_id}:{percent-encoded primary_key_value}."""
    _validate_document_id(document_id)
    if not isinstance(primary_key_value, str) or not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary_key_value cannot be empty or pure whitespace")

    encoded_doc = urllib.parse.quote(document_id, safe="")
    encoded_pk = urllib.parse.quote(primary_key_value, safe="")
    source_id = f"csv:{encoded_doc}:{encoded_pk}"

    if len(source_id) > 256:
        raise ValueError(f"Generated source_id exceeds 256 characters ({len(source_id)} > 256)")
    return source_id


# =====================================================================
# 2. Data models
# =====================================================================


class CSVDocumentInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_row_input(self) -> CSVRowInput:
        _validate_document_id(self.document_id)
        _validate_primary_key(self.primary_key)
        _validate_columns(self.columns, self.primary_key)
        _validate_values(self.columns, self.values, self.primary_key, self.primary_key_value)
        csv_row_source_id(self.document_id, self.primary_key_value)
        return self


class CSVRowSource(Source):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    source_id: str = ""
    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    definition_version: str = "1"
    description: str | None = None
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="before")
    @classmethod
    def _populate_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict):
            patched = dict(data)
            doc_id = patched.get("document_id")
            pk_val = patched.get("primary_key_value")
            source_id = patched.get("source_id")
            name = patched.get("name")
            if not source_id and not name:
                if isinstance(doc_id, str) and isinstance(pk_val, str) and doc_id and pk_val.strip():
                    sid = csv_row_source_id(doc_id, pk_val)
                    patched["source_id"] = sid
                    patched["name"] = sid
            elif not name and source_id:
                patched["name"] = source_id
            elif not source_id and name:
                patched["source_id"] = name
            if "materialization" not in patched or patched["materialization"] is None:
                patched["materialization"] = SourceMaterialization.CAPTURED
            if "definition_version" not in patched or patched["definition_version"] is None:
                patched["definition_version"] = "1"
            return patched
        return data

    @model_validator(mode="after")
    def _validate_source(self) -> CSVRowSource:
        _validate_document_id(self.document_id)
        _validate_primary_key(self.primary_key)
        _validate_columns(self.columns, self.primary_key)
        _validate_values(self.columns, self.values, self.primary_key, self.primary_key_value)
        expected_id = csv_row_source_id(self.document_id, self.primary_key_value)
        if self.source_id != expected_id:
            raise ValueError(
                f"source_id must match csv_row_source_id: expected {expected_id!r}, got {self.source_id!r}"
            )
        if self.name != expected_id:
            raise ValueError(f"name must match source_id: expected {expected_id!r}, got {self.name!r}")
        if self.materialization != SourceMaterialization.CAPTURED:
            raise ValueError("CSVRowSource materialization must be CAPTURED")
        if self.definition_version != "1":
            raise ValueError("definition_version must be '1'")
        return self


# =====================================================================
# 3. CSV parsing
# =====================================================================


def _decode_and_read_csv(content: bytes) -> list[list[str]]:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ValueError("content must be bytes")
    raw_bytes = bytes(content)

    if not raw_bytes:
        raise ValueError("CSV content cannot be empty")

    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV content must be valid UTF-8") from exc

    if text.startswith("\ufeff"):
        text = text[1:]

    if not text:
        raise ValueError("CSV content cannot be empty after BOM removal")

    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=",",
        quotechar='"',
        doublequote=True,
        strict=True,
    )
    try:
        records = list(reader)
    except csv.Error as exc:
        raise ValueError(f"Malformed CSV content: {exc}") from exc

    if not records or not records[0]:
        raise ValueError("CSV document contains no records or empty header")
    return records


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    if not isinstance(value, CSVDocumentInput):
        raise ValueError("Input must be a CSVDocumentInput instance")

    document_id = _validate_document_id(value.document_id)
    primary_key = _validate_primary_key(value.primary_key)
    records = _decode_and_read_csv(value.content)

    columns = tuple(records[0])
    _validate_columns(columns, primary_key)

    pk_index = columns.index(primary_key)
    width = len(columns)
    prefix_len = len(f"csv:{urllib.parse.quote(document_id, safe='')}:")

    rows: list[CSVRowInput] = []
    seen_pks: set[str] = set()

    for record in records[1:]:
        if len(record) == 0:
            continue

        if len(record) != width:
            raise ValueError(f"Row field count ({len(record)}) does not match header width ({width})")

        pk_val = record[pk_index]
        if not pk_val or not pk_val.strip():
            raise ValueError("Primary key value cannot be empty or pure whitespace")

        if pk_val in seen_pks:
            raise ValueError(f"Duplicate primary key value: {pk_val!r}")
        seen_pks.add(pk_val)

        if prefix_len + len(urllib.parse.quote(pk_val, safe="")) > 256:
            raise ValueError("Generated source_id exceeds 256 characters")

        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=pk_val,
                columns=columns,
                values=tuple(record),
            )
        )

    return tuple(rows)


# =====================================================================
# 4. Projection & Source Definition
# =====================================================================


class CSVRowTextEvidenceProjection:
    name: str = TEXT_EVIDENCE_PROJECTION_KEY.name
    version: str = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[CSVRowSource] = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        content = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        metadata: dict[str, JsonValue] = {
            "document_id": source.document_id,
            "primary_key": source.primary_key,
            "primary_key_value": source.primary_key_value,
            "columns": list(source.columns),
            "row": row,
        }
        return TextEvidence(
            source_type="csv-row",
            source_id=source.source_id,
            content=content,
            metadata=metadata,
        )


class CSVRowSourceAdapter:
    input_class: type[CSVRowInput] = CSVRowInput
    name: str = "csv-row"
    source_class: type[CSVRowSource] = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        if not isinstance(value, CSVRowInput):
            value = CSVRowInput.model_validate(value)
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            definition_version="1",
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        if not isinstance(source, CSVRowSource):
            source = CSVRowSource.model_validate(source)
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    adapter=CSVRowSourceAdapter(),
    version="1",
    projections=(CSVRowTextEvidenceProjection(),),
)


__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
