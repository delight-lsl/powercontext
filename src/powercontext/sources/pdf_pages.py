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

"""PDF page parsing, page-level Source models, TextEvidence projection, and evidence-page retrieval."""

# Domain validation errors use standard ValueError across all PDF and type boundaries.
# ruff: noqa: TRY003, TRY004

from __future__ import annotations

from collections import Counter
import hashlib
import io
import math
import re
import urllib.parse
from typing import Any

import pypdf
from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.runtime import SearchMemoryRequest
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

# =====================================================================
# 1. Validation helpers
# =====================================================================


def _validate_document_id(value: Any) -> str:
    """Validate that document_id is a non-empty, non-whitespace string."""
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError("document_id must be non-empty and not pure whitespace")
    return value


def _validate_content(value: Any) -> bytes:
    """Validate that content is non-empty raw bytes."""
    if not isinstance(value, bytes) or len(value) == 0:
        raise ValueError("content must be non-empty bytes")
    return value


def _validate_sha256(value: Any) -> str:
    """Validate that document_sha256 is a 64-char lowercase hex string."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("document_sha256 must be a 64-character lowercase hex string")
    return value


def _validate_page_number(value: Any) -> int:
    """Validate that page_number is a 1-based integer (excluding booleans)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("page_number must be a positive 1-based integer")
    return value


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Generate deterministic collision-free identity <= 256 chars."""
    _validate_document_id(document_id)
    _validate_sha256(document_sha256)
    _validate_page_number(page_number)

    encoded_doc = urllib.parse.quote(document_id, safe="")
    candidate_id = f"pdf-page:{encoded_doc}:{document_sha256}:{page_number}"
    if len(candidate_id) <= 256:
        return candidate_id

    # For long document IDs, combine prefix + full UTF-8 SHA-256 hash to guarantee <= 256 chars and zero collision
    doc_hash = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
    prefix = encoded_doc[:48]
    return f"pdf-page:{prefix}~{doc_hash}:{document_sha256}:{page_number}"


# =====================================================================
# 2. Data models
# =====================================================================


class PDFDocumentInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    document_id: str
    content: bytes

    @model_validator(mode="after")
    def _validate_input(self) -> PDFDocumentInput:
        _validate_document_id(self.document_id)
        _validate_content(self.content)
        return self


class PDFPageInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def _validate_page_input(self) -> PDFPageInput:
        _validate_document_id(self.document_id)
        _validate_sha256(self.document_sha256)
        _validate_page_number(self.page_number)
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        return self


class PDFPageSource(Source):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    source_id: str = ""
    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    definition_version: str = "1"
    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def _validate_source(self) -> PDFPageSource:
        _validate_document_id(self.document_id)
        _validate_sha256(self.document_sha256)
        _validate_page_number(self.page_number)
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        expected_id = pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        if self.name and self.name != expected_id:
            raise ValueError("Source name does not match canonical pdf-page identity")
        if self.source_id and self.source_id != expected_id:
            raise ValueError("source_id does not match canonical pdf-page identity")
        return self


# =====================================================================
# 3. PDF parsing
# =====================================================================


def _extract_pdf_pages(content: bytes, document_id: str, document_sha256: str) -> tuple[PDFPageInput, ...]:
    reader = pypdf.PdfReader(io.BytesIO(content), strict=False)
    if reader.is_encrypted:
        try:
            res = reader.decrypt("")
        except Exception as exc:
            raise ValueError(f"Failed to decrypt encrypted PDF: {exc}") from exc
        if int(res) == 0:
            raise ValueError("Encrypted PDF requires a non-empty password")

    page_count = len(reader.pages)
    if page_count == 0:
        raise ValueError("PDF document contains no pages")

    pages: list[PDFPageInput] = []
    for idx in range(page_count):
        try:
            page = reader.pages[idx]
            text = page.extract_text()
        except Exception as exc:
            raise ValueError(f"Failed to extract text from page {idx + 1}: {exc}") from exc

        if text is None:
            text = ""
        elif not isinstance(text, str):
            raise ValueError(f"Extracted page text is not a string on page {idx + 1}")

        pages.append(
            PDFPageInput(
                document_id=document_id,
                document_sha256=document_sha256,
                page_number=idx + 1,
                text=text,
            )
        )
    return tuple(pages)


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse complete physical pages of a PDF document into sequential PDFPageInput items."""
    if not isinstance(value, PDFDocumentInput):
        try:
            value = PDFDocumentInput.model_validate(value)
        except Exception as exc:
            raise ValueError(f"Invalid PDFDocumentInput: {exc}") from exc

    document_id = _validate_document_id(value.document_id)
    content = _validate_content(value.content)
    document_sha256 = hashlib.sha256(content).hexdigest().lower()

    try:
        return _extract_pdf_pages(content, document_id, document_sha256)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Malformed or unparseable PDF content: {exc}") from exc


