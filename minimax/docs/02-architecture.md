# 02 · 方案架构 (Architecture)

```
                    ┌──────────────────────────────────────────┐
                    │      PowerContext Server (1.4.1)         │
  sddctx CLI  ────► │  HTTP API /v1/...  +  MCP (可选)        │
                    │  Content Sources · Atomic Memory        │
                    └───────────────┬──────────────────────────┘
                                    │  mysql+aoceanbase (pyobvector)
                    ┌───────────────▼──────────────────────────┐
                    │   OceanBase seekdb 1.4.0 (127.0.0.1:2881)│
                    │   pc_sources · pc_atomic_memory_current  │
                    └──────────────────────────────────────────┘
                                    ▲
                    ┌───────────────┴──────────────────────────┐
                    │  minimax/sddctx   索引 · 检索 · 渲染       │
                    │  parsing · retrieval · render · store    │
                    └──────────────────────────────────────────┘
                                    │  检索到的上下文 (prompt)
                    ┌───────────────▼──────────────────────────┐
                    │  MiniMax API (api.minimaxi.com/v1)      │
                    │  MiniMax-M3 / MiniMax-M3.1-Flash        │
                    └──────────────────────────────────────────┘
```

## 1. 组件

| 路径 | 职责 |
| --- | --- |
| `minimax/sddctx/models.py` | `Record` 数据模型、JSON 信封、内容寻址 `source_id` |
| `minimax/sddctx/parsing.py` | 把规格 / 代码 / 测试解析成可索引的最小记录单元 |
| `minimax/sddctx/store.py` | `PowerContextStore`：把记录写入 Content Sources、把进度写入 Atomic Memory |
| `minimax/sddctx/retrieval.py` | 双语词法 + IDF 排序 + `applies_to` 变更面推导 |
| `minimax/sddctx/render.py` | 渲染发给模型的提示词与带出处的检索报告 |
| `minimax/sddctx/minimax_client.py` | MiniMax 文本 API 客户端（OpenAI / Anthropic 两种风格） |
| `minimax/sddctx/cli.py` | 命令行入口：`ingest` / `query` / `prompt` / `state` / `minimax` / `apply` |

## 2. 记录模型 (Record model)

PowerContext 的 Content Source 是**整块内容**的存储单元。为了让检索结果精确到
「一条规格 / 一个函数 / 一个测试」，本方案先把项目切分成记录，再把记录写入存储。

每条记录包含：

```json
{
  "record_id": "SPEC-BILL-001#促销码目录-promotion-catalog",
  "kind": "spec_section",
  "file": "specs/SPEC-BILL-001-invoice-promo-discount.md",
  "lines": [46, 54],
  "sha256": "<文件内容哈希>",
  "text": "<该单元的原文>",
  "meta": { "spec_id": "SPEC-BILL-001", "applies_to": ["symbol:...", "file:...", "test:..."] }
}
```

三种记录类型：

| kind | 切分方式 | 样例 |
| --- | --- | --- |
| `spec_section` | YAML front matter + `##` / `###` 标题切分 | `SPEC-BILL-001#折扣计算规则-discount-rules` |
| `code_symbol` | Python AST 的模块级 `def` / `class` | `billing.pricing.compute_invoice_totals` |
| `test_case` | Python AST 的 `test_*` 函数 | `tests/test_pricing.py::test_regression_tax_is_computed_on_the_subtotal` |

`test_*` 中以 `test_regression_` 开头的用例自动标记为 `category = "regression"`，
其余为功能用例。这样「本次改动的验收测试」与「必须保持通过的既有行为」在数据结构上
就是可区分的。

## 3. 内容寻址与版本 (Content addressing and versioning)

PowerContext 的 `source_id` 是身份的载体：同一个 `source_id` 再次写入且内容不同会返回
`409 source_conflict`。为了既避免冲突、又能表达「规格改过」：

```python
source_id = "sddctx:v1:" + kind + ":" + record_id + ":" + sha256(text + sha256(file) + lines)[:12]
```

哈希覆盖**文本 + 文件哈希 + 行号范围**，因此：

* 内容不变 → `source_id` 不变 → 重复导入是幂等的，不产生新行；
* 内容变化 → `source_id` 变化 → 写入新的 Source 版本，旧版本仍然留在库里。

