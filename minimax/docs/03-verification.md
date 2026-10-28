# 03 · 验证样例与预期结果 (Verification samples and expected results)

本文按赛题 8 条要求逐条给出**可复现的命令**、**实际输出**与**预期结果**，
并指向对应的证据文件。所有输出均为本机实际运行结果，未做任何修饰。

复现入口：

```bash
cd minimax
.venv/Scripts/python.exe scripts/run_e2e.py                  # 主流程
.venv/Scripts/python.exe scripts/prove_restart_persistence.py # 重启持久化
.venv/Scripts/python.exe scripts/prove_spec_revision.py       # 规格修订
```

---

## 要求 1 · 可运行、有测试的样例项目与一处明确的代码改动

**样例项目**：`minimax/sample/`（纯 Python，无第三方依赖，pytest 驱动）。

```
minimax/sample/
├── pyproject.toml
├── pytest.ini
├── specs/
│   ├── SPEC-BILL-001-invoice-promo-discount.md   # 本次改动的新行为规格
│   └── SPEC-BILL-002-money-invariants.md          # 必须保持不变的既有行为
├── src/billing/
│   ├── money.py      # Money 值对象
│   ├── pricing.py    # ← 本次改动的目标文件
│   └── invoice.py    # 故意不改动，作为回归护栏
└── tests/
    ├── conftest.py
    ├── test_money.py       # 8 个回归用例
    ├── test_pricing.py     # 9 个回归用例 + 6 个功能用例
    └── test_invoice.py     # 3 个回归用例
```

**本次改动**：`billing.pricing.compute_invoice_totals` 新增关键字参数 `promo_code`，
实现促销码折扣（先折扣后计税）。详见 `SPEC-BILL-001`。

**必须保持的原有行为**：见 `SPEC-BILL-002` 与 20 个 `test_regression_*` 用例，
涵盖小计计算、税额舍入（`ROUND_HALF_UP` 而非银行家舍入）、
`tax_rate` 校验、`LineItem` 校验与发票渲染。

**验证命令与预期结果**：

```bash
cd minimax/sample
../.venv/Scripts/python.exe -m pytest -q
# 改动前：6 failed, 20 passed
# 改动后：26 passed
```

---

## 要求 2 · 导入规格 / 代码 / 测试到 seekdb，并提供明确的调用入口

**导入**（写入 PowerContext Content Sources，落库到 seekdb 的 `pc_sources`）：

```bash
cd minimax
.venv/Scripts/python.exe -m sddctx ingest --project sample --json artifacts/ingest-report.json
```

实际输出：

```
ingested 47 records into scope scp_2fervhjt293vd3eym9mdjn5q18
(12 spec sections, 9 code symbols, 26 test cases)
```

**调用入口**：`python -m sddctx`（子命令 `ingest` / `query` / `prompt` / `state` / `minimax` / `apply`），
以及统一脚本 `scripts/run_e2e.py`。

**数据库直查**（证明数据确实在 seekdb 里，而不是本地文件）：

```bash
docker exec oceanbase-seekdb mysql -h127.0.0.1 -P2881 -uroot -N -e \
  "SELECT scope_id, COUNT(*) FROM powercontext_sdd.pc_sources GROUP BY scope_id;"
# scp_2fervhjt293vd3eym9mdjn5q18   50
```

本轮导入 **47 条语料**（`source_id` 前缀 `sddctx:v1:`）；另外 3 行是 PowerContext 自己
把 Atomic Memory 条目发布成 Content Source 的结果（`source_id` 前缀 `src_`）。
规格或测试文件每次被修改都会在内容寻址机制下留下一个新版本的 Source，旧版本仍然
保留（见要求 3），因此行数会随开发过程增长，但**同一内容重复导入是幂等的**：
重复执行 `ingest` 不会增加行数（已核对：当前 47 条记录对应的 `source_id` 全部命中，无新增）。
用 `scripts/reset.py --scope minimax-sdd` 重置作用域后，行数回到同一基线。

