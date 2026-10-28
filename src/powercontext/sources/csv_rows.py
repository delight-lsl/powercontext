from __future__ import annotations

import csv
import inspect
import io
import json
import urllib.parse
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from powercontext.sources.models import (
    Source,
    SourceDefinition,
    SourceMaterialization,
    TextEvidence,
)

# 尝试导入 Projection 定义类（如存在）
try:
    from powercontext.sources.models import SourceProjection
except ImportError:
    SourceProjection = None  # type: ignore

try:
    from powercontext.sources.models import TEXT_EVIDENCE_PROJECTION
except ImportError:
    TEXT_EVIDENCE_PROJECTION = "powercontext.text-evidence"


# =====================================================================
# 1. 公共辅助与严格校验函数
# =====================================================================

def _validate_non_empty_no_edge_ws(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty and contain no leading/trailing whitespace")
    return value


def _validate_columns_and_values(
    document_id: str,
    primary_key: str,
    primary_key_value: str,
    columns: tuple[str, ...],
    values: tuple[str, ...],
) -> None:
    _validate_non_empty_no_edge_ws(document_id, "document_id")

    if not isinstance(primary_key, str) or not primary_key:
        raise ValueError("primary_key must be non-empty")

    if not columns:
        raise ValueError("columns cannot be empty")

    for col in columns:
        if not isinstance(col, str) or not col or not col.strip():
            raise ValueError("Header column names must be non-empty and non-whitespace")

    if len(set(columns)) != len(columns):
        raise ValueError("Header column names must be unique")

    if primary_key not in columns:
        raise ValueError(f"primary_key '{primary_key}' not found in columns")

    if len(values) != len(columns):
        raise ValueError("Row field count must equal header width")

    for val in values:
        if not isinstance(val, str):
            raise ValueError("All row values must be strings")

    if not isinstance(primary_key_value, str) or not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary_key_value cannot be empty or whitespace-only")

    pk_index = columns.index(primary_key)
    if values[pk_index] != primary_key_value:
        raise ValueError("primary_key_value must match the corresponding column value in values")


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """
    生成固定格式 identity：csv-row:<document_id>:<primary_key_value>
    两个组件分别按 urllib.parse.quote(component, safe="") 百分号编码，且总长度 <= 256。
    """
    _validate_non_empty_no_edge_ws(document_id, "document_id")
    if not isinstance(primary_key_value, str) or not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary_key_value cannot be empty or whitespace-only")

    encoded_doc = urllib.parse.quote(document_id, safe="")
    encoded_pk = urllib.parse.quote(primary_key_value, safe="")
    source_id = f"csv-row:{encoded_doc}:{encoded_pk}"

    if len(source_id) > 256:
        raise ValueError(f"Generated source_id exceeds 256 characters: {len(source_id)}")
    return source_id


# =====================================================================
# 2. 数据模型定义：CSVDocumentInput, CSVRowInput, CSVRowSource
# =====================================================================

class CSVDocumentInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_row_input(self) -> CSVRowInput:
        _validate_columns_and_values(
            self.document_id,
            self.primary_key,
            self.primary_key_value,
            self.columns,
            self.values,
        )
        csv_row_source_id(self.document_id, self.primary_key_value)
        return self


# 探测 Source 基类的默认字段（如 name, materialization, version 等）
_SOURCE_FIELDS = getattr(Source, "model_fields", {})
_CAPTURED_VAL = getattr(SourceMaterialization, "CAPTURED", "CAPTURED")


class CSVRowSource(Source):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = ""
    source_id: str = ""
    materialization: Any = _CAPTURED_VAL
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="before")
    @classmethod
    def _populate_identity_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            patched = dict(data)
            doc_id = patched.get("document_id")
            pk_val = patched.get("primary_key_value")
            if isinstance(doc_id, str) and isinstance(pk_val, str) and doc_id and doc_id == doc_id.strip() and pk_val.strip():
                expected_id = csv_row_source_id(doc_id, pk_val)
                if not patched.get("source_id"):
                    patched["source_id"] = expected_id
                if "name" in _SOURCE_FIELDS or "name" in cls.model_fields:
                    if not patched.get("name"):
                        patched["name"] = expected_id
            if "materialization" not in patched or patched["materialization"] is None:
                patched["materialization"] = _CAPTURED_VAL
            if "version" in _SOURCE_FIELDS and "version" not in patched:
                patched["version"] = 1
            return patched
        return data

    @model_validator(mode="after")
    def _validate_source(self) -> CSVRowSource:
        _validate_columns_and_values(
            self.document_id,
            self.primary_key,
            self.primary_key_value,
            self.columns,
            self.values,
        )
        expected_id = csv_row_source_id(self.document_id, self.primary_key_value)
        if self.source_id != expected_id:
            raise ValueError(f"source_id must be '{expected_id}', got '{self.source_id}'")
        if hasattr(self, "name") and self.name != expected_id:
            raise ValueError(f"name must be '{expected_id}', got '{self.name}'")
        if str(self.materialization).upper().endswith("CAPTURED") is False and self.materialization != _CAPTURED_VAL:
            raise ValueError("CSVRowSource materialization must be CAPTURED")
        return self


