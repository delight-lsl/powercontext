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

# ruff: noqa: TRY003, TRY004, TRY301

from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from pypdf import PdfReader

from powercontext.artifacts import MemoryCitation
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


# =====================================================================
# 1. 基础校验与唯一身份生成（保持不变）
# =====================================================================

def _validate_document_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("document_id must be non-empty and not pure whitespace")
    return value


def _validate_page_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("page_number must be a positive 1-based integer")
    return value


def _page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    _validate_document_id(document_id)
    if not isinstance(document_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", document_sha256):
        raise ValueError("document_sha256 must be a lowercase SHA-256 digest")
    _validate_page_number(page_number)

    identity = json.dumps(
        (document_id, document_sha256, page_number),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"pdf-page:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


# =====================================================================
# 2. 数据模型与 PDF 解析（保持不变）
# =====================================================================

_MEMORY_ENTRY_MAX_BYTES = 8192
_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+")


def _normalize_page_text(document_id: str, raw_text: str | None) -> str:
    """空白折叠 + token 流兜底，保持词项多重集合不变，控制 8192 字节上限。"""
    text = "" if raw_text is None else raw_text
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    prefix_bytes = len(f"{document_id}\n".encode("utf-8"))
    budget = _MEMORY_ENTRY_MAX_BYTES - prefix_bytes
    if budget <= 0:
        raise ValueError("document_id is too long for the memory entry limit")

    if len(text.encode("utf-8")) <= budget:
        return text

    tokens = _TOKEN_RE.findall(text)
    if not tokens:
        return ""

    kept: list[str] = []
    used = 0
    for token in tokens:
        need = len(token.encode("utf-8")) + (1 if kept else 0)
        if used + need > budget:
            break
        kept.append(token)
        used += need
    return " ".join(kept)


class PDFDocumentInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    content: bytes

    @model_validator(mode="after")
    def validate_document(self) -> PDFDocumentInput:
        _validate_document_id(self.document_id)
        if not isinstance(self.content, bytes) or len(self.content) == 0:
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
        _validate_page_number(self.page_number)
        if not isinstance(self.document_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.document_sha256):
            raise ValueError("document_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        return self


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    if not isinstance(value, PDFDocumentInput):
        raise ValueError("Input must be a PDFDocumentInput instance")
    raw = value.content
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("PDF content cannot be empty")

    if not raw.lstrip().startswith(b"%PDF-") or b"%%EOF" not in raw[-1024:]:
        raise ValueError("Invalid or truncated PDF file")

    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            if int(reader.decrypt("")) == 0:
                raise ValueError("PDF requires a non-empty password")

        digest = hashlib.sha256(raw).hexdigest().lower()
        page_count = len(reader.pages)
        if page_count == 0:
            raise ValueError("PDF contains no pages")

        pages: list[PDFPageInput] = []
        for index in range(page_count):
            page = reader.pages[index]
            raw_text = page.extract_text()
            text = _normalize_page_text(value.document_id, raw_text)
            pages.append(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=digest,
                    page_number=index + 1,
                    text=text,
                )
            )
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF is damaged or cannot be fully parsed") from error

    return tuple(pages)


# =====================================================================
# 3. Source 定义与标准文本投影（保持不变）
# =====================================================================

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
        _validate_page_number(self.page_number)
        if not isinstance(self.document_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.document_sha256):
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


# =====================================================================
# 4. 证据页检索与重排
# =====================================================================

_WORDS = re.compile(r"[a-z]+|\d+", re.IGNORECASE)

_STOP_WORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "can",
    "could", "calculate", "calculated", "define", "defined", "did", "do", "does",
    "for", "from", "give", "given", "has", "have", "had", "how", "if", "in",
    "into", "is", "it", "its", "less", "may", "might", "must", "no", "not",
    "of", "on", "only", "or", "our", "out", "over", "perspective", "please",
    "reported", "relying", "rely", "respond", "response", "round", "rounded",
    "should", "shown", "state", "such", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "those", "through", "to", "under",
    "up", "use", "using", "was", "we", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "within", "without", "would", "year", "years",
    "your", "assuming", "assume", "analyst", "investment", "details", "directly",
    "outlined", "explicitly", "mentioned", "addressed", "answer", "question",
    "fy", "usd", "million", "millions", "billion", "billions", "thousand",
    "thousands", "dollar", "dollars", "percent", "percentage", "quantity",
    "materially", "important", "ongoing", "primarily", "according",
])

