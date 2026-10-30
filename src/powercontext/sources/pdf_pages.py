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

"""Complete PDF parsing and page-level Source retrieval."""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from pypdf import PdfReader

from powercontext.artifacts import MemoryCitation
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


def _validate_document_id(value: str) -> str:
    if not isinstance(value, str) or not value or not value.strip() or value != value.strip():
        raise ValueError("document_id must be non-empty and contain no leading or trailing whitespace")
    return value


def _page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    identity = json.dumps((document_id, document_sha256, page_number), ensure_ascii=False, separators=(",", ":"))
    return f"pdf-page:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


class PDFDocumentInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    content: bytes

    @model_validator(mode="after")
    def validate_document(self) -> PDFDocumentInput:
        _validate_document_id(self.document_id)
        if not self.content:
            raise ValueError("PDF content cannot be empty")
        return self


class PDFPageInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @model_validator(mode="after")
    def validate_page(self) -> PDFPageInput:
        _validate_document_id(self.document_id)
        if not re.fullmatch(r"[0-9a-f]{64}", self.document_sha256):
            raise ValueError("document_sha256 must be a lowercase SHA-256 digest")
        return self


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    if not isinstance(value, PDFDocumentInput):
        raise ValueError("Input must be a PDFDocumentInput instance")
    raw = value.content
    if not raw:
        raise ValueError("PDF content cannot be empty")

    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a password")
        digest = hashlib.sha256(raw).hexdigest()
        pages = tuple(
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=digest,
                page_number=index,
                text=page.extract_text() or "",
            )
            for index, page in enumerate(reader.pages, start=1)
        )
        if not pages:
            raise ValueError("PDF contains no pages")
        return pages
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF is damaged or cannot be fully parsed") from error


class PDFPageSource(Source):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    source_id: str
    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    definition_version: str = "1"
    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str

    @model_validator(mode="after")
    def validate_source(self) -> PDFPageSource:
        _validate_document_id(self.document_id)
        if not re.fullmatch(r"[0-9a-f]{64}", self.document_sha256):
            raise ValueError("document_sha256 must be a lowercase SHA-256 digest")
        expected = _page_source_id(self.document_id, self.document_sha256, self.page_number)
        if self.name != expected or self.source_id != expected:
            raise ValueError("PDF page source identity does not match its document revision and page")
        if self.materialization != SourceMaterialization.CAPTURED or self.definition_version != "1":
            raise ValueError("PDF page source must be CAPTURED definition version 1")
        return self


class PDFPageTextEvidenceProjection:
    name: str = TEXT_EVIDENCE_PROJECTION_KEY.name
    version: str = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PDFPageSource] = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        metadata: dict[str, JsonValue] = {
            "document_id": source.document_id,
            "document_sha256": source.document_sha256,
            "page_number": source.page_number,
        }
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata=metadata,
        )


class PDFPageSourceAdapter:
    input_class: type[PDFPageInput] = PDFPageInput
    name: str = "pdf-page"
    source_class: type[PDFPageSource] = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        if not isinstance(value, PDFPageInput):
            value = PDFPageInput.model_validate(value)
        source_id = _page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        if not isinstance(source, PDFPageSource):
            source = PDFPageSource.model_validate(source)
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    adapter=PDFPageSourceAdapter(),
    version="1",
    projections=(PDFPageTextEvidenceProjection(),),
)


_STOP_WORDS = frozenset(
    "a an and are as at be by can calculate define for from give has have how in is it less of on or our "
    "reported relying state that the their then to use what when where which with within year".split()
)
_WORDS = re.compile(r"[a-z]+|\d+", re.IGNORECASE)


def _query_terms(question: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            word.lower()
            for word in _WORDS.findall(question)
            if len(word) > 2 and word.lower() not in _STOP_WORDS
        )
    )


def _rank_hits(question: str, hits: list[Any]) -> list[Any]:
    terms = _query_terms(question)
    if not terms:
        return hits
    documents = [Counter(word.lower() for word in _WORDS.findall(hit.text)) for hit in hits]
    document_frequency = Counter(term for term in terms for document in documents if document[term])
    count = len(documents)
    scored = []
    for hit, frequencies in zip(hits, documents, strict=True):
        score = sum(
            (1 + (count - document_frequency[term]) / (1 + document_frequency[term]))
            * (1 + min(frequencies[term], 3) / 3)
            for term in terms
            if frequencies[term]
        )
        scored.append((score, hit.score, hit))
    return [hit for _, _, hit in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)]


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be non-empty text")
    if limit < 1:
        raise ValueError("limit must be positive")

    terms = _query_terms(question)
    queries = (question, *terms[:16])
    hits_by_id: dict[tuple[str, str], Any] = {}
    candidate_limit = max(50, min(200, limit * 5))
    for query in queries:
        result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=candidate_limit))
        for hit in result.hits:
            hits_by_id[(hit.entry_id, hit.entry_version_id)] = hit

    ranked = _rank_hits(question, list(hits_by_id.values()))
    citations: list[MemoryCitation] = []
    page_ids: set[str] = set()
    for hit in ranked:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = await memory.get(GetMemoryEntryRequest(citation=citation))
        source_ids = {
            source.source_id for source in entry.entry.sources if source.source_type == "pdf-page"
        }
        if len(source_ids) == 1 and source_ids.isdisjoint(page_ids):
            citations.append(citation)
            page_ids.update(source_ids)
            if len(citations) == limit:
                break
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "parse_pdf_document",
    "search_pdf_pages",
]