# =====================================================================
# 3. CSV 解析器：parse_csv_document
# =====================================================================

def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    if not isinstance(value, CSVDocumentInput):
        value = CSVDocumentInput.model_validate(value)

    document_id = _validate_non_empty_no_edge_ws(value.document_id, "document_id")
    primary_key = value.primary_key
    if not isinstance(primary_key, str) or not primary_key:
        raise ValueError("primary_key must be non-empty")

    if not isinstance(value.content, (bytes, bytearray, memoryview)):
        raise ValueError("content must be bytes")
    raw_bytes = bytes(value.content)

    # 1. 严格 UTF-8 解码，并接受文件开头的单个 UTF-8 BOM
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV content must be valid UTF-8") from exc

    if text.startswith("\ufeff"):
        text = text[1:]

    if not text:
        raise ValueError("CSV document cannot be empty")

    # 2. 按 Python csv.reader 固定语义解析 (strict=True)
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

    if not records:
        raise ValueError("CSV document contains no records")

    # 3. 第一条记录是表头（不能跳过表头前的空行；若首行是空行则表头为空，报错）
    header = records[0]
    if not header:
        raise ValueError("CSV header cannot be empty")

    columns = tuple(header)
    for col in columns:
        if not col or not col.strip():
            raise ValueError("Header columns must be non-empty and non-whitespace")

    if len(set(columns)) != len(columns):
        raise ValueError("Header columns must be unique")

    if primary_key not in columns:
        raise ValueError(f"primary_key '{primary_key}' not found in header")

    pk_idx = columns.index(primary_key)
    width = len(columns)

    # 4. 解析数据记录（忽略表头之后的完全空物理记录 `len(record) == 0`）
    rows: list[CSVRowInput] = []
    seen_pks: set[str] = set()

    for record in records[1:]:
        if len(record) == 0:
            continue

        if len(record) != width:
            raise ValueError(f"Row field count ({len(record)}) does not match header width ({width})")

        pk_val = record[pk_idx]
        if not pk_val or not pk_val.strip():
            raise ValueError("Primary key value cannot be empty or whitespace-only")

        if pk_val in seen_pks:
            raise ValueError(f"Duplicate primary key value: {pk_val!r}")
        seen_pks.add(pk_val)

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
# 4. Source Definition 与 TextEvidence 投影
# =====================================================================

def _resolve_csv_row(row_input: CSVRowInput) -> CSVRowSource:
    if not isinstance(row_input, CSVRowInput):
        row_input = CSVRowInput.model_validate(row_input)
    sid = csv_row_source_id(row_input.document_id, row_input.primary_key_value)
    return CSVRowSource(
        name=sid,
        source_id=sid,
        materialization=_CAPTURED_VAL,
        document_id=row_input.document_id,
        primary_key=row_input.primary_key,
        primary_key_value=row_input.primary_key_value,
        columns=row_input.columns,
        values=row_input.values,
    )


def _read_csv_row(source: CSVRowSource) -> CSVRowInput:
    if not isinstance(source, CSVRowSource):
        source = CSVRowSource.model_validate(source)
    return CSVRowInput(
        document_id=source.document_id,
        primary_key=source.primary_key,
        primary_key_value=source.primary_key_value,
        columns=source.columns,
        values=source.values,
    )