`pc_atomic_memory_current` 只保存**当前生效**的进度条目，因此条目被
`POST /v1/atomic-memory/lifecycle` 置为 `forgotten` 后会从该表消失，
但它在 `pc_sources` 里的内容版本仍作为历史保留：

```bash
docker exec oceanbase-seekdb mysql -h127.0.0.1 -P2881 -uroot -N -e \
  "SELECT COUNT(*) FROM powercontext_sdd.pc_atomic_memory_current;"
# 3
```

**按任务检索**：

```bash
.venv/Scripts/python.exe -m sddctx query --task "为发票总价增加促销码折扣能力，先折扣后计税"
```

实际输出（节选，完整见 `artifacts/context/pack-001.md`）：

```
Task: 为发票总价增加促销码折扣能力，先折扣后计税
Scope: scp_2fervhjt293vd3eym9mdjn5q18
Corpus records considered: 47
Files to modify: src/billing/pricing.py

## Applicable specifications (12)
## Code to modify (7)
## Functional tests (6)
## Regression tests (20)
```

---

## 要求 3 · 输出包含适用规格、待改代码与测试的真实来源；改规格重导入后输出随之变化

### 3.1 输出内容与出处

每一条结果都带 `source_id` / `db_position` / `file_sha256` / `server_content_digest`，
可逐条回溯到数据库行与仓库文件。证据：`artifacts/context/pack-001.json`、`pack-001.md`。

- 适用规格：12 个规格小节（`SPEC-BILL-001` 7 节 + `SPEC-BILL-002` 5 节）
- 待改代码：7 个符号，全部来自 `src/billing/pricing.py`
- 功能测试：6 个（`test_promo_*`）
- 回归测试：20 个（`test_regression_*`）

### 3.2 规格修订后输出跟随变化

**命令**：

```bash
.venv/Scripts/python.exe scripts/prove_spec_revision.py
```

**实际输出**：

```json
{
  "spec_sections_before": 12,
  "spec_sections_after": 12,
  "respecified_sections": 7,
  "new_content_present_after_reimport": true,
  "new_content_absent_before_edit": true,
  "source_ids_changed": true,
  "repository_restored": true,
  "production_scope_untouched": true,
  "passed": true
}
```

**实际发生了什么**：脚本把 `SPEC-BILL-001` 的促销码目录从 3 个码改为 4 个
（新增 `SAVE30`）、把 `SAVE20` 的折扣上限从 100.00 改为 150.00、`version` 从 1 改为 2，
重新导入后检索结果包含 `SAVE30`，而改动前的检索结果不包含。

**数据库中的两个版本**（`artifacts/spec-revision/spec-revision-proof.json` 的
`stored_source_versions` 字段，实际 SQL 结果）：

```
sddctx:v1:spec_section:SPEC-BILL-001#促销码目录-promotion-catalog:13bbead34177   3
sddctx:v1:spec_section:SPEC-BILL-001#促销码目录-promotion-catalog:2cf2853168b2   50
```

**脚本会还原仓库**：退出前用原始字节覆盖回规格文件，并校验 SHA-256 一致
（`repository_restored: true`），同时确认生产作用域 `minimax-sdd` 未被污染
（`production_scope_untouched: true`）。

对比报告：`artifacts/spec-revision/report-before.md` 与 `report-after.md`。

---

## 要求 4 · 把真正检索到的上下文交给 MiniMax，并留下可核验记录

**命令**：

```bash
.venv/Scripts/python.exe -m sddctx minimax --task "为发票总价增加促销码折扣能力，先折扣后计税"
```

**产物**（`artifacts/minimax/`）：

| 文件 | 内容 |
| --- | --- |
| `request.json` | 真实发出的任务、上下文包、完整提示词、系统提示词、模型与端点 |
| `response.json` | 模型回答 / 失败时的真实错误载荷 |
| `code.diff` | 应用到工作区的真实 diff |

**不记录密钥，也不记录模型的私有思维链**：