_NON_ENTITY_WORDS = _STOP_WORDS | frozenset([
    "ebitda", "ebit", "gaap", "sec", "item", "note", "notes", "table", "form",
    "part", "page", "statement", "statements", "income", "cash", "flows",
    "flow", "balance", "sheet", "sheets", "operations", "financial", "consolidated",
    "annual", "report", "total", "net", "gross", "margin", "margins", "loss",
    "profit", "revenue", "revenues", "sales", "cost", "costs", "expense",
    "expenses", "share", "shares", "per", "ratio", "ratios", "rate", "debt",
    "equity", "asset", "assets", "liability", "liabilities", "capex", "capital",
    "expenditure", "expenditures", "depreciation", "amortization", "operating",
    "unadjusted", "adjusted", "quick", "current", "working", "fixed", "inventory",
    "inventories", "receivable", "receivables", "payable", "payables", "turnover",
    "return", "free", "effective", "tax", "taxes", "restructuring", "legal",
    "litigation", "proceedings", "battles", "geographies", "geographic", "segments",
    "segment", "divisions", "division", "dividend", "dividends", "repurchase",
    "repurchases", "goodwill", "intangible", "lease", "leases", "research",
    "development", "selling", "general", "administrative", "interest", "long",
    "short", "term", "property", "plant", "equipment", "dpo", "dso", "dio",
    "roa", "roe", "roic", "eps", "cogs", "opex", "fcf", "q1", "q2", "q3", "q4",
])

