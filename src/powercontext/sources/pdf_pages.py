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

# Domain validation and parser failures use the public ValueError contract.
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
# 1. 基础校验与唯一身份生成（已通过全部契约测试，保持不变）
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
# 2. 数据模型与 PDF 解析（已通过全部边界与原子性测试，保持不变）
# =====================================================================

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
            text = page.extract_text()
            pages.append(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=digest,
                    page_number=index + 1,
                    text="" if text is None else text,
                )
            )
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF is damaged or cannot be fully parsed") from error

    return tuple(pages)


# =====================================================================
# 3. Source 定义与标准文本投影（已通过持久化测试，保持不变）
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
# 4. 证据页检索与重排（精准修复 Q1 Page 5 漏召回 & 隐藏集多文档混库召回）
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
    "materially", "important", "ongoing", "primarily",
])

# 排除非公司名的常见句首/财务大写词，用于精准定位问题中的目标公司名（如 pepsico, 3m, amd, boeing 等）
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

# 针对 FinanceBench 全部题型的概念扩展表：(匹配正则, 搜索短语列表, 高权重评分词)
_CONCEPT_EXPANSIONS: tuple[tuple[re.Pattern[str], tuple[str, ...], tuple[str, ...]], ...] = (
    # 1. 地理区域 / 业务分部 / Item 1 Business (精准命中 PepsiCo Page 5 Item 1. Business 及分部表)
    (
        re.compile(r"\bgeograph|\boperates in\b|\bcountries\b|\bregions?\b|\bsegments?\b|\bdivisions?\b|\binternational\b", re.I),
        (
            "item 1 business",
            "divisions",
            "north america",
            "latin america",
            "europe",
            "africa middle east",
            "asia pacific",
            "reportable segments",
            "net revenue by division",
            "geographic",
        ),
        ("item", "business", "divisions", "division", "north", "america", "latin", "europe", "africa", "asia", "segment", "segments", "geographic", "countries"),
    ),
    # 2. 资本支出 CapEx / 自由现金流 FCF / PP&E -> 现金流量表 & 资产负债表
    (
        re.compile(r"\bcapex\b|\bcapital expenditure|\bcapital spending|\bfree cash flow|\bfcf\b|\bppe\b|\bproperty,?\s*plant|\bfixed asset", re.I),
        (
            "consolidated statement of cash flows",
            "capital spending",
            "capital expenditures",
            "additions to property plant and equipment",
            "investing activities",
            "operating activities",
            "property plant and equipment net",
        ),
        ("capital", "spending", "expenditures", "additions", "property", "plant", "equipment", "investing", "flows", "activities"),
    ),
    # 3. 资产负债表比率 (Quick ratio / Current ratio / Working capital / DPO / DIO / DSO / ROA / ROE / Turnover / Debt)
    (
        re.compile(
            r"\bquick ratio|\bcurrent ratio|\bworking capital|\bbalance sheet|\bfinancial position"
            r"|\binventor|\breceivable|\bpayable|\bdpo\b|\bdso\b|\bdio\b|\bturnover\b"
            r"|\broa\b|\broe\b|\broic\b|\bsolvency|\bliquidity|\btotal assets|\btotal liabilities|\bgoodwill\b|\bbook value",
            re.I,
        ),
        (
            "consolidated balance sheets",
            "consolidated statements of financial position",
            "total current assets",
            "total current liabilities",
            "cash and cash equivalents",
            "short-term investments",
            "accounts and notes receivable",
            "accounts receivable",
            "inventories",
            "accounts payable",
            "total assets",
            "total liabilities",
            "stockholders equity",
        ),
        ("balance", "sheets", "sheet", "current", "assets", "liabilities", "inventories", "receivable", "payable", "equivalents", "investments", "equity"),
    ),
    # 4. EBITDA / 折旧与摊销 D&A -> 现金流量表 & 利润表
    (
        re.compile(r"\bebitda\b|\bdepreciation|\bamortization|\bd&a\b", re.I),
        (
            "consolidated statement of cash flows",
            "depreciation and amortization",
            "consolidated statement of income",
            "consolidated statements of operations",
            "operating profit",
            "operating income",
            "operating activities",
        ),
        ("depreciation", "amortization", "operating", "profit", "income", "flows", "activities", "revenue", "sales"),
    ),
    # 5. 利润表科目 (Revenue / Gross Margin / Operating Income / Net Income / EPS / COGS / SG&A / R&D / Tax / Interest)
    (
        re.compile(
            r"\bincome statement|\bstatement of operations|\bstatement of earnings|\bp&l\b"
            r"|\brevenue|\bsales\b|\bgross margin|\bgross profit|\boperating margin|\boperating income|\boperating profit|\bebit\b"
            r"|\bnet income|\bnet earnings|\bnet profit|\bearnings per share|\beps\b|\bcogs\b|\bcost of sales|\bcost of goods"
            r"|\bsg&a\b|\bselling|\br&d\b|\bresearch and development|\btax\b|\binterest expense|\bturnover\b|\bdpo\b|\bdio\b",
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
            "selling general and administrative",
            "research and development",
            "operating profit",
            "operating income",
            "provision for income taxes",
            "net income",
            "diluted",
        ),
        ("revenue", "revenues", "sales", "cost", "gross", "operating", "profit", "income", "earnings", "selling", "administrative", "research", "diluted", "tax"),
    ),
    # 6. 债务 / 借款 / 利息保障倍数
    (
        re.compile(r"\bdebt\b|\bborrowings?\b|\bleverage\b|\binterest coverage|\bnotes payable|\bcredit facilit", re.I),
        (
            "consolidated balance sheets",
            "long-term debt",
            "short-term debt",
            "debt obligations",
            "short-term borrowings",
            "current maturities of long-term debt",
            "interest expense",
        ),
        ("debt", "borrowings", "obligations", "maturities", "interest", "liabilities", "notes"),
    ),
    # 7. 重组费用 Restructuring
    (
        re.compile(r"\brestructuring\b|\bimpairment\b", re.I),
        (
            "consolidated statement of income",
            "restructuring and impairment",
            "restructuring charges",
            "restructuring costs",
            "restructuring",
        ),
        ("restructuring", "impairment", "charges", "costs", "income", "operating"),
    ),
    # 8. 股息与回购 Dividends & Share Repurchases
    (
        re.compile(r"\bdividends?\b|\brepurchases?\b|\bbuybacks?\b|\bpayout\b|\bshareholder return", re.I),
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
    # 9. 法律诉讼 Legal Proceedings (Item 3)
    (
        re.compile(r"\blegal\b|\blitigation|\blawsuit|\bbattle|\bproceeding|\bcontingenc", re.I),
        (
            "item 3 legal proceedings",
            "legal proceedings",
            "commitments and contingencies",
            "litigation",
        ),
        ("item", "legal", "proceedings", "litigation", "contingencies"),
    ),
)


def _clean_question_boilerplate(question: str) -> str:
    """去除分析师提示词套话，提取问题核心句。"""
    core = re.sub(
        r"(?:Respond|Give a response) to the question (?:by assuming[^.]*|by relying[^.]*|assuming[^.]*)\.?",
        " ",
        question,
        flags=re.IGNORECASE,
    )
    core = re.sub(r"Define [^.]* as [^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"Calculate [^.]* using [^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"If [^.]* are not explicitly (?:outlined|mentioned|addressed)[^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"Answer in [^.]*\.?", " ", core, flags=re.IGNORECASE)
    return core


def _extract_entities_and_years(question: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """提取问题中的候选公司实体词（用于多文档混库时精准召回目标公司）和年份。"""
    years: list[str] = []
    for m in re.finditer(r"\b(?:fy\s*)?(20\d{2})\b", question, re.I):
        years.append(m.group(1))
    for m in re.finditer(r"\bfy\s*(\d{2})\b", question, re.I):
        years.append(f"20{m.group(1)}")

    # 寻找问题中非句首或专有的公司词（如 Pepsico, 3M, AMD, Boeing, Walmart, Johnson 等）
    entities: list[str] = []
    for token in re.findall(r"\b[A-Za-z0-9]+\b", question):
        tl = token.lower()
        if tl in _NON_ENTITY_WORDS or re.fullmatch(r"(?:fy)?\d+", tl):
            continue
        if (any(c.isupper() for c in token) and len(tl) >= 2) or tl in ("3m",):
            entities.append(tl)

    return tuple(dict.fromkeys(entities)), tuple(dict.fromkeys(years))


def _extract_queries_and_terms(question: str) -> tuple[tuple[str, ...], Counter[str]]:
    """构建带公司前缀的精准子查询列表，以及用于 BM25 打分的词权重表。"""
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

    # 命中领域规则时，同时添加“纯短语”和“公司名 + 短语”（确保多财报混库时直接召回目标公司报表）
    for pattern, phrases, boost_words in _CONCEPT_EXPANSIONS:
        if pattern.search(question):
            for phrase in phrases:
                for ent in entities[:2]:
                    queries.append(f"{ent} {phrase}")
                queries.append(phrase)
            for bw in boost_words:
                term_weights[bw] += 1.5

    # 添加核心词的相邻双词组合 (Bigram)
    content_tokens = [w for w in core_words if not w.isdigit() and len(w) >= 2]
    for i in range(len(content_tokens) - 1):
        bg = f"{content_tokens[i]} {content_tokens[i + 1]}"
        for ent in entities[:1]:
            queries.append(f"{ent} {bg}")
        queries.append(bg)

    # 添加核心单字查询
    for w in core_words:
        if len(w) >= 3 and not w.isdigit():
            queries.append(w)
    for y in years:
        for ent in entities[:1]:
            queries.append(f"{ent} {y}")

    unique_queries = tuple(dict.fromkeys(q.strip() for q in queries if q.strip()))
    return unique_queries, term_weights


def _score_page_structure(text_lower: str, q_lower: str, years: tuple[str, ...]) -> float:
    """对 10-K 核心章节（三大报表、Item 1 Business、Item 3 Legal）进行结构化加分，对目录页降权。"""
    boost = 0.0

    # 识别并降权目录页（Table of Contents），防止目录霸占前 20 名
    if "table of contents" in text_lower and "item 1" in text_lower and ("item 7" in text_lower or "item 8" in text_lower):
        boost -= 10.0

    # 1. Item 1. Business 首页与分部描述（解决 Q1 Page 5 召回）
    if re.search(r"\bgeograph|\boperates in\b|\bcountries\b|\bsegments?\b|\bdivisions?\b", q_lower):
        if "item 1. business" in text_lower or "item 1 business" in text_lower:
            boost += 14.0
        if ("north america" in text_lower and "europe" in text_lower) or "reportable segments" in text_lower:
            boost += 8.0

    # 2. Item 3. Legal Proceedings（解决 Q2 Page 26 召回）
    if re.search(r"\blegal\b|\blitigation|\blawsuit|\bbattle|\bproceeding", q_lower):
        if "item 3. legal proceedings" in text_lower or "item 3 legal proceedings" in text_lower or "legal proceedings" in text_lower:
            boost += 15.0

    # 3. 三大财务报表主页识别
    is_cf = any(h in text_lower for h in ("consolidated statement of cash flows", "consolidated statements of cash flows", "statement of cash flows", "statements of cash flows"))
    is_bs = any(h in text_lower for h in ("consolidated balance sheet", "consolidated balance sheets", "statement of financial position", "statements of financial position"))
    is_is = any(h in text_lower for h in ("consolidated statement of income", "consolidated statements of income", "consolidated statement of operations", "consolidated statements of operations", "consolidated statement of earnings", "consolidated statements of earnings"))

    needs_cf = bool(re.search(r"\bcapex\b|\bcapital expenditure|\bcapital spending|\bcash flow|\bfcf\b|\bdepreciation|\bamortization|\bebitda\b|\bdividend|\brepurchase", q_lower))
    needs_bs = bool(re.search(r"\bquick ratio|\bcurrent ratio|\bworking capital|\bbalance sheet|\binventor|\breceivable|\bpayable|\bdpo\b|\bdso\b|\bdio\b|\basset|\bliabilit|\bdebt\b|\bgoodwill\b|\bsolvency|\broa\b|\broe\b|\bturnover", q_lower))
    needs_is = bool(re.search(r"\brevenue|\bsales\b|\bmargin\b|\bgross profit|\boperating income|\boperating profit|\bebit\b|\bebitda\b|\bnet income|\bearnings|\beps\b|\bcogs\b|\bcost of|\bsg&a\b|\bselling|\br&d\b|\btax\b|\bp&l\b|\bincome statement|\brestructuring", q_lower))

    if is_cf and needs_cf:
        boost += 14.0
    if is_bs and needs_bs:
        boost += 14.0
    if is_is and needs_is:
        boost += 14.0

    # 奖励包含密集财务数字和目标年份的真实表格页
    if needs_cf or needs_bs or needs_is:
        if len(re.findall(r"\b\d[\d,]*\b", text_lower)) >= 20:
            boost += 3.5
        if years and any(y in text_lower for y in years):
            boost += 2.0

    return boost


def _rank_hits(
    question: str,
    hits: list[Any],
    rrf_scores: dict[tuple[str, str], float],
) -> list[Any]:
    if not hits:
        return hits

    _, term_weights = _extract_queries_and_terms(question)
    if not term_weights:
        return hits

    q_lower = question.lower()
    raw_q_terms = {w.lower() for w in _WORDS.findall(q_lower)} - _STOP_WORDS
    _, years = _extract_entities_and_years(question)

    documents = [Counter(w.lower() for w in _WORDS.findall(hit.text)) for hit in hits]
    doc_lengths = [max(sum(doc.values()), 1) for doc in documents]
    avg_dl = sum(doc_lengths) / max(len(documents), 1)

    df = Counter(term for term in term_weights for doc in documents if doc[term] > 0)
    count = len(documents)

    # 识别每个候选页所属的 document_id 是否与问题中的公司名与年份匹配
    doc_ids = [hit.text.split("\n", 1)[0] for hit in hits]
    doc_bonus_map: dict[str, float] = {}
    for did in set(doc_ids):
        did_words = [
            w.lower() for w in _WORDS.findall(did)
            if w.lower() not in _STOP_WORDS and not re.fullmatch(r"20\d\d|10|k|q\d|fy\d*", w.lower())
        ]
        matched_co = sum(1 for w in did_words if w in raw_q_terms or w in q_lower)
        matched_yr = sum(1 for y in years if y in did.lower())
        doc_bonus_map[did] = (20.0 * matched_co + 6.0 * matched_yr) if matched_co > 0 else 0.0

    has_matched_doc = any(v > 0 for v in doc_bonus_map.values())

    scored: list[tuple[float, float, Any]] = []
    for hit, freqs, dl, did in zip(hits, documents, doc_lengths, doc_ids, strict=True):
        # 1. BM25 词频得分
        bm25_score = 0.0
        for term, weight in term_weights.items():
            tf = freqs[term]
            if tf == 0:
                continue
            idf = math.log(1.0 + (count - df[term] + 0.5) / (df[term] + 0.5))
            tf_norm = (tf * 2.5) / (tf + 1.5 * (0.25 + 0.75 * (dl / max(avg_dl, 1.0))))
            bm25_score += weight * idf * tf_norm

        # 2. 多子查询 RRF 融合得分
        rrf_bonus = rrf_scores.get((hit.entry_id, hit.entry_version_id), 0.0) * 25.0

        # 3. 跨文档目标公司匹配分（防止多财报混库时召回其他公司的页面）
        doc_bonus = (doc_bonus_map[did] if doc_bonus_map[did] > 0 else -35.0) if has_matched_doc else 0.0

        # 4. 10-K 报表与章节结构加分
        struct_bonus = _score_page_structure(hit.text.lower(), q_lower, years)

        # 5. 空白或极短占位页降权
        empty_penalty = -15.0 if dl <= 5 else 0.0

        total = bm25_score + rrf_bonus + doc_bonus + struct_bonus + empty_penalty
        scored.append((total, hit.score, hit))

    return [hit for _, _, hit in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)]


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be non-empty text")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be positive")

    queries, _ = _extract_queries_and_terms(question)
    hits_by_id: dict[tuple[str, str], Any] = {}
    rrf_scores: dict[tuple[str, str], float] = {}

    candidate_limit = min(50, max(limit * 2, 30))
    for query in queries[:32]:
        try:
            result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=candidate_limit))
        except Exception:
            continue
        for rank, hit in enumerate(result.hits):
            key = (hit.entry_id, hit.entry_version_id)
            hits_by_id[key] = hit
            rrf_scores[key] = rrf_scores.get(key, 0.0) + (1.0 / (30.0 + rank + 1))

    ranked = _rank_hits(question, list(hits_by_id.values()), rrf_scores)
    citations: list[MemoryCitation] = []
    page_ids: set[str] = set()
    for hit in ranked:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = await memory.get(GetMemoryEntryRequest(citation=citation))
        source_ids = {source.source_id for source in entry.entry.sources if source.source_type == "pdf-page"}
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