* `request.json` 中 `api_key` 字段被写为 `<redacted>`，密钥从不落盘；
* MiniMax 客户端在返回前就丢弃 `reasoning_content` 字段，只保留公开元数据
  （`model` / `usage` / `finish_reason` / `request_id` / `content`）。

### 当前实际状态：MiniMax 账户余额不足（未完成项）

本机调用 MiniMax 的**真实**结果是：

```json
{
  "status": "failed",
  "model": "MiniMax-M3",
  "base_url": "https://api.minimaxi.com/v1",
  "error": "insufficient balance (1008)",
  "error_code": 1008,
  "http_status": 402,
  "error_payload": {
    "type": "error",
    "error": {
      "type": "insufficient_balance_error",
      "message": "insufficient balance (1008)",
      "http_code": "402"
    },
    "request_id": "073129e3fbd6ef6179d8eeafa4498bc6"
  }
}
```

排查结论（均为实测，非推测）：

* 密钥有效：`api.minimax.io`（国际站）返回 `401 / code 2049 invalid api key`，
  说明该密钥属于**国内站**账号；
* 端点必须是 `https://api.minimaxi.com/v1`（该端点鉴权通过并进入计费校验）；
* 模型必须是 `MiniMax-M3` 或 `MiniMax-M3.1-Flash`；
  `MiniMax-M2.5` / `MiniMax-M2.7` 返回 `code 2069 model no longer available`；
* 唯一剩余阻塞点是**账户余额**（HTTP 402 / 业务码 1008）。

恢复步骤见 `07-open-issues.md`。额度可用后重新执行

```bash
.venv/Scripts/python.exe scripts/run_e2e.py
```

即可产出真实的 `artifacts/minimax/response.json`（模型回答）与 `code.diff`，
并且「改动后」的代码将来自模型而非参考实现。

---

## 要求 5 · 保留改动前后快照与同一套目标测试，证明「改前失败、改后通过、回归仍通过」

**命令**：

```bash
.venv/Scripts/python.exe scripts/run_e2e.py
```

**实际输出**（`artifacts/e2e-steps.json` 与 `artifacts/tests/*.txt`）：

```
[OK ] ingest corpus into PowerContext (exit=0) scope=scp_2fervhjt293vd3eym9mdjn5q18 records=47 {'spec_section': 12, 'code_symbol': 9, 'test_case': 26}
[OK ] retrieve development context (exit=0) specs=12 code=7 functional=6 regression=20
[OK ] write task progress into PowerContext (exit=0) artifacts=['art_832b76b3e16945e79af62157514c9a68', 'art_995fd5f135674c1cac621e8ceaf1350b']
[FAIL] test suite before the change (exit=1)              log=artifacts/tests/before.txt
[OK ] regression suite before the change (exit=0)         log=artifacts/tests/before-regression.txt
[FAIL] target functional suite before the change (exit=1) log=artifacts/tests/before-functional.txt
[FAIL] MiniMax code generation (exit=4) error=insufficient balance (1008) code=1008 http=402
[OK ] apply the reference implementation to the working tree (exit=0)
[OK ] test suite after the change (exit=0)               log=artifacts/tests/after.txt
[OK ] regression suite after the change (exit=0)          log=artifacts/tests/after-regression.txt
[OK ] target functional suite after the change (exit=0)    log=artifacts/tests/after-functional.txt
[OK ] write the after-change verification result into PowerContext (exit=0)
[OK ] restore the pre-change working tree (exit=0)
[OK ] retrieve the context again with the stored progress (exit=0) prior_progress=3
```

14 个步骤中 11 个退出码为 0，3 个非 0：两个是**改动前的目标功能测试**（预期失败，
正是需求 5 要证明的「改前未实现」），一个是 **MiniMax 调用**（账户余额不足的真实错误）。

改动前的目标功能测试失败原因（`artifacts/tests/before-functional.txt`）：