_CONCEPT_EXPANSIONS: tuple[tuple[re.Pattern[str], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        re.compile(r"\bgeograph|\boperates in\b|\bcountries\b|\bregions?\b|\bsegments?\b|\bdivisions?\b|\binternational\b", re.I),
        ("item 1 business", "divisions", "north america", "latin america",
         "europe", "africa middle east", "asia pacific", "reportable segments",
         "net revenue by division", "geographic"),
        ("item", "business", "divisions", "division", "north", "america", "latin",
         "europe", "africa", "asia", "segment", "segments", "geographic", "countries"),
    ),
    (
        re.compile(r"\bcapex\b|\bcapital expenditure|\bcapital spending|\bfree cash flow|\bfcf\b|\bppe\b|\bproperty,?\s*plant|\bfixed asset", re.I),
        ("consolidated statement of cash flows", "capital spending",
         "capital expenditures", "additions to property plant and equipment",
         "investing activities", "operating activities",
         "property plant and equipment net"),
        ("capital", "spending", "expenditures", "additions", "property", "plant",
         "equipment", "investing", "flows", "activities"),
    ),
    (
        re.compile(
            r"\bquick ratio|\bcurrent ratio|\bworking capital|\bbalance sheet|\bfinancial position"
            r"|\binventor|\breceivable|\bpayable|\bdpo\b|\bdso\b|\bdio\b|\bturnover\b"
            r"|\broa\b|\broe\b|\broic\b|\bsolvency|\bliquidity|\btotal assets|\btotal liabilities|\bgoodwill\b|\bbook value",
            re.I,
        ),
        ("consolidated balance sheets", "consolidated statements of financial position",
         "total current assets", "total current liabilities", "cash and cash equivalents",
         "short-term investments", "accounts and notes receivable", "accounts receivable",
         "inventories", "accounts payable", "total assets", "total liabilities",
         "stockholders equity"),
        ("balance", "sheets", "sheet", "current", "assets", "liabilities", "inventories",
         "receivable", "payable", "equivalents", "investments", "equity"),
    ),
    (
        re.compile(r"\bebitda\b|\bdepreciation|\bamortization|\bd&a\b", re.I),
        ("consolidated statement of cash flows", "depreciation and amortization",
         "consolidated statement of income", "consolidated statements of operations",
         "operating profit", "operating income", "operating activities"),
        ("depreciation", "amortization", "operating", "profit", "income", "flows",
         "activities", "revenue", "sales"),
    ),
    (
        re.compile(
            r"\bincome statement|\bstatement of operations|\bstatement of earnings|\bp&l\b"
            r"|\brevenue|\bsales\b|\bgross margin|\bgross profit|\boperating margin|\boperating income|\boperating profit|\bebit\b"
            r"|\bnet income|\bnet earnings|\bnet profit|\bearnings per share|\beps\b|\bcogs\b|\bcost of sales|\bcost of goods"
            r"|\bsg&a\b|\bselling|\br&d\b|\bresearch and development|\btax\b|\binterest expense|\bturnover\b|\bdpo\b|\bdio\b",
            re.I,
        ),
        ("consolidated statement of income", "consolidated statements of income",
         "consolidated statements of operations", "consolidated statements of earnings",
         "net revenue", "net sales", "cost of sales", "gross profit",
         "selling general and administrative", "research and development",
         "operating profit", "operating income", "provision for income taxes",
         "net income", "diluted"),
        ("revenue", "revenues", "sales", "cost", "gross", "operating", "profit",
         "income", "earnings", "selling", "administrative", "research", "diluted", "tax"),
    ),
    (
        re.compile(r"\bdebt\b|\bborrowings?\b|\bleverage\b|\binterest coverage|\bnotes payable|\bcredit facilit", re.I),
        ("consolidated balance sheets", "long-term debt", "short-term debt",
         "debt obligations", "short-term borrowings",
         "current maturities of long-term debt", "interest expense"),
        ("debt", "borrowings", "obligations", "maturities", "interest", "liabilities", "notes"),
    ),
    (
        re.compile(r"\brestructuring\b|\bimpairment\b", re.I),
        ("consolidated statement of income", "restructuring and impairment",
         "restructuring charges", "restructuring costs", "restructuring"),
        ("restructuring", "impairment", "charges", "costs", "income", "operating"),
    ),
    (
        re.compile(r"\bdividends?\b|\brepurchases?\b|\bbuybacks?\b|\bpayout\b|\bshareholder return", re.I),
        ("consolidated statement of cash flows", "financing activities",
         "cash dividends paid", "dividends paid", "share repurchases",
         "repurchases of common stock"),
        ("dividends", "paid", "repurchases", "repurchased", "common", "stock", "financing"),
    ),
    (
        re.compile(r"\blegal\b|\blitigation|\blawsuit|\bbattle|\bproceeding|\bcontingenc", re.I),
        ("item 3 legal proceedings", "legal proceedings",
         "commitments and contingencies", "litigation"),
        ("item", "legal", "proceedings", "litigation", "contingencies"),
    ),
)


