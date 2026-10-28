# 规有所依 —— 面向软件持续演进的 SDD 上下文管理系统

以 **PowerContext** 为上下文底座、以 **OceanBase seekdb** 为持久化存储，
实现「规格 → 检索 → 模型 → 代码改动 → 验证」的闭环，并让每一步都**可追溯、可复现**。

赛题：<https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/qlqnoryzrs7gtqbw>

---

## 目录结构

```
minimax/
├── sddctx/              # 上下文管理系统（索引 · 检索 · 渲染 · MiniMax 客户端 · CLI）
│   ├── models.py        #   记录模型、JSON 信封、内容寻址 source_id
│   ├── parsing.py       #   规格 / 代码 / 测试的切分
│   ├── store.py         #   PowerContext 读写封装（Content Sources + Atomic Memory）
│   ├── retrieval.py     #   双语词法 + IDF + applies_to 变更面推导
│   ├── render.py        #   提示词与带出处的检索报告
│   ├── minimax_client.py#   MiniMax 客户端（密钥不落盘，丢弃私有思维链）
│   └── cli.py           #   ingest / query / prompt / state / minimax / apply
├── sample/              # 样例项目：2 份规格 + 3 个源文件 + 26 个测试
│   ├── specs/           #   SPEC-BILL-001（新行为）· SPEC-BILL-002（不变量）
│   ├── src/billing/     #   money.py · pricing.py（改动目标）· invoice.py（回归护栏）
│   └── tests/           #   20 个回归用例 + 6 个功能用例
├── reference/           # SPEC-BILL-001 的手写参考实现
├── scripts/             # 端到端验证 · 重启持久化验证 · 规格修订验证 · 重置
├── env/                 # 配置模板（仅占位值）
├── docs/                # 搭建 · 架构 · 验证 · 重置 · 使用记录 · 技术说明 · 未完成项
└── artifacts/           # 全部验证证据
```

---

## 快速开始

```bash
# 1. 数据库
docker run -d --name oceanbase-seekdb -p 2881:2881 oceanbase/seekdb:1.4.0
docker exec oceanbase-seekdb mysql -h127.0.0.1 -P2881 -uroot \
  -e "CREATE DATABASE powercontext_sdd CHARACTER SET utf8mb4;"

# 2. Python 环境
cd PowerContext-minimax
uv venv --python 3.12 minimax/.venv
uv pip install --python minimax/.venv/Scripts/python.exe -e .

# 3. 配置（模板里全是占位值，真实值只写本机、不入库）
cp minimax/env/server.env.example minimax/.runtime/server.env

# 4. 启动 PowerContext Server
minimax/.venv/Scripts/powercontext.exe server run --env-file minimax/.runtime/server.env

# 5. 导入 + 检索
cd minimax
.venv/Scripts/python.exe -m sddctx ingest --project sample
.venv/Scripts/python.exe -m sddctx query --task "为发票总价增加促销码折扣能力，先折扣后计税"
```

完整的依赖版本、配置项与初始化步骤见 [`docs/01-setup.md`](docs/01-setup.md)。

---

## 验证

```bash
cd minimax
.venv/Scripts/python.exe scripts/run_e2e.py                  # 端到端
.venv/Scripts/python.exe scripts/prove_restart_persistence.py # 重启持久化
.venv/Scripts/python.exe scripts/prove_spec_revision.py       # 规格修订
```

最近一次实际运行结果：

| 步骤 | 退出码 |
| --- | --- |
| 导入语料到 PowerContext（47 条） | 0 |
| 检索开发上下文（12 规格节 / 7 符号 / 6 功能测试 / 20 回归测试） | 0 |
| 写入任务进度到 PowerContext | 0 |
| 改动前全量测试 | **1**（`6 failed, 20 passed`） |
| 改动前回归测试 | 0（`20 passed`） |
| 改动前目标功能测试 | **1**（`6 failed`，`AttributeError`：功能未实现） |
| MiniMax 代码生成 | **4**（HTTP 402 / code 1008 `insufficient balance`） |
| 应用参考实现 | 0 |
| 改动后全量测试 | 0（`26 passed`） |
| 改动后回归测试 | 0（`20 passed`） |
| 改动后目标功能测试 | 0（`6 passed`） |
| 重启持久化验证 | 通过（4 项检查全部 PASS） |
| 规格修订验证 | 通过（8 项检查全部 PASS） |

证据文件与逐条说明见 [`docs/03-verification.md`](docs/03-verification.md)。

---

## 当前缺口

MiniMax 生成代码这一步**未跑通**：本机 MiniMax 账户余额不足，API 返回
HTTP 402 / 业务码 1008 `insufficient balance`。请求记录、错误载荷与端点 / 模型
排查结论均已落盘。额度恢复后重跑 `scripts/run_e2e.py` 即可产出真实的模型回答与 diff。

在此之前，「改动后」的代码来自仓库内的手写参考实现 `reference/pricing_promo.py`，
产物中以 `code_source: reference` 明确标注，不与模型产出混淆。

详见 [`docs/07-open-issues.md`](docs/07-open-issues.md)。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [`docs/01-setup.md`](docs/01-setup.md) | 依赖与版本、配置项、初始化步骤、命令速查 |
| [`docs/02-architecture.md`](docs/02-architecture.md) | 记录模型、内容寻址、检索算法、变更面推导、出处追溯 |
| [`docs/03-verification.md`](docs/03-verification.md) | 按赛题 8 条要求逐条给出命令、实际输出、预期结果与证据索引 |
| [`docs/04-reset.md`](docs/04-reset.md) | 工作区 / 作用域 / 产物 / 数据库四类重置 |
| [`docs/05-usage-record.md`](docs/05-usage-record.md) | 技术使用记录（版本、用途、验证证据）与未使用技术的理由 |
| [`docs/06-technical-note.md`](docs/06-technical-note.md) | 一页技术说明 |
| [`docs/07-open-issues.md`](docs/07-open-issues.md) | 未完成项、真实错误、排查过程与干净环境复现清单 |

---

## 安全与合规

* 仓库中**不含任何真实凭据**：`minimax/env/server.env.example` 全部为占位值，
  真实配置写在被 `.gitignore` 忽略的 `minimax/.runtime/`；
* MiniMax 密钥只从环境变量 / Windows 用户注册表读取，**从不**写入日志或产物
  （`request.json` 中固定为 `<redacted>`）；
* 模型的私有思维链（`reasoning_content`）在客户端就被丢弃，不进入任何产物。