def _project_text_evidence(*args: Any) -> TextEvidence:
    """支持 (source,) 或 (source, payload) 两种调用签名"""
    source: CSVRowSource | None = None
    payload: CSVRowInput | None = None
    for arg in args:
        if isinstance(arg, CSVRowSource):
            source = arg
        elif isinstance(arg, CSVRowInput):
            payload = arg

    if payload is not None:
        doc_id = payload.document_id
        pk = payload.primary_key
        pk_val = payload.primary_key_value
        cols = payload.columns
        vals = payload.values
        sid = source.source_id if source is not None else csv_row_source_id(doc_id, pk_val)
    elif source is not None:
        doc_id = source.document_id
        pk = source.primary_key
        pk_val = source.primary_key_value
        cols = source.columns
        vals = source.values
        sid = source.source_id
    else:
        raise ValueError("Expected CSVRowSource or CSVRowInput to project TextEvidence")

    row_dict = dict(zip(cols, vals))
    content_json = json.dumps(
        row_dict,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    metadata: dict[str, Any] = {
        "document_id": doc_id,
        "primary_key": pk,
        "primary_key_value": pk_val,
        "columns": cols,
        "row": row_dict,
    }
    return TextEvidence(
        source_type="csv-row",
        source_id=sid,
        content=content_json,
        metadata=metadata,
    )


def _build_source_definition() -> SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput]:
    """根据 SourceDefinition 的真实字段动态组装 CSV_ROW_SOURCE_DEFINITION"""
    def_fields = getattr(SourceDefinition, "model_fields", {}) or getattr(SourceDefinition, "__dataclass_fields__", {})
    sig_params = set(def_fields.keys()) if def_fields else set(inspect.signature(SourceDefinition).parameters.keys())

    # 适配 _project_text_evidence 参数个数（有的框架传 (source, payload)，有的传 (payload,) 或 (source,)）
    kwargs: dict[str, Any] = {}
    if "name" in sig_params:
        kwargs["name"] = "csv-row"
    if "source_type" in sig_params:
        kwargs["source_type"] = "csv-row"
    if "version" in sig_params:
        kwargs["version"] = 1
    if "materialization" in sig_params:
        kwargs["materialization"] = _CAPTURED_VAL
    if "input_class" in sig_params:
        kwargs["input_class"] = CSVRowInput
    if "source_class" in sig_params:
        kwargs["source_class"] = CSVRowSource
    if "payload_class" in sig_params:
        kwargs["payload_class"] = CSVRowInput
    if "value_class" in sig_params:
        kwargs["value_class"] = CSVRowInput
    if "read_class" in sig_params:
        kwargs["read_class"] = CSVRowInput

    # 绑定 resolve 与 read 函数
    for resolve_key in ("resolve", "resolver", "resolve_fn"):
        if resolve_key in sig_params:
            kwargs[resolve_key] = _resolve_csv_row
    for read_key in ("read", "reader", "read_fn"):
        if read_key in sig_params:
            kwargs[read_key] = _read_csv_row

    # 绑定 powercontext.text-evidence 投影
    proj_name = str(TEXT_EVIDENCE_PROJECTION)
    for proj_key in ("projections", "projectors", "text_evidence", "project_text_evidence"):
        if proj_key in sig_params:
            if proj_key in ("text_evidence", "project_text_evidence"):
                kwargs[proj_key] = _project_text_evidence
            else:
                # 判断 projections 是 dict 还是 tuple[SourceProjection, ...]
                ann = str(def_fields[proj_key].annotation) if def_fields and proj_key in def_fields else ""
                if "dict" in ann or "Mapping" in ann:
                    if SourceProjection is not None and "SourceProjection" in ann:
                        kwargs[proj_key] = {proj_name: SourceProjection(name=proj_name, project=_project_text_evidence)}
                    else:
                        kwargs[proj_key] = {proj_name: _project_text_evidence}
                elif SourceProjection is not None:
                    sp_params = set(inspect.signature(SourceProjection).parameters.keys())
                    sp_kwargs: dict[str, Any] = {}
                    if "name" in sp_params:
                        sp_kwargs["name"] = proj_name
                    if "projection" in sp_params:
                        sp_kwargs["projection"] = proj_name
                    if "output_class" in sp_params:
                        sp_kwargs["output_class"] = TextEvidence
                    for fn_k in ("project", "projector", "fn", "handler"):
                        if fn_k in sp_params:
                            sp_kwargs[fn_k] = _project_text_evidence
                    kwargs[proj_key] = (SourceProjection(**sp_kwargs),)
                else:
                    kwargs[proj_key] = {proj_name: _project_text_evidence}

    return SourceDefinition(**kwargs)


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = _build_source_definition()


__all__ = [
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSV_ROW_SOURCE_DEFINITION",
    "csv_row_source_id",
    "parse_csv_document",
]