```
FAILED tests/test_pricing.py::test_promo_code_applies_a_percentage_discount_before_tax
FAILED tests/test_pricing.py::test_promo_discount_is_capped_by_the_maximum_discount
FAILED tests/test_pricing.py::test_promo_without_code_keeps_the_discount_at_zero
FAILED tests/test_pricing.py::test_unknown_promo_code_is_rejected - AttributeError
FAILED tests/test_pricing.py::test_expired_promo_code_is_rejected - AttributeError
FAILED tests/test_pricing.py::test_promo_errors_are_pricing_errors - AttributeError
====================== 6 failed, 20 deselected in 0.13s =======================
```

即**目标能力尚未实现**（`promo_code` 参数与 `UnknownPromoError` 不存在），
而 20 个回归用例在改动前就全部通过。全量套件在改动前是 `6 failed, 20 passed`，
改动后是 `26 passed`。

改动后（`artifacts/tests/after-functional.txt`、`after-regression.txt`、`after.txt`）：

```
tests\test_pricing.py ......                                             [100%]
====================== 6 passed, 20 deselected in 0.03s =======================
```

**代码快照**：`artifacts/snapshots/before/` 与 `artifacts/snapshots/after/`
（`src/`、`specs/`、`tests/` 三棵目录树，不含 `__pycache__`）。

**关于「改动后代码的来源」**：由于 MiniMax 账户余额不足（要求 4），本次 E2E 的
「改动后」代码来自仓库内的**手写参考实现** `minimax/reference/pricing_promo.py`，
产物中明确标注：

```json
{
  "code_source": "reference",
  "source_file": "reference/pricing_promo.py",
  "target_file": "sample/src/billing/pricing.py",
  "reason": "the MiniMax endpoint was unreachable during this run"
}
```

参考实现的作用是让「改前失败 / 改后通过 / 回归通过」这组证据在模型不可用时依然
可复现；它同时证明 `SPEC-BILL-001` 是可实现的。脚本运行结束会把工作区还原为
改动前状态，因此可以重复执行。

---

## 要求 6 · 真正使用 PowerContext 保存进度与验证结果；空状态起步、模块重启后读回并复用

**命令**：

```bash
.venv/Scripts/python.exe scripts/prove_restart_persistence.py
```

**实际输出**：

```json
{
  "empty_state_confirmed": true,
  "progress_survived_restart": true,
  "server_process_was_restarted": true,
  "progress_reused_in_next_context": true,
  "passed": true
}
```

完整记录见 `artifacts/restart/restart-proof.md` 与 `restart-proof.json`。真实过程：

1. **空状态**：新建作用域 `minimax-sdd-restart-proof-<时间戳>`，
   `POST /v1/memory/entries/list` 返回 `entry_count: 0`；
2. **写入**：把任务进度与验证结果写入 Atomic Memory，
   得到 `art_74e6eaf60a4b4ce3bf3afeea5b804485`（progress）与
   `art_b94e2150447743a788fd25536e9ea909`（verification），revision 均为 1；
3. **重启前读回**：2 条，artifact_id 与文本完全一致；
4. **模块重启**：`SIGTERM` 结束 PowerContext Server 进程（PID 15088 → 47344，
   `service_instance_id` 由 `0550d2ee…` 变为 `60e84d3d…`），再以同一环境文件重新启动；
5. **重启后读回**：仍是同样 2 条，artifact_id / revision / text / state 逐字段一致；
6. **直接查库**：

```bash
docker exec oceanbase-seekdb mysql -h127.0.0.1 -P2881 -uroot -N -e \
  "SELECT artifact_id, revision, kind FROM powercontext_sdd.pc_atomic_memory_current
   WHERE scope_id = 'scp_...';"
# art_b94e2150447743a788fd25536e9ea909	1	verification
# art_74e6eaf60a4b4ce3bf3afeea5b804485	1	progress
```

7. **复用**：重启后执行 `query`，上下文包中 `prior_progress` 带上这两条进度，
   并与从数据库读回的规格 / 代码 / 测试共同构成下一次开发的上下文：