检索时按 `record_id` 取 `db_position` 最大的那一版，即「最新版本」。
实测（`artifacts/spec-revision/spec-revision-proof.json`）：

```
sddctx:v1:spec_section:SPEC-BILL-001#促销码目录-promotion-catalog:13bbead34177   3   (v1)
sddctx:v1:spec_section:SPEC-BILL-001#促销码目录-promotion-catalog:2cf2853168b2   50  (v2)
```

## 4. 检索算法 (Retrieval)

检索是**可解释**的，不依赖向量模型，因此 PowerContext 无需配置任何推理服务即可工作。

### 4.1 特征提取

* 拉丁词：`[a-z0-9_]+`，长度 ≥ 2，去停用词；
* 中文：连续 CJK 串的**字符二元组**（bigram）。

`"促销码折扣"` → `促销`、`销码`、`码折`、`折扣`；`"promo discount"` → `promo`、`discount`。
中文任务与中文规格因此可以互相召回，无需分词器或嵌入模型。

### 4.2 排序

对每条记录计算

```
score(record) = Σ idf(term) for term in query ∩ record   /   Σ idf(term) for term in query
idf(term) = ln(1 + N / (1 + df(term)))
```

即 **IDF 加权的查询覆盖率**：覆盖率越高越相关，罕见词（高 IDF）权重更大。

### 4.3 变更面推导 (change surface)

规格 front matter 中的 `applies_to` 是权威指针：

```yaml
applies_to:
  - symbol:billing.pricing.compute_invoice_totals
  - file:src/billing/pricing.py
  - test:tests/test_pricing.py
```

检索时：

1. 按分数选出 **主规格**（本次改动的规格）；
2. 主规格的 `applies_to` 定义**变更面**：需要改的文件、要改的符号、要跑的测试；
3. 词法命中的其它规格 / 符号 / 测试作为补充并入上下文，但标记为 `selected_by: lexical`；
4. 每条结果都记录 `linked_by`（被哪个规格链接）与 `linked_by_primary_spec`（是否来自主规格）。

`ContextPack.files_to_modify` 只统计「主规格链接」或「词法命中」的代码项，
因此输出里的「Files to modify」是**规格推导出来的**，不是猜出来的。

一次真实检索的结果（`artifacts/context/pack-001.md`）：

```
Files to modify: src/billing/pricing.py
## Applicable specifications (12)
## Code to modify (7)
## Functional tests (6)
## Regression tests (20)
```

## 5. 进度与验证结果 (Atomic Memory)

任务进度与验证结果写入 PowerContext 的 Atomic Memory（`pc_atomic_memory_current` 表），
而不是本地文件：

```bash
python -m sddctx state save --kind progress --text "..."
python -m sddctx state list
```

每次 `query` / `prompt` 都会把作用域内的进度一并读出，放进上下文包的 `prior_progress`，
最终渲染为提示词中的「Progress recorded in the context store」段落。
这使得**跨会话、跨进程重启**的上下文可以延续：详见 `03-verification.md` 的重启验证。

## 6. 出处可追溯 (Provenance)

检索报告与提示词中每一项都带出处：

```
## billing.pricing.compute_invoice_totals (src/billing/pricing.py:66-87)
[source_id=sddctx:v1:code_symbol:billing.compute_invoice_totals:4da3…
 db_position=21 file_sha256=6a69111712e4… server_digest=sha256:72efa14dffef…]
```

* `source_id`：存储中的身份，内容变化即变化；
* `db_position`：该版本在数据库日志中的位置，用于判定最新版本；
* `file_sha256`：源文件内容哈希，用于和仓库文件逐一对齐；
* `server_content_digest`：服务端计算的内容摘要（形如 `sha256:72efa14dffef…`），用于核对存储侧未被篡改。

## 7. 提示词渲染 (Prompt rendering)

`render.py` 的 `SYSTEM_PROMPT` 明确告诉模型：规格是权威的、只改列出的符号、
回归测试覆盖的行为不得改变，并以 `=== FILE: <path> ===` 分块返回完整文件内容。
`sddctx/cli.py` 的 `parse_model_files()` 解析这些分块，`sddctx apply` 负责落盘并生成 diff。

模型回答中记录的只有**公开元数据**（model、usage、finish_reason、request_id、content）。
密钥在写盘前被替换为 `<redacted>`，模型的私有思维链字段 `reasoning_content` 在客户端
就被丢弃，不进入任何产物。