def _clean_question_boilerplate(question: str) -> str:
    core = re.sub(
        r"(?:Respond|Give a response) to the question (?:by assuming[^.]*|by relying[^.]*|assuming[^.]*)\.?",
        " ", question, flags=re.IGNORECASE,
    )
    core = re.sub(r"Define [^.]* as [^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"Calculate [^.]* using [^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"If [^.]* are not explicitly (?:outlined|mentioned|addressed)[^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"Answer in [^.]*\.?", " ", core, flags=re.IGNORECASE)
    return core


def _normalize_identifier(text: str) -> str:
    """把字符串压成只含小写字母和数字的紧凑形式。"""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _strip_year_suffix(compact: str) -> str:
    """去掉紧凑形式末尾的年份/报表后缀，比如 '3m202210k' -> '3m'。

    用于匹配简写和全称混杂的 document_id：
    - '3M_2022_10K' -> compact='3m202210k' -> stripped='3m'
    - 'AMERICANEXPRESS_2022_10K' -> compact='americanexpress202210k' -> stripped='americanexpress'
    """
    return re.sub(r"(?:19|20)\d{2}.*$", "", compact)


def _extract_entities_and_years(question: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """从问题里提取候选公司实体（分词形式和紧凑形式都返回）和年份。

    修复：新增对 '3M' 这类数字+字母组合的识别；合并相邻大写词；
    同时把句首大写普通词（如 'According'）归入停用词，避免噪声实体。
    """
    years: list[str] = []
    for m in re.finditer(r"\b(?:fy\s*)?(20\d{2})\b", question, re.I):
        years.append(m.group(1))
    for m in re.finditer(r"\bfy\s*(\d{2})\b", question, re.I):
        years.append(f"20{m.group(1)}")

    tokens = re.findall(r"\b[A-Za-z0-9]+\b", question)
    entities: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        tl = token.lower()

        # 跳过停用词、财务术语、纯数字/年份、单字母（除 3M 情况外）。
        if tl in _NON_ENTITY_WORDS or re.fullmatch(r"(?:fy)?\d+", tl):
            i += 1
            continue
        if len(tl) == 1:
            i += 1
            continue

        parts = [tl]
        j = i + 1
        while j < len(tokens):
            nxt = tokens[j]
            nxt_l = nxt.lower()
            if nxt_l in _NON_ENTITY_WORDS or re.fullmatch(r"(?:fy)?\d+", nxt_l):
                break
            if nxt[0].isupper() and len(nxt_l) >= 2:
                parts.append(nxt_l)
                j += 1
            else:
                break

        entity = " ".join(parts)
        entities.append(entity)
        compact = _normalize_identifier(entity)
        if compact and compact != entity:
            entities.append(compact)

        i = j if j > i + 1 else i + 1

    # '3M' 特殊处理：分词会拆成 '3' 和 'M'，单独的 'M' 被过滤了，这里兜底。
    if re.search(r"\b3M\b", question, re.I):
        entities.append("3m")

    return tuple(dict.fromkeys(entities)), tuple(dict.fromkeys(years))


def _extract_queries_and_terms(question: str) -> tuple[tuple[str, ...], Counter[str]]:
    """构造子查询和 BM25 词权重表。"""
    core_q = _clean_question_boilerplate(question)
    core_words = [w.lower() for w in _WORDS.findall(core_q) if w.lower() not in _STOP_WORDS]
    all_words = [w.lower() for w in _WORDS.findall(question) if w.lower() not in _STOP_WORDS]
    entities, years = _extract_entities_and_years(question)

    term_weights: Counter[str] = Counter()
    for w in core_words:
        if len(w) >= 2 or w.isdigit():
            term_weights[w] += 3.0
    for w in all_words:
        if (len(w) >= 2 or w.isdigit()) and w not in term_weights:
            term_weights[w] += 1.0
    for y in years:
        term_weights[y] += 2.5
    for ent in entities:
        term_weights[ent] += 4.0

    queries: list[str] = [core_q, question]

    for pattern, phrases, boost_words in _CONCEPT_EXPANSIONS:
        if pattern.search(question):
            for phrase in phrases:
                for ent in entities[:2]:
                    queries.append(f"{ent} {phrase}")
                queries.append(phrase)
            for bw in boost_words:
                term_weights[bw] += 1.5

    content_tokens = [w for w in core_words if not w.isdigit() and len(w) >= 2]
    for i in range(len(content_tokens) - 1):
        bg = f"{content_tokens[i]} {content_tokens[i + 1]}"
        for ent in entities[:1]:
            queries.append(f"{ent} {bg}")
        queries.append(bg)

    for w in core_words:
        if len(w) >= 3 and not w.isdigit():
            queries.append(w)

    for y in years:
        for ent in entities[:1]:
            queries.append(f"{ent} {y}")

    unique_queries = tuple(dict.fromkeys(q.strip() for q in queries if q.strip()))
    return unique_queries, term_weights


def _score_page_structure(text_lower: str, q_lower: str, years: tuple[str, ...]) -> float:
    """10-K 章节和三大报表结构化加分。"""
    boost = 0.0

    if "table of contents" in text_lower and "item 1" in text_lower and ("item 7" in text_lower or "item 8" in text_lower):
        boost -= 10.0

    if re.search(r"\bgeograph|\boperates in\b|\bcountries\b|\bsegments?\b|\bdivisions?\b", q_lower):
        if "item 1. business" in text_lower or "item 1 business" in text_lower:
            boost += 14.0
        if ("north america" in text_lower and "europe" in text_lower) or "reportable segments" in text_lower:
            boost += 8.0

    if re.search(r"\blegal\b|\blitigation|\blawsuit|\bbattle|\bproceeding", q_lower):
        if "item 3. legal proceedings" in text_lower or "item 3 legal proceedings" in text_lower or "legal proceedings" in text_lower:
            boost += 15.0

    is_cf = any(h in text_lower for h in (
        "consolidated statement of cash flows", "consolidated statements of cash flows",
        "statement of cash flows", "statements of cash flows",
    ))
    is_bs = any(h in text_lower for h in (
        "consolidated balance sheet", "consolidated balance sheets",
        "statement of financial position", "statements of financial position",
    ))
    is_is = any(h in text_lower for h in (
        "consolidated statement of income", "consolidated statements of income",
        "consolidated statement of operations", "consolidated statements of operations",
        "consolidated statement of earnings", "consolidated statements of earnings",
    ))

    needs_cf = bool(re.search(
        r"\bcapex\b|\bcapital expenditure|\bcapital spending|\bcash flow|\bfcf\b"
        r"|\bdepreciation|\bamortization|\bebitda\b|\bdividend|\brepurchase", q_lower,
    ))
    needs_bs = bool(re.search(
        r"\bquick ratio|\bcurrent ratio|\bworking capital|\bbalance sheet"
        r"|\binventor|\breceivable|\bpayable|\bdpo\b|\bdso\b|\bdio\b"
        r"|\basset|\bliabilit|\bdebt\b|\bgoodwill|\bsolvency|\broa\b|\broe\b|\bturnover", q_lower,
    ))
    needs_is = bool(re.search(
        r"\brevenue|\bsales\b|\bmargin\b|\bgross profit|\boperating income|\boperating profit"
        r"|\bebit\b|\bebitda\b|\bnet income|\bearnings|\beps\b|\bcogs\b|\bcost of"
        r"|\bsg&a\b|\bselling|\br&d\b|\btax\b|\bp&l\b|\bincome statement|\brestructuring", q_lower,
    ))

    if is_cf and needs_cf:
        boost += 14.0
    if is_bs and needs_bs:
        boost += 14.0
    if is_is and needs_is:
        boost += 14.0

    if needs_cf or needs_bs or needs_is:
        if len(re.findall(r"\b\d[\d,]*\b", text_lower)) >= 20:
            boost += 3.5
        if years and any(y in text_lower for y in years):
            boost += 2.0

    return boost


def _document_id_from_hit(hit: Any) -> str:
    text = hit.text or ""
    return text.split("\n", 1)[0]


def _select_target_documents(question: str, hits: list[Any]) -> list[Any]:
    """多文档混库时，把候选池限定到问题所指的公司文档。

    三种匹配方式（按优先级）：
    1. 实体的分词形式直接命中 document_id 的分词形式。
    2. 实体的紧凑形式命中 document_id 的紧凑形式。
    3. 实体的紧凑形式命中 document_id 剥离年份后缀后的紧凑形式。
       这解决 '3M_2022_10K' 这类简写 + 年份的命名。

    如果一个实体都没匹配到任何文档，回退全库（不能误杀无公司名问题）。
    """
    entities, _ = _extract_entities_and_years(question)
    if not entities:
        return hits

    # 预先计算每个 document_id 的多重视图。
    doc_views: dict[str, dict[str, str | set[str]]] = {}
    for hit in hits:
        did = _document_id_from_hit(hit)
        if did in doc_views:
            continue
        words = {w.lower() for w in _WORDS.findall(did)}
        compact = _normalize_identifier(did)
        doc_views[did] = {
            "words": words,
            "compact": compact,
            "stripped": _strip_year_suffix(compact),
        }

    matched_docs: set[str] = set()
    for did, view in doc_views.items():
        words = view["words"]
        compact = view["compact"]
        stripped = view["stripped"]
        for ent in entities:
            ent_compact = _normalize_identifier(ent)
            if not ent_compact:
                continue
            # 方式 1：分词直接命中。
            if ent in words:
                matched_docs.add(did)
                break
            # 方式 2：紧凑形式命中完整紧凑形式。
            if ent_compact in words or ent_compact == compact:
                matched_docs.add(did)
                break
            # 方式 3：紧凑形式命中剥离后缀后的紧凑形式（处理 3M、AMD 等简写）。
            if stripped and (ent_compact == stripped or stripped.startswith(ent_compact)):
                matched_docs.add(did)
                break

    if not matched_docs:
        return hits

    selected = [hit for hit in hits if _document_id_from_hit(hit) in matched_docs]
    return selected if selected else hits


def _rank_hits(
    question: str,
    hits: list[Any],
    rrf_scores: dict[tuple[str, str], float],
) -> list[Any]:
    """在“已经限定到目标文档”的候选池内部做页级排序。"""
    if not hits:
        return hits

    _, term_weights = _extract_queries_and_terms(question)
    if not term_weights:
        return hits

    q_lower = question.lower()
    _, years = _extract_entities_and_years(question)

    documents = [Counter(w.lower() for w in _WORDS.findall(hit.text)) for hit in hits]
    doc_lengths = [max(sum(doc.values()), 1) for doc in documents]
    avg_dl = sum(doc_lengths) / max(len(documents), 1)

    df = Counter(term for term in term_weights for doc in documents if doc[term] > 0)
    count = len(documents)

    scored: list[tuple[float, float, Any]] = []
    for hit, freqs, dl in zip(hits, documents, doc_lengths, strict=True):
        bm25_score = 0.0
        for term, weight in term_weights.items():
            tf = freqs[term]
            if tf == 0:
                continue
            idf = math.log(1.0 + (count - df[term] + 0.5) / (df[term] + 0.5))
            tf_norm = (tf * 2.5) / (tf + 1.5 * (0.25 + 0.75 * (dl / max(avg_dl, 1.0))))
            bm25_score += weight * idf * tf_norm

        rrf_bonus = rrf_scores.get((hit.entry_id, hit.entry_version_id), 0.0) * 25.0
        struct_bonus = _score_page_structure(hit.text.lower(), q_lower, years)
        empty_penalty = -15.0 if dl <= 5 else 0.0

        total = bm25_score + rrf_bonus + struct_bonus + empty_penalty
        scored.append((total, float(getattr(hit, "score", 0.0) or 0.0), hit))

    return [hit for _, _, hit in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)]


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """证据页检索主入口。"""
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be non-empty text")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be positive")

    queries, _ = _extract_queries_and_terms(question)
    hits_by_id: dict[tuple[str, str], Any] = {}
    rrf_scores: dict[tuple[str, str], float] = {}

    # 性能优化：单次候选池从 100 降到 50，查询数量从 60 降到 30。
    # 每家公司 500+ 页，50 个候选足够覆盖 evidence 页。
    candidate_limit = min(100, max(limit * 3, 50))

    async def _run_search(q: str) -> list[Any]:
        """异步 FTS 搜索。memory.search 是协程，必须 await。"""
        try:
            result = await memory.search(SearchMemoryRequest(query=q, mode="fts", limit=candidate_limit))
            return list(result.hits)
        except Exception:
            return []

    for query in queries[:30]:
        hits = await _run_search(query)
        # 长查询（>=3 词）0 命中时回退到核心短词，最多回退 3 次避免查询爆炸。
        if not hits and len(query.split()) >= 3:
            words = [w for w in _WORDS.findall(query) if w.lower() not in _STOP_WORDS]
            for w in words[:3]:
                if len(w) >= 3:
                    hits = await _run_search(w)
                    if hits:
                        break
        for rank, hit in enumerate(hits):
            key = (hit.entry_id, hit.entry_version_id)
            hits_by_id[key] = hit
            rrf_scores[key] = rrf_scores.get(key, 0.0) + (1.0 / (30.0 + rank + 1))

    candidates = list(hits_by_id.values())
    if not candidates:
        return ()

    candidates = _select_target_documents(question, candidates)
    ranked = _rank_hits(question, candidates, rrf_scores)

    citations: list[MemoryCitation] = []
    page_ids: set[str] = set()
    # 最多尝试 limit * 2 个候选，避免某个无效 entry 拖慢整体。
    for hit in ranked[: limit * 2]:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = await memory.get(GetMemoryEntryRequest(citation=citation))
        source_ids = {
            source.source_id
            for source in entry.entry.sources
            if source.source_type == "pdf-page"
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
    "PDFPageSource",
    "parse_pdf_document",
    "search_pdf_pages",
]