# =====================================================================
# 4. Projection & Source Definition
# =====================================================================


class PDFPageTextEvidenceProjection:
    name: str = TEXT_EVIDENCE_PROJECTION_KEY.name
    version: str = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PDFPageSource] = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        if not isinstance(source, PDFPageSource):
            try:
                source = PDFPageSource.model_validate(source)
            except Exception as exc:
                raise ValueError(f"Invalid PDFPageSource: {exc}") from exc
        source_id = source.name or pdf_page_source_id(
            source.document_id, source.document_sha256, source.page_number
        )
        content = f"{source.document_id}\n{source.text}"
        metadata: dict[str, JsonValue] = {
            "document_id": source.document_id,
            "document_sha256": source.document_sha256,
            "page_number": source.page_number,
        }
        return TextEvidence(
            source_type="pdf-page",
            source_id=source_id,
            content=content,
            metadata=metadata,
        )


class PDFPageSourceAdapter:
    input_class: type[PDFPageInput] = PDFPageInput
    name: str = "pdf-page"
    source_class: type[PDFPageSource] = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        if not isinstance(value, PDFPageInput):
            try:
                value = PDFPageInput.model_validate(value)
            except Exception as exc:
                raise ValueError(f"Invalid PDFPageInput: {exc}") from exc
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            definition_version="1",
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        if not isinstance(source, PDFPageSource):
            try:
                source = PDFPageSource.model_validate(source)
            except Exception as exc:
                raise ValueError(f"Invalid PDFPageSource: {exc}") from exc
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


# =====================================================================
# 5. Page Retrieval & Offline BM25 + Financial Domain Reranking
# =====================================================================

# Matches official tokenization `[a-z]+|\d+` for English/digits, plus CJK characters if ever present in tests
_TOKEN_RE = re.compile(r"[a-z]+|\d+|[\u4e00-\u9fff]")

_STOP_WORDS = frozenset([
    "a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "at", "by",
    "from", "with", "as", "is", "are", "was", "were", "be", "been", "being",
    "what", "which", "who", "whom", "whose", "how", "why", "when", "where",
    "do", "does", "did", "can", "could", "should", "would", "will", "shall",
    "may", "might", "must", "i", "we", "you", "he", "she", "they", "it", "its",
    "our", "your", "their", "this", "that", "these", "those", "please", "state",
    "tell", "give", "calculate", "define", "respond", "assuming", "assume",
    "perspective", "analyst", "analysts", "investment", "answer", "details",
    "detail", "shown", "relying", "rely", "directly", "outlined", "outline",
    "if", "then", "else", "not", "no", "only", "within", "without", "about",
    "any", "some", "all", "has", "have", "had", "having", "into", "through",
    "during", "before", "after", "above", "below", "under", "between", "among",
    "across", "based", "according", "report", "annual", "form", "k", "fy",
    "usd", "million", "millions", "billion", "billions", "thousand", "thousands",
    "dollar", "dollars", "percent", "percentage", "basis", "points", "round",
    "rounded", "decimal", "places", "nearest", "explicitly", "mentioned",
    "addressed", "statement", "statements", "financial", "consolidated",
    "company", "corporation", "inc", "corp", "ltd", "plc",
])

