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
# 第一部分：基础校验与唯一身份生成（对应题目 2.3 节）
# =====================================================================

def _validate_document_id(value: Any) -> str:
    """
    校验文档 ID：
    【题目 2.3 原文】：“文档 ID 不得为空或纯空白。”
    如果传入的不是字符串，或者是 "" / "   "，直接抛出 ValueError。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("document_id must be non-empty and not pure whitespace")
    return value


def _validate_page_number(value: Any) -> int:
    """
    校验物理页码：
    【题目 2.3 原文】：“按顺序返回所有页，页码从 1 开始”
    【防御推论】：在 Python 中 isinstance(True, int) 为 True，必须显式排除 bool 类型！
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("page_number must be a positive 1-based integer")
    return value


def _page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """
    生成每一页的唯一身份标识（Source Identity）：
    【题目 2.3 原文】：“Source 身份由 (document_id, document_sha256, page_number) 决定，
    稳定、无碰撞且不超过 256 字符。PDF 修订后产生新身份，旧引用继续指向原修订。”

    【代码解释】：
    这里把 (文档名, PDF的SHA256摘要, 页码) 转成紧凑的 JSON 字符串，再算一次 SHA256，
    拼成 "pdf-page:<64位哈希>"，总长度固定为 73 个字符（远小于 256 上限），既稳定又绝对无碰撞！
    """
    _validate_document_id(document_id)
    if not isinstance(document_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", document_sha256):
        raise ValueError("document_sha256 must be a lowercase SHA-256 digest")
    _validate_page_number(page_number)

    identity = json.dumps((document_id, document_sha256, page_number), ensure_ascii=False, separators=(",", ":"))
    return f"pdf-page:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


# =====================================================================
# 第二部分：输入输出数据模型与 PDF 解析（对应题目 2.2 & 2.3 节）
# =====================================================================

class PDFDocumentInput(BaseModel):
    """
    整本 PDF 的输入模型：
    【修改点】：加上 strict=True，防止把字符串 "abc" 自动当成 bytes 放行。
    """
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    document_id: str
    content: bytes

    @model_validator(mode="after")
    def validate_document(self) -> PDFDocumentInput:
        _validate_document_id(self.document_id)
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("PDF content cannot be empty")
        return self


class PDFPageInput(BaseModel):
    """
    单页 PDF 的输出模型：
    包含文档名、整本 PDF 的 SHA-256 摘要、1开始的物理页码、当页提取出的文本。
    """
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

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
    核心解析函数：把整本 PDF 拆成一页一页的 PDFPageInput。
    【题目 2.3 原文要求】：
    1. “以物理页为分块边界，按顺序返回所有页，页码从 1 开始，包括空白页。”
    2. “摘要为整个输入 PDF 的 SHA-256 小写十六进制字符串。”
    3. “带有空密码权限标记、无需用户提供密码即可读取的 PDF 属于合法输入。”
    4. “空文件、非 PDF、损坏或无法完整解析的文件以 ValueError 失败，不返回部分页。”
    """
    if not isinstance(value, PDFDocumentInput):
        raise ValueError("Input must be a PDFDocumentInput instance")
    raw = value.content
    if not raw:
        raise ValueError("PDF content cannot be empty")

    try:
        # 【关键修改 1】：将 strict=True 改为 strict=False！
        # 原因：真实财报 PDF 常带有微小的非致命格式警告，strict=True 会导致正常财报误报错崩溃。
        reader = PdfReader(io.BytesIO(raw), strict=False)

        # 处理加密 PDF：尝试用空密码 "" 解密；如果返回 0 说明需要真密码，抛出 ValueError
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a password")

        # 计算整份 PDF 原始字节流的 SHA-256 小写十六进制指纹
        digest = hashlib.sha256(raw).hexdigest().lower()

        # 遍历所有物理页（从 1 开始计数），提取文字；如果是空白页（None），转为空字符串 ""
        # 因为在 tuple(...) 列表推导式里一次性跑完，只要中间任何一页损坏报错，就会直接跳到 except，绝不会返回部分页！
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
    except ValueError:
        raise
    except Exception as error:
        # 将所有 pypdf 内部异常统一转换为题目要求的 ValueError
        raise ValueError("PDF is damaged or cannot be fully parsed") from error
    return pages


# =====================================================================
# 第三部分：Source 定义与标准文本投影（对应题目 2.3 节下半部分）
# =====================================================================

class PDFPageSource(Source):
    """
    存入框架底层的 Source 对象：
    【题目 2.3 原文】：“Definition 的名称为 pdf-page、版本为 1，输入类型为 PDFPageInput。
    每页解析为 CAPTURED Source”
    """
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

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
    """
    标准文本投影器（把页面转换成 Memory 检索库里的格式）：
    【题目 2.3 原文】：“标准文本投影的 source_type="pdf-page"，source_id 为 Source name，
    content 为 DOCUMENT_ID\nPAGE_TEXT。metadata 恰好包含 document_id、document_sha256 和 page_number。”
    """
    name: str = TEXT_EVIDENCE_PROJECTION_KEY.name
    version: str = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PDFPageSource] = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        if not isinstance(source, PDFPageSource):
            source = PDFPageSource.model_validate(source)
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
    """
    Source 适配器：
    - resolve: 入库时把 PDFPageInput 包装成带唯一 ID 的 PDFPageSource。
    - read: 【题目 2.3 原文】“read 返回该页输入”，把 PDFPageSource 还原为 PDFPageInput。
    """
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
# 第四部分：离线证据页检索与重排（对应题目 2.4 & 2.5 & 2.6 节）
# =====================================================================

# 【题目 2.5 原文】：“文本转小写，以正则 [a-z]+|\d+ 分词”
_WORDS = re.compile(r"[a-z]+|\d+", re.IGNORECASE)

# 停用词表：过滤掉疑问词、介词以及 FinanceBench 题目里常见的分析师套话词
_STOP_WORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "by", "can",
    "could", "calculate", "define", "did", "do", "does", "for", "from", "give",
    "has", "have", "had", "how", "if", "in", "into", "is", "it", "its", "less",
    "may", "might", "must", "no", "not", "of", "on", "only", "or", "our", "out",
    "over", "perspective", "please", "reported", "relying", "rely", "respond",
    "response", "round", "rounded", "should", "shown", "state", "statement",
    "statements", "such", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "those", "through", "to", "under", "up", "use",
    "using", "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "will", "with", "within", "without", "would", "year", "years", "your",
    "assuming", "assume", "analyst", "investment", "details", "directly",
    "outlined", "explicitly", "mentioned", "addressed", "answer", "question",
    "company", "consolidated", "financial", "annual", "report", "form", "fy",
    "usd", "million", "millions", "billion", "billions", "thousand", "thousands",
    "dollar", "dollars", "percent", "percentage",
])

# 广撒网探测词：用来把当前 scoped Memory 中的所有候选页面尽量捞出来，防止因为用词差异漏掉证据页
_BROAD_PROBES = ("the", "of", "and", "in", "to", "2022", "2023", "2021", "total", "net")

# 财务领域同义词与报表映射规则：(匹配问题的正则, 扩展搜索短语, 额外加权的核心词)
# 解决“问题问 quick ratio / capex，但财报原文写的是 balance sheets / capital spending”的鸿沟
_FINANCIAL_RULES: list[tuple[re.Pattern[str], tuple[str, ...], tuple[str, ...]]] = [
    # 1. 资本支出 / 自由现金流 -> 现金流量表 (PepsiCo 叫 capital spending)
    (
        re.compile(
            r"\bcapex\b|\bcapital expenditure|\bcapital spending\b|\bfree cash flow\b|\bfcf\b|\bproperty,?\s*plant\b|\bppe\b",
            re.I),
        ("consolidated statement of cash flows", "capital spending", "capital expenditures",
         "additions to property plant and equipment", "investing activities"),
        ("capital", "spending", "expenditures", "additions", "property", "plant", "equipment", "flows"),
    ),
    # 2. 资产负债表比率 (速动比率/流动比率/周转率/营运资金/存货/应收应付) -> 资产负债表
    (
        re.compile(
            r"\bquick ratio\b|\bcurrent ratio\b|\bworking capital\b|\bbalance sheet|\bfinancial position\b"
            r"|\binventor|\breceivable|\bpayable|\bdpo\b|\bdso\b|\bdio\b|\bfixed assets?\b|\btotal assets\b"
            r"|\bsolvency\b|\bliquidity\b|\bturnover\b|\broa\b|\broe\b|\broic\b|\bgoodwill\b",
            re.I,
        ),
        (
        "consolidated balance sheets", "total current assets", "total current liabilities", "cash and cash equivalents",
        "inventories", "accounts receivable", "accounts payable", "total assets"),
        ("balance", "sheets", "current", "assets", "liabilities", "inventories", "receivable", "payable", "equivalents",
         "goodwill"),
    ),
    # 3. 债务与借款 -> 资产负债表 & 债务附注
    (
        re.compile(r"\bdebt\b|\bborrowings?\b|\bleverage\b|\binterest coverage\b|\bnotes payable\b", re.I),
        ("consolidated balance sheets", "long-term debt", "short-term debt", "debt obligations",
         "short-term borrowings"),
        ("debt", "borrowings", "obligations", "maturities", "liabilities", "interest"),
    ),
    # 4. 利润表相关 (营收/毛利/营业利润/净利/EPS/成本/费用/税) -> 利润表
    (
        re.compile(
            r"\bincome statement|\bstatement of operations|\bstatement of earnings|\bp&l\b|\brevenue|\bsales\b"
            r"|\bgross margin|\bgross profit|\boperating margin|\boperating income|\boperating profit|\bebit\b"
            r"|\bnet income|\bnet profit|\bearnings per share|\beps\b|\bcogs\b|\bcost of|\bsg&a\b|\bselling\b|\br&d\b|\btax\b",
            re.I,
        ),
        ("consolidated statement of income", "consolidated statements of operations",
         "consolidated statements of earnings", "net revenue", "net sales", "cost of sales", "gross profit",
         "operating profit", "operating income", "provision for income taxes"),
        ("revenue", "revenues", "sales", "gross", "operating", "profit", "income", "earnings", "cost", "selling",
         "administrative", "diluted"),
    ),
    # 5. EBITDA / 折旧摊销 -> 现金流量表 & 利润表
    (
        re.compile(r"\bebitda\b|\bdepreciation\b|\bamortization\b|\bd&a\b", re.I),
        (
        "depreciation and amortization", "consolidated statement of cash flows", "operating profit", "operating income",
        "operating activities"),
        ("depreciation", "amortization", "operating", "income", "profit", "flows"),
    ),
    # 6. 重组费用 (Restructuring)
    (
        re.compile(r"\brestructuring\b|\bimpairment\b", re.I),
        ("restructuring and impairment", "restructuring charges", "restructuring costs"),
        ("restructuring", "impairment", "charges", "costs"),
    ),
    # 7. 股息与股票回购 -> 现金流量表融资活动
    (
        re.compile(r"\bdividends?\b|\brepurchases?\b|\bbuybacks?\b|\bpayout\b", re.I),
        ("consolidated statement of cash flows", "financing activities", "cash dividends paid", "share repurchases",
         "repurchases of common stock"),
        ("dividends", "paid", "repurchases", "repurchased", "common", "stock", "financing"),
    ),
    # 8. 法律诉讼 -> Item 3 Legal Proceedings
    (
        re.compile(r"\blegal\b|\blitigation\b|\blawsuits?\b|\bproceedings?\b|\bbattles?\b|\bcontingenc", re.I),
        ("item 3 legal proceedings", "legal proceedings", "commitments and contingencies"),
        ("legal", "proceedings", "litigation", "contingencies", "item"),
    ),
    # 9. 业务分部与地理区域 -> Segment 附注 (PepsiCo 叫 divisions)
    (
        re.compile(r"\bsegments?\b|\bdivisions?\b|\bgeograph|\bcountries\b|\bregions?\b|\boperates in\b", re.I),
        ("divisions", "reportable segments", "segment reporting", "geographic", "net revenue by division"),
        ("segment", "segments", "division", "divisions", "geographic", "countries", "international"),
    ),
]


def _clean_boilerplate(question: str) -> str:
    """剥离 FinanceBench 问题里长长的分析师角色扮演提示词，防止里面的废话词干扰搜索权重。"""
    core = re.sub(
        r"(?:Respond|Give a response) to the question (?:by assuming[^.]*|by relying[^.]*|assuming[^.]*)\.?",
        " ",
        question,
        flags=re.IGNORECASE,
    )
    core = re.sub(r"Define [^.]* as [^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"Calculate [^.]* using [^.]*\.?", " ", core, flags=re.IGNORECASE)
    core = re.sub(r"If [^.]* are not explicitly (?:outlined|mentioned|addressed)[^.]*\.?", " ", core,
                  flags=re.IGNORECASE)
    return core


def _extract_years(question: str) -> tuple[str, ...]:
    """提取问题中的 4 位年份（例如把 FY2022 或 FY22 统一提取为 '2022'）。"""
    years: list[str] = []
    for m in re.finditer(r"\b(?:fy\s*)?(20\d{2})\b", question, re.I):
        years.append(m.group(1))
    for m in re.finditer(r"\bfy\s*(\d{2})\b", question, re.I):
        years.append(f"20{m.group(1)}")
    return tuple(dict.fromkeys(years))


def _query_terms_and_subqueries(question: str) -> tuple[Counter[str], tuple[str, ...]]:
    """
    【关键修改 2 & 3】：
    1. 修复了原本 `len(word) > 2` 误删 '3M'、'22'、'SG&A' 的 Bug。
    2. 生成带权重的查询词表（供 BM25 算分）和扩展子查询列表（供 memory.search 召回）。
    """
    core_q = _clean_boilerplate(question)
    core_words = [w.lower() for w in _WORDS.findall(core_q) if w.lower() not in _STOP_WORDS]
    all_words = [w.lower() for w in _WORDS.findall(question) if w.lower() not in _STOP_WORDS]
    years = _extract_years(question)

    # 给核心词赋予 3.0 权重，普通词 1.0 权重，年份 2.5 权重
    term_weights: Counter[str] = Counter()
    for w in core_words:
        if len(w) > 1 or w.isdigit():
            term_weights[w] += 3.0
    for w in all_words:
        if (len(w) > 1 or w.isdigit()) and w not in term_weights:
            term_weights[w] += 1.0
    for y in years:
        term_weights[y] += 2.5

    subqueries: list[str] = [question, core_q]
    # 加入相邻双词词组（Bigram），提高短语匹配精度
    non_digits = [w for w in core_words if not w.isdigit() and len(w) > 1]
    for i in range(len(non_digits) - 1):
        subqueries.append(f"{non_digits[i]} {non_digits[i + 1]}")

    # 匹配财务概念规则，加入专业会计表头查询和扩展词权重
    for pattern, phrases, boost_tokens in _FINANCIAL_RULES:
        if pattern.search(question):
            subqueries.extend(phrases)
            for bt in boost_tokens:
                term_weights[bt] += 1.5

    subqueries.extend(w for w in core_words if len(w) >= 3 and not w.isdigit())
    unique_queries = tuple(dict.fromkeys(q.strip() for q in subqueries if q.strip()))
    return term_weights, unique_queries


def _score_statement_structure(text_lower: str, question_lower: str, years: tuple[str, ...]) -> float:
    """
    【关键修改 5】：根据 10-K 财报的报表结构加分，并对目录页（Table of Contents）扣分。
    """
    boost = 0.0
    # 如果某一页是目录页（同时出现 table of contents 和多个 item 标题），扣 8 分防止霸榜
    if "table of contents" in text_lower and "item 1" in text_lower and "item 8" in text_lower:
        boost -= 8.0

    is_cf = "statement of cash flows" in text_lower or "statements of cash flows" in text_lower
    is_bs = "balance sheet" in text_lower or "balance sheets" in text_lower or "financial position" in text_lower
    is_is = any(
        h in text_lower
        for h in ("statement of income", "statements of income", "statement of operations", "statements of operations",
                  "statement of earnings", "statements of earnings")
    )

    needs_cf = bool(re.search(
        r"\bcapex\b|\bcapital expenditure|\bcapital spending|\bcash flow|\bfcf\b|\bdepreciation|\bamortization|\bebitda\b|\bdividend|\brepurchase",
        question_lower))
    needs_bs = bool(re.search(
        r"\bquick ratio|\bcurrent ratio|\bworking capital|\bbalance sheet|\binventor|\breceivable|\bpayable|\bdpo\b|\bdso\b|\basset|\bliabilit|\bdebt\b|\bgoodwill\b|\bsolvency|\broa\b|\broe\b|\bturnover",
        question_lower))
    needs_is = bool(re.search(
        r"\brevenue|\bsales\b|\bmargin\b|\bgross profit|\boperating income|\boperating profit|\bebit\b|\bebitda\b|\bnet income|\bearnings|\beps\b|\bcogs\b|\bcost of|\bsg&a\b|\bselling|\br&d\b|\btax\b|\bp&l\b|\bincome statement",
        question_lower))

    if is_cf and needs_cf:
        boost += 12.0
    if is_bs and needs_bs:
        boost += 12.0
    if is_is and needs_is:
        boost += 12.0

    if re.search(r"\blegal\b|\blitigation|\blawsuit|\bbattle|\bproceeding", question_lower):
        if "item 3" in text_lower or "legal proceedings" in text_lower:
            boost += 14.0

    # 如果问题是算数字/指标，奖励包含密集表格数字和目标年份的页面
    if needs_cf or needs_bs or needs_is:
        if len(re.findall(r"\b\d[\d,]*\b", text_lower)) >= 25:
            boost += 3.0
        if years and any(y in text_lower for y in years):
            boost += 2.0

    return boost


def _rank_hits(question: str, hits: list[Any], rrf_scores: dict[tuple[str, str], float]) -> list[Any]:
    """
    核心排序函数：结合 BM25 词频得分 + RRF 多重查询得分 + 目标公司匹配分 + 财务报表结构分。
    """
    if not hits:
        return hits

    term_weights, _ = _query_terms_and_subqueries(question)
    if not term_weights:
        return hits

    question_lower = question.lower()
    q_token_set = {w.lower() for w in _WORDS.findall(question_lower)} - _STOP_WORDS
    years = _extract_years(question)

    # 1. 对所有候选页面进行分词，统计每个词的文档频率（DF）和页面长度（用于 BM25）
    doc_counters: list[Counter[str]] = []
    doc_lengths: list[int] = []
    doc_ids: list[str] = []
    df_counts: Counter[str] = Counter()

    for hit in hits:
        # 题目规定投影的 content 格式为 "DOCUMENT_ID\nPAGE_TEXT"，所以第一行就是 document_id！
        doc_id = hit.text.split("\n", 1)[0]
        doc_ids.append(doc_id)
        tokens = [w.lower() for w in _WORDS.findall(hit.text)]
        counts = Counter(tokens)
        doc_counters.append(counts)
        doc_lengths.append(max(len(tokens), 1))
        for term in term_weights:
            if counts[term] > 0:
                df_counts[term] += 1

    num_docs = len(hits)
    avg_dl = sum(doc_lengths) / max(num_docs, 1)

    # 2. 【关键修改 4】：识别问题针对的是哪家公司的财报（防止在 5 份财报混库时召回错公司）
    unique_doc_ids = set(doc_ids)
    doc_affinity_map: dict[str, float] = {}
    for did in unique_doc_ids:
        # 提取 document_id 里的公司特征词（排除 2022、10k 等通用后缀）
        did_tokens = [
            w.lower() for w in _WORDS.findall(did)
            if w.lower() not in _STOP_WORDS and not re.fullmatch(r"20\d{2}|10|k|q\d", w.lower())
        ]
        matched_co = sum(1 for t in did_tokens if t in q_token_set or t in question_lower)
        matched_yr = sum(1 for y in years if y in did.lower())
        doc_affinity_map[did] = (15.0 * matched_co + 6.0 * matched_yr) if matched_co > 0 else 0.0

    any_company_matched = any(v > 0 for v in doc_affinity_map.values())

    # 3. 逐页计算综合得分（标准 BM25 + RRF + 公司亲和度 + 报表表头加分）
    k1 = 1.5
    b = 0.75
    scored: list[tuple[float, float, Any]] = []

    for hit, counts, dl, did in zip(hits, doc_counters, doc_lengths, doc_ids, strict=True):
        bm25 = 0.0
        for term, weight in term_weights.items():
            tf = counts[term]
            if tf == 0:
                continue
            df = df_counts[term]
            idf = math.log(1.0 + (num_docs - df + 0.5) / (df + 0.5))
            tf_norm = (tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * (dl / max(avg_dl, 1.0))))
            bm25 += weight * idf * tf_norm

        rrf_bonus = rrf_scores.get((hit.entry_id, hit.entry_version_id), 0.0) * 25.0
        co_bonus = (doc_affinity_map[did] if doc_affinity_map[did] > 0 else -40.0) if any_company_matched else 0.0
        struct_bonus = _score_statement_structure(hit.text.lower(), question_lower, years)
        empty_penalty = -15.0 if dl <= 5 else 0.0

        total_score = bm25 + rrf_bonus + co_bonus + struct_bonus + empty_penalty
        scored.append((total_score, hit.score, hit))

    return [hit for _, _, hit in sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)]


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """
    问题检索主接口：
    【题目 2.4 原文】：“通过公开 Memory API 搜索并返回最多 limit 个不同页面的精确 MemoryCitation。
    判题逐条 memory.get 回查文本及 Source，核对完整文档摘要和物理页码。”
    """
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be non-empty text")
    # 拦截 bool 类型的 limit（防止 limit=True 混入）
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be positive")

    _, queries = _query_terms_and_subqueries(question)
    hits_by_id: dict[tuple[str, str], Any] = {}
    rrf_scores: dict[tuple[str, str], float] = {}

    # 1. 先执行针对性的子查询，累积倒数排名分（RRF）
    candidate_limit = max(80, min(300, limit * 8))
    for query in queries[:32]:
        try:
            result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=candidate_limit))
        except Exception:
            continue
        for rank, hit in enumerate(result.hits):
            key = (hit.entry_id, hit.entry_version_id)
            hits_by_id[key] = hit
            rrf_scores[key] = rrf_scores.get(key, 0.0) + (1.0 / (40.0 + rank + 1))

    # 2. 补充执行广撒网高频词探测，确保把库里的证据页全部拉进内存参与 BM25 打分
    for probe in _BROAD_PROBES:
        try:
            result = await memory.search(SearchMemoryRequest(query=probe, mode="fts", limit=1500))
        except Exception:
            continue
        for hit in result.hits:
            key = (hit.entry_id, hit.entry_version_id)
            if key not in hits_by_id:
                hits_by_id[key] = hit
        if len(hits_by_id) >= 1150:
            break

    # 3. 在内存中对所有候选页执行 BM25 + 财报领域规则重排
    ranked = _rank_hits(question, list(hits_by_id.values()), rrf_scores)

    # 4. 逐条通过 memory.get 核对真实的 pdf-page Source 身份，确保返回的是不同物理页的精确引用
    citations: list[MemoryCitation] = []
    page_ids: set[str] = set()
    for hit in ranked:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        try:
            entry = await memory.get(GetMemoryEntryRequest(citation=citation))
        except Exception:
            continue
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