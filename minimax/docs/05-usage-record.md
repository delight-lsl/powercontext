# 05 · 技术使用记录 (Technology usage record)

本表记录本方案**实际使用**的技术组件、版本、在本方案中的用途，以及对应的验证证据。
凡未使用的技术不列入。

## 1. 核心平台

| 技术 | 版本 | 在本方案中的用途 | 验证证据 |
| --- | --- | --- | --- |
| **PowerContext** | 1.4.1.dev180+gcd1b5e3f5（仓库 `src/powercontext`） | 上下文存储与检索底座：Content Sources 存语料，Atomic Memory 存进度与验证结果；提供 HTTP API 与官方 Python SDK | `artifacts/ingest-report.json`、`artifacts/context/pack-001.json` |
| **OceanBase seekdb** | 1.4.0（`VERSION()` = `5.7.25-OceanBase seekdb-v1.4.0.0`） | PowerContext 的持久化数据库，语料与进度真实落库 | `pc_sources` / `pc_atomic_memory_current` 的 SQL 直查结果 |
| **SQLAlchemy + pyobvector** | 2.1.6 / 0.2.29 | OceanBase 官方异步方言 `mysql+aoceanbase`，由 PowerContext 自身使用 | PowerContext Server 启动日志与 `pc_sources` 表 |
| **FastAPI + Uvicorn** | 0.162.0 / 0.54.0 | PowerContext Server 的 HTTP 承载 | `curl /v1/server-info` 返回 `package_version` |
| **Pydantic** | 2.14.1 | PowerContext 的请求 / 响应模型 | `artifacts/minimax/response.json` 的结构化错误载荷 |

## 2. 本方案自研组件（Python 3.12.14，无第三方依赖）

| 模块 | 职责 | 关键设计 |
| --- | --- | --- |
| `sddctx/models.py` | 记录模型与 JSON 信封 | 内容寻址 `source_id`（文本 + 文件哈希 + 行号范围） |
| `sddctx/parsing.py` | 规格 / 代码 / 测试切分 | YAML front matter + AST；`test_regression_*` 自动归类 |
| `sddctx/store.py` | PowerContext 读写封装 | 幂等写入、作用域解析、进度读写与检索 |
| `sddctx/retrieval.py` | 任务驱动检索 | 拉丁词 + CJK 二元组；IDF 加权覆盖率；`applies_to` 推导变更面 |
| `sddctx/render.py` | 提示词与报告渲染 | 每条结果携带 `source_id` / `db_position` / `sha256` / `server_content_digest` |
| `sddctx/minimax_client.py` | MiniMax 客户端 | OpenAI / Anthropic 两种风格；密钥不落盘；丢弃 `reasoning_content` |
| `sddctx/cli.py` | 命令行入口 | `ingest` / `query` / `prompt` / `state` / `minimax` / `apply` |
| `scripts/run_e2e.py` | 端到端验证 | 逐步记录命令与退出码；失败不中断，真实记录 |
| `scripts/prove_restart_persistence.py` | 重启持久化验证 | 自起服务 → 空状态 → 写入 → 重启 → 读回 → 复用 |
| `scripts/prove_spec_revision.py` | 规格修订验证 | 改规格 → 重导入 → 对比上下文 → 还原仓库 |
| `scripts/reset.py` | 重置工具 | 作用域 / 产物 / 工作区 / 数据库四类重置 |

## 3. 模型服务

| 技术 | 版本 / 端点 | 用途 | 状态 |
| --- | --- | --- | --- |
| **MiniMax-M3** | `https://api.minimaxi.com/v1`（国内站） | 依据检索到的上下文生成改动代码 | **未完成**：HTTP 402 / 业务码 1008 `insufficient balance` |
| MiniMax-M3.1-Flash | 同上 | 备选模型 | 同上（同样受余额限制） |
| Anthropic 兼容端点 | `<base>/anthropic/v1/messages` | 备选调用风格 | 已实现，未在本轮使用 |

## 4. 工程工具

| 工具 | 版本 | 用途 |
| --- | --- | --- |
| uv | 0.12.15 | 创建虚拟环境、安装依赖（可编辑模式） |
| pytest | 9.2.0 | 样例项目测试（26 个用例） |
| Docker | Desktop / Engine | 运行 seekdb 1.4.0 容器 |
| MySQL 客户端 | 容器内 `mysql` | 直接查询 `pc_*` 表做证据核对 |

## 5. 样例项目

| 项 | 说明 |
| --- | --- |
| 语言 / 运行时 | Python 3.12（`from __future__ import annotations`） |
| 依赖 | 仅标准库（`dataclasses` / `decimal` / `datetime` / `types` / `typing`） |
| 测试 | pytest，26 个用例（20 回归 + 6 功能） |
| 金额表示 | 全部使用 `Decimal`，`ROUND_HALF_UP` 量化到分 |
| 规格 | 2 份 Markdown + YAML front matter（`applies_to` / `keywords`） |

## 6. 未使用的技术及原因

| 技术 | 原因 |
| --- | --- |
| 向量嵌入 / 语义检索 | 检索采用可解释的双语词法 + IDF 方案；PowerContext 未配置推理服务，避免引入对外部模型服务的硬依赖 |
| PowerContext MCP 服务 | 本方案以 HTTP API + 官方 Python SDK 为入口（题目允许「CLI 或其它调用入口」）；MCP 已通过 `POWERCONTEXT_SERVER_MCP_ENABLED=false` 显式关闭 |
| PowerContext Dashboard | 同上，`POWERCONTEXT_SERVER_DASHBOARD_ENABLED=false` |
| LLM 生成式摘要 / "dream" 能力 | 上下文组装要求可追溯到具体记录行，摘要会破坏出处链 |

## 7. 版本锁定建议

复现环境的关键版本：

```
python            3.12.14
uv                0.12.15
powercontext      1.4.1.dev180+gcd1b5e3f5   (editable, from ./src/powercontext)
sqlalchemy        2.1.6
pyobvector         0.2.29
httpx              0.28.1
pytest             9.2.0
oceanbase/seekdb   1.4.0-100000172026082615
```
