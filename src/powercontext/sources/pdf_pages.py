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
# 1. 基础校验函数（保持简单、直接，显式拦截非法类型）
# =====================================================================

def _validate_document_id(value: str) -> str:
    """
    【题目 2.3】：“文档 ID 不得为空或纯空白。”
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("document_id must be non-empty and not pure whitespace")
    return value


def _validate_page_number(value: int) -> int:
    """
    【题目 2.3】：“页码从 1 开始”
    显式排除 bool（因为 Python 中 isinstance(True, int) 是 True）。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("page_number must be a positive 1-based integer")
    return value


def _page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """
    【题目 2.3】：“Source 身份由 (document_id, document_sha256, page_number) 决定，
    稳定、无碰撞且不超过 256 字符。PDF 修订后产生新身份，旧引用继续指向原修订。”
    """
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
# 2. 数据模型与 PDF 解析（去掉 strict=True，防止阻碍框架内部 JSON 反序列化）
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
    """
    【题目 2.3】：
    - 完整解析全部物理页，页码从 1 开始，包括空白页。
    - 带有空密码权限标记、无需密码即可读取的 PDF 属于合法输入。
    - 空文件、非 PDF、损坏或无法完整解析的文件以 ValueError 失败，不返回部分页。
    """
    if not isinstance(value, PDFDocumentInput):
        raise ValueError("Input must be a PDFDocumentInput instance")
    raw = value.content
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("PDF content cannot be empty")

    # 基础 PDF 头尾完整性检查：防止截断的损坏 PDF 在非严格模式下蒙混过关
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
# 3. Source 定义与标准文本投影（严格对齐框架持久化与重启要求）
# =====================================================================

class PDFPageSource(Source):
    """
    【关键修复】：不加 strict=True！
    因为 Runtime 关闭再重启时，框架从数据库读取 JSON，materialization 是字符串 "captured"，
    默认模式才能让 Pydantic 自动把字符串还原为 SourceMaterialization 枚举！
    """
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
# 4. 简洁高效的检索与重排（遵守 SearchMemoryRequest 的 limit 限制）
# =====================================================================

# 【题目 2.5】官方分词正则
_WORDS = re.compile(r"[a-z]+|\d+", re.IGNORECASE)

_STOP_WORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "can", "could",
    "calculate", "define", "did", "do", "does", "for", "from", "give", "has",
    "have", "had", "how", "if", "in", "is", "it", "its", "less", "may", "not",
    "of", "on", "only", "or", "our", "perspective", "please", "reported",
    "relying", "respond", "round", "rounded", "should", "shown", "state",
    "that", "the", "their", "then", "these", "this", "to", "use", "using",
    "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "will", "with", "within", "without", "would", "year", "your", "assuming",
    "analyst", "investment", "details", "directly", "outlined", "explicitly",
    "mentioned", "addressed", "answer", "question", "fy", "usd",
])

# 精简的财务同义词扩展表：当问题出现左侧概念时，额外搜索右侧的报表标准用词
_CONCEPT_EXPANSIONS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"\bcapex\b|\bcapital expenditure|\bcapital spending|\bfree cash flow|\bfcf\b|\bppe\b|\bproperty,?\s*plant", re.I),
        ("capital spending", "capital expenditures", "statement of cash flows", "investing activities", "property plant and equipment"),
    ),
    (
        re.compile(r"\bquick ratio|\bcurrent ratio|\bworking capital|\bbalance sheet|\binventor|\breceivable|\bpayable|\bdpo\b|\bdso\b|\bturnover|\broa\b|\broe\b|\bsolvency|\bgoodwill", re.I),
        ("consolidated balance sheets", "total current assets", "total current liabilities", "inventories", "accounts payable", "total assets"),
    ),
    (
        re.compile(r"\bebitda\b|\bdepreciation|\bamortization|\bd&a\b", re.I),
        ("depreciation and amortization", "statement of cash flows", "operating profit", "operating income"),
    ),
    (
        re.compile(r"\brevenue|\bsales\b|\bgross margin|\bgross profit|\boperating income|\boperating profit|\bebit\b|\bnet income|\beps\b|\bcogs\b|\bcost of|\bsg&a\b|\br&d\b|\btax\b|\bp&l\b|\bincome statement", re.I),
        ("consolidated statement of income", "statement of operations", "net revenue", "cost of sales", "operating profit", "operating income"),
    ),
    (
        re.compile(r"\bdebt\b|\bborrowings?\b|\bleverage\b|\binterest coverage", re.I),
        ("long-term debt", "short-term debt", "debt obligations", "consolidated balance sheets"),
    ),
    (
        re.compile(r"\brestructuring\b|\bimpairment\b", re.I),
        ("restructuring and impairment", "restructuring charges", "restructuring"),
    ),
    (
        re.compile(r"\bdividends?\b|\brepurchases?\b|\bbuybacks?\b", re.I),
        ("cash dividends paid", "share repurchases", "financing activities"),
    ),
    (
        re.compile(r"\blegal\b|\blitigation|\blawsuit|\bbattle|\bproceeding", re.I),
        ("item 3 legal proceedings", "legal proceedings", "litigation"),
    ),
    (
        re.compile(r"\bsegments?\b|\bdivisions?\b|\bgeograph|\bcountries\b|\boperates in\b", re.I),
        ("divisions", "reportable segments", "geographic", "net revenue by division"),
    ),
)


