# 06 · 一页技术说明 (One-page technical note)

**规有所依 —— 面向软件持续演进的 SDD 上下文管理系统**

## 问题

规格驱动开发（SDD）里，规格是权威的，但工程实践通常把规格、代码、测试和历史决策
散落在 Markdown、IDE 和聊天记录里。模型拿到的是「整仓库的文本」而不是「这次改动
真正相关的那部分」，于是它要么漏改、要么越界改、要么破坏了本该保持不变的既有行为。
本方案要解决的问题是：**让一次开发任务所需要的上下文，可以被精确检索、可追溯、
可跨会话延续。**

## 做法

以 **PowerContext** 作为上下文底座，以 **OceanBase seekdb** 作为持久化存储，
在其上构建一条从「规格」到「代码改动」的可追溯流水线。

### 1. 把仓库切成可寻址的记录

规格按 front matter + 标题切分为小节，代码按 AST 切成模块级符号，测试按 AST 切成
单个用例；`test_regression_*` 自动归类为回归用例。每条记录带文件、行号范围与文件哈希，
并封装成 JSON 信封写入 PowerContext 的 **Content Sources**，真实落在 seekdb 的
`pc_sources` 表里。

### 2. 内容寻址，让「改规格」成为一等公民

记录身份 `source_id` 由 **文本 + 文件哈希 + 行号范围** 的摘要构成。于是：

* 内容不变 → 身份不变 → 重复导入幂等，不产生重复行；
* 内容变化 → 身份变化 → 写入新版本，**旧版本仍然留在库里**。

检索时按记录取 `db_position` 最大的版本，即最新版本。规格一改，重新导入，
检索到的内容立刻随之改变——实测验证见 `03-verification.md` 要求 3。

### 3. 用规格的 `applies_to` 推导变更面

规格 front matter 显式声明它约束的符号、文件与测试：

```yaml
applies_to:
  - symbol:billing.pricing.compute_invoice_totals
  - file:src/billing/pricing.py
  - test:tests/test_pricing.py
```

检索先按分数选出**主规格**（本次改动的规格），再用它的 `applies_to` 定义
「要改哪些文件、要改哪些符号、要跑哪些测试」。词法命中的其它内容作为补充并入，
但标记为 `selected_by: lexical`，与 `spec-link` 区分。结果中的
`Files to modify` 是**推导出来的**，不是模型猜的。

### 4. 双语词法 + IDF，无需向量模型

特征 = 拉丁词 + **CJK 字符二元组**（`促销码折扣` → `促销`/`销码`/`码折`/`折扣`）。
排序用 **IDF 加权的查询覆盖率**：覆盖率越高越相关，罕见词权重更大。
因此中文任务能召回中文规格、英文任务能召回英文规格，
**且不需要任何嵌入模型或推理服务**——PowerContext 可以纯离线运行。

### 5. 进度与验证结果也存进上下文底座

任务进度与验证结果写入 PowerContext 的 **Atomic Memory**
（`pc_atomic_memory_current` 表），而不是本地文件。每次检索都会把作用域内的进度
一并读出，作为上下文包的一部分交给模型。实测：服务进程重启后，
进度条目仍可读回（artifact_id / revision / text 逐字段一致），
并被下一次检索直接复用。

### 6. 出处贯穿始终

检索报告、提示词、模型请求三处都携带 `source_id`、`db_position`、`file_sha256`、
`server_content_digest`。任何一条上下文都能回溯到**数据库中的哪一行**和
**仓库中的哪几行**。

## 结果

| 指标 | 实测值 |
| --- | --- |
| 一次检索返回的规格小节 | 12（`SPEC-BILL-001` 7 + `SPEC-BILL-002` 5） |
| 待改文件 | `src/billing/pricing.py`（唯一，由主规格推导） |
| 待改符号 | 7 |
| 功能测试 / 回归测试 | 6 / 20 |
| 改动前测试结果 | 全量 `6 failed, 20 passed`（功能未实现） |
| 改动后测试结果 | 全量 `26 passed` |
| 重启后进度条目 | 2 条，字段逐一致 |
| 规格修订后上下文 | 7 个小节换新版本，新内容出现，旧内容不再出现 |

## 边界与当前缺口

* **MiniMax 生成代码这一步尚未跑通**：本机 MiniMax 账户余额不足，
  API 返回 HTTP 402 / 业务码 1008 `insufficient balance`。
  请求记录、错误载荷、端点与模型选择的排查结论都已落盘
  （`artifacts/minimax/response.json`），额度恢复后重跑
  `scripts/run_e2e.py` 即可产出真实的模型回答与 diff。
  当前的「改动后」代码来自仓库内的手写参考实现
  `minimax/reference/pricing_promo.py`，产物中以 `code_source: reference` 明确标注。
* 检索采用词法方案，规格与代码的措辞差异较大时召回会下降；
  当前靠 `applies_to` 显式链接兜底，这是有意的取舍——出处链不会被破坏。

## 交付物

| 目录 | 内容 |
| --- | --- |
| `minimax/sddctx/` | 索引 / 检索 / 渲染 / MiniMax 客户端 / CLI |
| `minimax/sample/` | 样例项目（2 份规格、3 个源文件、26 个测试） |
| `minimax/reference/` | 规格的手写参考实现 |
| `minimax/scripts/` | 端到端验证、重启持久化验证、规格修订验证、重置 |
| `minimax/artifacts/` | 全部验证证据（命令、退出码、请求 / 响应、diff、测试日志、快照） |
| `minimax/docs/` | 搭建、架构、验证、重置、技术使用记录、本页说明、未完成项 |