```
corpus_size = 47
files_to_modify = ["src/billing/pricing.py"]
applicable_spec_sections = 12
functional_tests = 6
regression_tests = 20
prior_progress = [verification, progress]
```

> 说明：PowerContext Server 收到 `SIGTERM` 后以退出码 1 结束，这是它自身的关闭
> 行为，不代表数据丢失——数据落在 seekdb 的 `pc_atomic_memory_current` 表中，
> 与进程无关。

---

## 要求 7 · 源码、依赖与版本、配置与初始化步骤、验证样例、预期结果、重置方法、技术使用记录、一页技术说明

| 内容 | 位置 |
| --- | --- |
| 完整源码 | `minimax/sddctx/`、`minimax/sample/`、`minimax/scripts/`、`minimax/reference/` |
| 依赖与版本 | `docs/01-setup.md` §1 |
| 配置与初始化步骤 | `docs/01-setup.md` §2 |
| 验证样例与预期结果 | 本文 `docs/03-verification.md` |
| 重置方法 | `docs/04-reset.md` |
| 技术使用记录 | `docs/05-usage-record.md` |
| 一页技术说明 | `docs/06-technical-note.md` |
| 配置模板（仅占位值） | `minimax/env/server.env.example` |

仓库中**不含任何真实凭据**：`minimax/.runtime/` 被 `.gitignore` 忽略，
`minimax/env/server.env.example` 中所有连接串均为占位符。

---

## 要求 8 · 端到端验证与未完成项的真实记录

**已完成的端到端验证**：见 `artifacts/e2e-steps.json`，14 个步骤中 11 个退出码为 0、
3 个非 0——两个是改动前的目标功能测试（预期失败），一个是 MiniMax 调用（真实错误）。

**未完成项**（真实错误，非伪造）：

| 未完成项 | 真实错误 | 证据 |
| --- | --- | --- |
| MiniMax 生成代码 | HTTP 402 / 业务码 1008 `insufficient balance` | `artifacts/minimax/response.json` |

详见 `docs/07-open-issues.md`。

---

## 证据文件索引

| 文件 | 内容 |
| --- | --- |
| `artifacts/e2e-steps.json` | 端到端每一步的命令、退出码、时间 |
| `artifacts/ingest-report.json` | 导入 PowerContext 的 47 条记录及其 `source_id` / `db_position` |
| `artifacts/minimax-status.json` | 本次运行的模型状态与改动前后的退出码 |
| `artifacts/context/pack-001.json` | 上下文包（含出处），`prior_progress` 为空 |
| `artifacts/context/pack-001.md` | 上下文包的人类可读报告 |
| `artifacts/context/prompt-001.md` | 实际发给模型的提示词 |
| `artifacts/context/pack-002.{json,md}` | 写入进度后再次检索的上下文包，`prior_progress` 为 3 条 |
| `artifacts/minimax/request.json` | MiniMax 请求记录（密钥已脱敏） |
| `artifacts/minimax/response.json` | MiniMax 响应或真实错误载荷 |
| `artifacts/reference/applied.json` | 「改动后」代码的来源标注（reference）与其原因 |
| `artifacts/reference/code.diff` | 参考实现应用到工作区的 diff |
| `artifacts/progress-written.json` | 写入 Atomic Memory 的改动前进度条目 |
| `artifacts/progress-written-after.json` | 写入 Atomic Memory 的改动后验证条目 |
| `artifacts/tests/before*.txt` | 改动前的三组测试输出 |
| `artifacts/tests/after*.txt` | 改动后的三组测试输出 |
| `artifacts/snapshots/before/` `after/` | 改动前后的代码快照 |
| `artifacts/restart/restart-proof.{json,md}` | 重启持久化验证记录 |
| `artifacts/spec-revision/spec-revision-proof.json` | 规格修订验证记录 |
| `artifacts/spec-revision/report-before.md` `report-after.md` | 修订前后的检索报告 |