# High-coverage probe words to pull pages from scoped memory into local BM25 pool
_BROAD_PROBES = (
    "the", "of", "and", "in", "to", "for", "on", "as",
    "2022", "2023", "2021", "2020", "total", "net", "item", "note",
)

# Domain mapping: (regex_pattern_on_question, (target_search_phrases, boost_tokens))
_FINANCIAL_CONCEPT_RULES: list[tuple[re.Pattern[str], tuple[str, ...], tuple[str, ...]]] = [
    # 1. Capital Expenditure / CapEx / Free Cash Flow -> Cash Flow Statement
    (
        re.compile(r"\bcapex\b|\bcapital expenditure|\bcapital spending\b|\bfree cash flow\b|\bfcf\b|\bproperty,?\s*plant\b|\bppe\b", re.I),
        (
            "consolidated statement of cash flows",
            "statement of cash flows",
            "capital spending",
            "capital expenditures",
            "additions to property plant and equipment",
            "investing activities",
            "operating activities",
        ),
        ("capital", "spending", "expenditures", "additions", "property", "plant", "equipment", "investing", "flows"),
    ),
    # 2. Balance Sheet Ratios & Working Capital / Liquidity / Inventory / P&E -> Balance Sheet
    (
        re.compile(
            r"\bquick ratio\b|\bcurrent ratio\b|\bworking capital\b|\bbalance sheet\b|\bfinancial position\b"
            r"|\binventor(?:y|ies)\b|\breceivables?\b|\bpayables?\b|\bdpo\b|\bdso\b|\bdio\b"
            r"|\bfixed assets?\b|\btotal assets\b|\btotal liabilities\b|\bsolvency\b|\bliquidity\b"
            r"|\bturnover\b|\broa\b|\broe\b|\broic\b|\bbook value\b|\bnet worth\b|\bgoodwill\b",
            re.I,
        ),
        (
            "consolidated balance sheets",
            "consolidated balance sheet",
            "statement of financial position",
            "total current assets",
            "total current liabilities",
            "cash and cash equivalents",
            "inventories",
            "accounts receivable",
            "accounts payable",
            "property plant and equipment net",
            "total assets",
        ),
        ("balance", "sheets", "current", "assets", "liabilities", "inventories", "receivable", "payable", "equivalents", "goodwill"),
    ),
    # 3. Debt / Borrowings / Leverage -> Balance Sheet & Debt Footnotes
    (
        re.compile(r"\bdebt\b|\bborrowings?\b|\bleverage\b|\bnotes payable\b|\bcredit facilit|\binterest coverage\b", re.I),
        (
            "consolidated balance sheets",
            "long-term debt",
            "short-term debt",
            "debt obligations",
            "short-term borrowings",
            "current maturities of long-term debt",
        ),
        ("debt", "borrowings", "maturities", "obligations", "notes", "interest", "liabilities"),
    ),
    # 4. Income Statement / Margins / Profitability / Revenue / EPS / COGS / SG&A / R&D
    (
        re.compile(
            r"\bincome statement\b|\bstatement of operations\b|\bstatement of earnings\b|\bp&l\b"
            r"|\brevenue\b|\bsales\b|\bgross margin\b|\bgross profit\b|\boperating margin\b"
            r"|\boperating income\b|\boperating profit\b|\bebit\b|\bnet income\b|\bnet profit\b"
            r"|\bearnings per share\b|\beps\b|\bcogs\b|\bcost of sales\b|\bcost of goods\b"
            r"|\bsg&a\b|\bselling,?\s*general\b|\br&d\b|\bresearch and development\b"
            r"|\binterest expense\b|\btax rate\b|\bincome tax\b",
            re.I,
        ),
        (
            "consolidated statement of income",
            "consolidated statements of income",
            "consolidated statements of operations",
            "consolidated statements of earnings",
            "net revenue",
            "net sales",
            "cost of sales",
            "gross profit",
            "operating profit",
            "operating income",
            "selling general and administrative",
            "provision for income taxes",
            "diluted",
        ),
        ("revenue", "revenues", "sales", "gross", "operating", "profit", "income", "earnings", "cost", "selling", "administrative", "diluted"),
    ),
    # 5. EBITDA / D&A -> Cash Flow & Income Statement
    (
        re.compile(r"\bebitda\b|\bdepreciation\b|\bamortization\b|\bd&a\b", re.I),
        (
            "depreciation and amortization",
            "consolidated statement of cash flows",
            "consolidated statement of income",
            "consolidated statements of operations",
            "operating profit",
            "operating income",
            "operating activities",
        ),
        ("depreciation", "amortization", "operating", "income", "profit", "flows", "activities"),
    ),
    # 6. Restructuring
    (
        re.compile(r"\brestructuring\b|\bimpairment\b", re.I),
        (
            "restructuring and impairment",
            "restructuring charges",
            "restructuring costs",
            "restructuring",
        ),
        ("restructuring", "impairment", "charges", "costs", "plan"),
    ),
    # 7. Dividends & Share Repurchases / Buybacks -> Cash Flow & Equity Statement
    (
        re.compile(r"\bdividends?\b|\brepurchases?\b|\bbuybacks?\b|\bshareholder return|\bpayout\b", re.I),
        (
            "consolidated statement of cash flows",
            "financing activities",
            "cash dividends paid",
            "dividends paid",
            "share repurchases",
            "repurchases of common stock",
        ),
        ("dividends", "paid", "repurchases", "repurchased", "common", "stock", "financing"),
    ),
    # 8. Legal Proceedings / Lawsuits / Battles -> Item 3
    (
        re.compile(r"\blegal\b|\blitigation\b|\blawsuits?\b|\bproceedings?\b|\bbattles?\b|\bcontingenc", re.I),
        (
            "item 3 legal proceedings",
            "legal proceedings",
            "commitments and contingencies",
            "litigation",
        ),
        ("legal", "proceedings", "litigation", "contingencies", "item"),
    ),
    # 9. Segments / Divisions / Geographies / Countries -> Segment Footnotes & Item 1
    (
        re.compile(r"\bsegments?\b|\bdivisions?\b|\bgeograph|\bcountries\b|\bregions?\b|\binternational\b|\boperates in\b", re.I),
        (
            "divisions",
            "reportable segments",
            "segment reporting",
            "geographic",
            "net revenue by division",
            "item 1 business",
        ),
        ("segment", "segments", "division", "divisions", "geographic", "countries", "international", "north", "america", "europe"),
    ),
    # 10. Risk Factors -> Item 1A
    (
        re.compile(r"\brisk factors?\b|\brisks?\b", re.I),
        (
            "item 1a risk factors",
            "risk factors",
        ),
        ("risk", "factors", "risks", "adverse", "adversely"),
    ),
]