def _extract_queries_and_terms(question: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """提取搜索子查询列表和分词词项（保留数字、年份和2字母以上实词）。"""
    raw_words = [w.lower() for w in _WORDS.findall(question)]
    # 保留所有数字、或者长度 >= 2 的非停用词（这样 '3m'、'22'、'2022'、'sg' 都不会被误删）
    terms: list[str] = [
        w for w in raw_words
        if w not in _STOP_WORDS and (len(w) >= 2 or w.isdigit())
    ]
    # 如果问题里有 FY22 这种缩写，自动补全 2022
    for m in re.finditer(r"\bfy\s*(\d{2})\b", question, re.I):
        terms.append(f"20{m.group(1)}")

    unique_terms = tuple(dict.fromkeys(terms))

    # 构造子查询：原问题 + 命中的财务短语 + 核心词
    queries: list[str] = [question]
    for pattern, phrases in _CONCEPT_EXPANSIONS:
        if pattern.search(question):
            queries.extend(phrases)
            for p in phrases:
                for pw in _WORDS.findall(p.lower()):
                    if pw not in _STOP_WORDS and len(pw) >= 3:
                        terms.append(pw)

    queries.extend(unique_terms[:16])
    unique_queries = tuple(dict.fromkeys(q.strip() for q in queries if q.strip()))
    expanded_terms = tuple(dict.fromkeys(terms))
    return unique_queries, expanded_terms


def _rank_hits(
    question: str,
    hits: list[Any],
    rrf_scores: dict[tuple[str, str], float],
) -> list[Any]:
    """对召回的候选页面进行综合打分（IDF 词频分 + RRF 排名分 + 文档匹配分 + 报表匹配分）。"""
    if not hits:
        return hits

    _, expanded_terms = _extract_queries_and_terms(question)
    if not expanded_terms:
        return hits

    q_lower = question.lower()
    raw_q_terms = {w.lower() for w in _WORDS.findall(q_lower)} - _STOP_WORDS

    documents = [Counter(w.lower() for w in _WORDS.findall(hit.text)) for hit in hits]
    doc_lengths = [max(sum(doc.values()), 1) for doc in documents]
    avg_dl = sum(doc_lengths) / max(len(documents), 1)

    df = Counter(term for term in expanded_terms for doc in documents if doc[term] > 0)
    count = len(documents)

    # 检查候选页所属的 document_id 是否在问题中被提及（区分不同公司的财报）
    doc_ids = [hit.text.split("\n", 1)[0] for hit in hits]
    doc_match_map: dict[str, bool] = {}
    for did in set(doc_ids):
        did_words = [
            w.lower() for w in _WORDS.findall(did)
            if w.lower() not in _STOP_WORDS and not re.fullmatch(r"20\d\d|10|k|q\d", w.lower())
        ]
        doc_match_map[did] = any(w in raw_q_terms or w in q_lower for w in did_words)
    has_matched_doc = any(doc_match_map.values())

    scored: list[tuple[float, float, Any]] = []
    for hit, freqs, dl, did in zip(hits, documents, doc_lengths, doc_ids, strict=True):
        # 1. BM25 风格词频得分（原问题里的词权重为 2.0，同义扩展词权重为 1.0）
        lex_score = 0.0
        for term in expanded_terms:
            tf = freqs[term]
            if tf == 0:
                continue
            weight = 2.0 if term in raw_q_terms else 1.0
            idf = math.log(1.0 + (count - df[term] + 0.5) / (df[term] + 0.5))
            tf_norm = (tf * 2.5) / (tf + 1.5 * (0.25 + 0.75 * (dl / max(avg_dl, 1.0))))
            lex_score += weight * idf * tf_norm

        # 2. 多子查询命中的 RRF 累积得分
        rrf_bonus = rrf_scores.get((hit.entry_id, hit.entry_version_id), 0.0) * 20.0

        # 3. 跨文档匹配加分（如果问题明确命中了某家公司的 document_id，优先排该公司的页面）
        doc_bonus = (15.0 if doc_match_map[did] else -15.0) if has_matched_doc else 0.0

        # 4. 目录页小幅降权（避免目录页排在真实数据表前面）
        text_lower = hit.text.lower()
        toc_penalty = -5.0 if ("table of contents" in text_lower and "item 1" in text_lower and "item 8" in text_lower) else 0.0

        total = lex_score + rrf_bonus + doc_bonus + toc_penalty
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

    # 【关键修复】：SearchMemoryRequest 的 limit 严格控制在 <= 50（或兼容大 limit 但绝不超过 100），防止触发框架上限报错！
    candidate_limit = min(50, max(limit * 2, 30))
    for query in queries[:24]:
        result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=candidate_limit))
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