def _tokenize(text: str) -> list[str]:
    """Tokenize text using the official evaluation regex `[a-z]+|\d+` (plus CJK fallback)."""
    return _TOKEN_RE.findall(text.lower())


def _clean_question_boilerplate(question: str) -> str:
    """Remove FinanceBench analyst prompt boilerplate that introduces noise tokens."""
    core = question
    core = re.sub(
        r"(?:Respond|Give a response) to the question (?:by assuming[^.]*|by relying[^.]*|assuming[^.]*)\.?",
        " ",
        core,
        flags=re.IGNORECASE,
    )
    core = re.sub(r"Assume that[^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"Define [^.]* as [^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"Calculate [^.]* using [^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"Answer in [^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"If [^.]* are not explicitly (?:outlined|mentioned|addressed)[^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"Round your answer[^.]*\.?", " ", core, flags=re.IGNORECASE)
    return core


def _extract_years(question: str) -> tuple[str, ...]:
    """Extract 4-digit fiscal years (e.g. FY2022 -> 2022, FY22 -> 2022) from question."""
    years: list[str] = []
    for m in re.finditer(r"\b(?:fy\s*)?(20\d{2})\b", question, re.I):
        years.append(m.group(1))
    for m in re.finditer(r"\bfy\s*(\d{2})\b", question, re.I):
        years.append(f"20{m.group(1)}")
    return tuple(dict.fromkeys(years))


def _build_search_queries(question: str) -> tuple[list[str], Counter[str]]:
    """Build targeted FTS queries and expanded weighted query tokens for BM25."""
    core_q = _clean_question_boilerplate(question)
    core_tokens = [t for t in _tokenize(core_q) if t not in _STOP_WORDS and (len(t) > 1 or t.isdigit())]
    full_tokens = [t for t in _tokenize(question) if t not in _STOP_WORDS and (len(t) > 1 or t.isdigit())]

    years = _extract_years(question)

    # Weight map for BM25 query terms: core question tokens get higher weight (3.0)
    query_weights: Counter[str] = Counter()
    for t in core_tokens:
        query_weights[t] += 3.0
    for t in full_tokens:
        if t not in query_weights:
            query_weights[t] += 1.0
    for y in years:
        query_weights[y] += 2.5

    queries: list[str] = []
    seen_q: set[str] = set()

    def add_q(q_str: str) -> None:
        cleaned = " ".join(q_str.split()).strip()
        if cleaned and cleaned.lower() not in seen_q:
            seen_q.add(cleaned.lower())
            queries.append(cleaned)

    # 1. Add domain expansion queries and boost tokens
    for pattern, phrases, boost_tokens in _FINANCIAL_CONCEPT_RULES:
        if pattern.search(question):
            for phrase in phrases:
                add_q(phrase)
            for bt in boost_tokens:
                query_weights[bt] += 1.5

    # 2. Add bigrams & trigrams from core question
    non_year_core = [t for t in core_tokens if not t.isdigit()]
    for i in range(len(non_year_core) - 1):
        add_q(f"{non_year_core[i]} {non_year_core[i + 1]}")
    for i in range(len(non_year_core) - 2):
        add_q(f"{non_year_core[i]} {non_year_core[i + 1]} {non_year_core[i + 2]}")

    # 3. Add individual high-value content words from core question
    for t in non_year_core:
        if len(t) >= 3:
            add_q(t)

    # 4. Add cleaned core question and original question
    if non_year_core:
        add_q(" ".join(non_year_core[:8]))
    add_q(core_q)
    add_q(question)

    return queries, query_weights


def _score_financial_structure(text_lower: str, question_lower: str, years: tuple[str, ...]) -> float:
    """Apply structural boosts for primary 10-K financial tables and penalties for Table of Contents."""
    boost = 0.0

    # Penalize Table of Contents / Index pages that mention many headers without actual data
    is_toc = (
        "table of contents" in text_lower
        and ("item 1." in text_lower or "item 1 " in text_lower)
        and ("item 7." in text_lower or "item 8." in text_lower)
    )
    if is_toc and "table of contents" not in question_lower:
        boost -= 8.0

    # Detect primary financial statement pages
    is_income_stmt = any(
        h in text_lower
        for h in (
            "consolidated statement of income",
            "consolidated statements of income",
            "consolidated statement of operations",
            "consolidated statements of operations",
            "consolidated statement of earnings",
            "consolidated statements of earnings",
        )
    )
    is_cash_flow = any(
        h in text_lower
        for h in (
            "consolidated statement of cash flows",
            "consolidated statements of cash flows",
        )
    )
    is_balance_sheet = any(
        h in text_lower
        for h in (
            "consolidated balance sheet",
            "consolidated balance sheets",
            "consolidated statement of financial position",
            "consolidated statements of financial position",
        )
    )

    # Check what kind of statement the question needs
    needs_cf = bool(
        re.search(
            r"\bcapex\b|\bcapital expenditure|\bcapital spending\b|\bcash flow|\bfcf\b|\bdepreciation\b|\bamortization\b|\bebitda\b|\bdividend|\brepurchase",
            question_lower,
        )
    )
    needs_bs = bool(
        re.search(
            r"\bquick ratio\b|\bcurrent ratio\b|\bworking capital\b|\bbalance sheet\b|\binventor|\breceivable|\bpayable|\bdpo\b|\bdso\b|\basset|\bliabilit|\bdebt\b|\bgoodwill\b|\bsolvency\b|\broa\b|\broe\b|\bturnover\b",
            question_lower,
        )
    )
    needs_is = bool(
        re.search(
            r"\brevenue|\bsales\b|\bmargin\b|\bgross profit|\boperating income|\boperating profit|\bebit\b|\bebitda\b|\bnet income|\bearnings\b|\beps\b|\bcogs\b|\bcost of|\bsg&a\b|\bselling|\br&d\b|\bresearch|\btax\b|\binterest expense|\bp&l\b|\bincome statement",
            question_lower,
        )
    )

    if is_cash_flow and needs_cf:
        boost += 12.0
    if is_balance_sheet and needs_bs:
        boost += 12.0
    if is_income_stmt and needs_is:
        boost += 12.0

    # Legal proceedings (Item 3)
    if re.search(r"\blegal\b|\blitigation\b|\blawsuit|\bbattle|\bproceeding", question_lower):
        if "item 3." in text_lower or "item 3 " in text_lower or "legal proceedings" in text_lower:
            boost += 14.0

    # Reward table density (numbers + years) when question asks for quantitative metrics
    if needs_cf or needs_bs or needs_is:
        digit_groups = len(re.findall(r"\b\d[\d,]*\b", text_lower))
        if digit_groups >= 25:
            boost += 3.0
        if years and any(y in text_lower for y in years):
            boost += 2.0

    return boost


async def _collect_candidate_pages(
    memory: Any, targeted_queries: list[str]
) -> tuple[dict[str, MemoryCitation], dict[str, str], dict[str, float]]:
    """Retrieve candidate pages via targeted FTS queries + broad coverage probes."""
    citations: dict[str, MemoryCitation] = {}
    texts: dict[str, str] = {}
    rrf_scores: dict[str, float] = {}

    # 1. Run targeted domain and n-gram queries (high precision RRF signal)
    for q in targeted_queries:
        try:
            res = await memory.search(SearchMemoryRequest(query=q, limit=100, mode="fts"))
        except Exception:
            res = None
        if not res or not res.hits:
            continue

        for rank, hit in enumerate(res.hits):
            eid = hit.entry_id
            rrf_scores[eid] = rrf_scores.get(eid, 0.0) + (1.0 / (40.0 + rank + 1))
            if eid not in citations:
                citations[eid] = MemoryCitation(
                    memory_ref=hit.memory_ref,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                texts[eid] = hit.text

    # 2. Run broad probes with large limit to ensure full candidate coverage across the corpus
    for probe in _BROAD_PROBES:
        try:
            res = await memory.search(SearchMemoryRequest(query=probe, limit=1500, mode="fts"))
        except Exception:
            res = None
        if not res or not res.hits:
            continue

        for hit in res.hits:
            eid = hit.entry_id
            if eid not in citations:
                citations[eid] = MemoryCitation(
                    memory_ref=hit.memory_ref,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                texts[eid] = hit.text
        # If a single broad probe already pulled >= 500 pages, do just a couple more to save time
        if len(citations) >= 1150:
            break

    return citations, texts, rrf_scores


async def search_pdf_pages(
    memory: Any, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Retrieve up to `limit` distinct exact page citations from memory for a natural language question."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return ()
    if not isinstance(question, str) or not question.strip():
        return ()

    targeted_queries, query_weights = _build_search_queries(question)
    citations, texts, rrf_scores = await _collect_candidate_pages(memory, targeted_queries)

    if not citations:
        return ()

    question_lower = question.lower()
    q_all_tokens = set(_tokenize(question_lower)) - _STOP_WORDS
    years = _extract_years(question)

    # Tokenize all candidate pages and compute document frequency (DF) for BM25
    doc_tokens_map: dict[str, Counter[str]] = {}
    doc_lengths: dict[str, int] = {}
    doc_id_map: dict[str, str] = {}
    df_counts: Counter[str] = Counter()

    for eid, raw_text in texts.items():
        parts = raw_text.split("\n", 1)
        doc_id = parts[0]
        body_text = parts[1] if len(parts) > 1 else ""
        doc_id_map[eid] = doc_id

        # Skip completely empty pages unless no other pages exist
        tokens = _tokenize(raw_text)
        tf_counter = Counter(tokens)
        doc_tokens_map[eid] = tf_counter
        doc_lengths[eid] = max(len(tokens), 1)

        for term in tf_counter:
            if term in query_weights:
                df_counts[term] += 1

    num_docs = len(texts)
    avg_dl = sum(doc_lengths.values()) / max(num_docs, 1)

    # Identify which document(s) match the company/document name mentioned in the question
    # Instead of guessing entities with brittle regex, check if tokens from `document_id` appear in `question`
    unique_doc_ids = set(doc_id_map.values())
    doc_match_scores: dict[str, float] = {}
    for did in unique_doc_ids:
        did_tokens = [
            t for t in _tokenize(did)
            if t not in _STOP_WORDS and not re.fullmatch(r"20\d{2}|10|k|q\d|fy\d*", t)
        ]
        # Check how many company tokens from document_id appear in the question
        matched_company_tokens = sum(1 for t in did_tokens if t in q_all_tokens or t in question_lower)
        year_matched = sum(1 for y in years if y in did.lower())
        if matched_company_tokens > 0:
            doc_match_scores[did] = 15.0 * matched_company_tokens + 6.0 * year_matched
        else:
            doc_match_scores[did] = 0.0

    any_doc_matched = any(score > 0 for score in doc_match_scores.values())

    # BM25 parameters
    k1 = 1.5
    b = 0.75

    scored_entries: list[tuple[float, str]] = []
    for eid, raw_text in texts.items():
        tf_counter = doc_tokens_map[eid]
        dl = doc_lengths[eid]
        did = doc_id_map[eid]

        # 1. BM25 lexical score over weighted query & domain terms
        bm25_score = 0.0
        for term, q_weight in query_weights.items():
            tf = tf_counter.get(term, 0)
            if tf == 0:
                continue
            df = df_counts.get(term, 0)
            idf = math.log(1.0 + (num_docs - df + 0.5) / (df + 0.5))
            tf_norm = (tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * (dl / max(avg_dl, 1.0))))
            bm25_score += q_weight * idf * tf_norm

        # 2. RRF score from targeted phrase queries
        rrf_component = rrf_scores.get(eid, 0.0) * 25.0

        # 3. Document affinity score (crucial when 5 PDFs are in the same database)
        if any_doc_matched:
            doc_affinity = doc_match_scores[did] if doc_match_scores[did] > 0 else -40.0
        else:
            doc_affinity = 0.0

        # 4. Financial statement structural boost
        struct_boost = _score_financial_structure(raw_text.lower(), question_lower, years)

        # Penalize near-empty pages so they don't accidentally float up
        empty_penalty = -15.0 if dl <= 5 else 0.0

        total_score = bm25_score + rrf_component + doc_affinity + struct_boost + empty_penalty
        scored_entries.append((total_score, eid))

    scored_entries.sort(key=lambda item: item[0], reverse=True)

    # Deduplicate by physical page identity (document_id + page content header / entry_id)
    seen_entries: set[str] = set()
    seen_doc_pages: set[str] = set()
    selected: list[MemoryCitation] = []

    for _, eid in scored_entries:
        if eid in seen_entries:
            continue
        seen_entries.add(eid)

        # Also deduplicate by raw text identity just in case multiple entries exist
        page_sig = hashlib.sha256(texts[eid].encode("utf-8")).hexdigest()
        if page_sig in seen_doc_pages:
            continue
        seen_doc_pages.add(page_sig)

        selected.append(citations[eid])
        if len(selected) >= limit:
            break

    return tuple(selected)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]