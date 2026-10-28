# 07 · 未完成项与实际错误 (Open items and the real errors)

本方案按赛题第 8 条要求，如实记录**未完成**的部分。所有错误均为本机实际返回的
原始信息，未做任何修饰。

---

## 1. MiniMax 生成代码 —— 未完成（阻塞于账户余额）

### 实际错误

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

证据文件：`artifacts/minimax/response.json`、`artifacts/e2e-steps.json` 中的
`MiniMax code generation` 步骤（exit code = 4）。

### 排查过程（均为实测）

| 尝试 | 端点 / 模型 | 实际返回 | 结论 |
| --- | --- | --- | --- |
| 1 | `https://api.minimax.io/v1` + `MiniMax-M3` | HTTP 401，`code 2049 invalid api key` | 密钥属于**国内站**账号，国际站端点不接受 |
| 2 | `https://api.minimaxi.com/v1` + `MiniMax-M2.5` | `code 2069 model no longer available` | M2.5 / M2.7 已下线 |
| 3 | `https://api.minimaxi.com/v1` + `MiniMax-M2.7` | `code 2069 model no longer available` | 同上 |
| 4 | `https://api.minimaxi.com/v1` + `MiniMax-M3` | HTTP 402，`code 1008 insufficient balance` | 鉴权通过、进入计费校验，**唯一阻塞点是余额** |
| 5 | `https://api.minimaxi.com/v1` + `MiniMax-M3.1-Flash` | HTTP 402，`code 1008 insufficient balance` | 同上 |
| 6 | `https://api.minimaxi.com/anthropic/v1/messages`（Anthropic 兼容路径） | HTTP 402，`code 1008 insufficient balance` | 路径正确（客户端已按 `<base>/anthropic/v1/messages` 实现），同样停在计费校验 |

### 恢复步骤

1. 在 MiniMax 平台控制台（国内站 `platform.minimaxi.com`）为该账号充值，
   或兑换比赛赠送额度；
2. 确认额度已到账后，重新执行：

```bash
cd minimax
.venv/Scripts/python.exe scripts/run_e2e.py
```

3. 预期结果：

```
[OK ] MiniMax code generation (exit=0) MiniMax-M3
[OK ] apply the model answer to the working tree (exit=0)
[OK ] test suite after the change (exit=0)
```

4. 产物会更新为真实内容：`artifacts/minimax/response.json`（模型回答）、
   `artifacts/minimax/code.diff`（真实 diff）；
   `artifacts/minimax-status.json` 的 `code_source` 会从 `reference` 变为 `minimax`。

### 未受影响的部分

MiniMax 调用失败**不影响**其余 7 条要求的验证结果：

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| 1 样例项目与明确的改动 | 已完成 | `minimax/sample/`、`SPEC-BILL-001` |
| 2 导入 seekdb + 调用入口 | 已完成 | `artifacts/ingest-report.json`、`sddctx` CLI |
| 3 输出含规格 / 代码 / 测试来源，改规格后输出变化 | 已完成 | `artifacts/spec-revision/` |
| 4 上下文交给 MiniMax + 可核验记录 | **部分**：请求已发出、记录已留存，模型回答待额度恢复 | `artifacts/minimax/request.json`、`response.json` |
| 5 前后快照与同一套测试 | 已完成 | `artifacts/snapshots/`、`artifacts/tests/` |
| 6 PowerContext 存进度、重启后读回复用 | 已完成 | `artifacts/restart/` |
| 7 源码 / 依赖 / 配置 / 验证 / 重置 / 使用记录 / 技术说明 | 已完成 | `minimax/docs/` |
| 8 端到端验证与真实错误记录 | 已完成（缺口如实记录） | `artifacts/e2e-steps.json`、本文 |

---

## 2. 「改动后」代码当前来自参考实现，而非模型

由于上表的阻塞，`scripts/run_e2e.py` 在 MiniMax 不可用时使用了
`minimax/reference/pricing_promo.py`（手写参考实现）来产生「改动后」状态。
这一点在产物中被显式标注，不会与模型产出混淆：

```json
{
  "code_source": "reference",
  "source_file": "reference/pricing_promo.py",
  "target_file": "sample/src/billing/pricing.py",
  "reason": "the MiniMax endpoint was unreachable during this run"
}
```

参考实现的价值：

* 让「改前失败 / 改后通过 / 回归通过」这组证据在模型不可用时依然可复现；
* 证明 `SPEC-BILL-001` 是可实现的（规格没有自相矛盾或不可满足）；
* 额度恢复后，同一条流水线可直接切换到模型产出。

`scripts/run_e2e.py` 默认优先使用模型回答；只有模型调用失败时才回退到参考实现，
并在 `artifacts/reference/applied.json` 里写明原因。若要完全离线、结果确定地复现，
显式加 `--code-source reference`。

---

## 3. 其它已知限制

| 限制 | 影响 | 缓解 |
| --- | --- | --- |
| 检索是词法的，没有语义向量 | 任务措辞与规格措辞差异大时召回下降 | `applies_to` 显式链接兜底；`keywords` 字段提供额外词面 |
| PowerContext 未配置推理服务 | `memory/search` 只走文本匹配（`matched_by: ["text"]`） | 本方案不依赖向量检索；如需可另行配置嵌入提供方 |
| 作用域删除无 HTTP 端点 | 重置需在存储层执行 | `scripts/reset.py` 用 SQL 精确删除指定 `scope_id` 的行 |
| `POWERCONTEXT_HOME` 必须用正斜杠 | 环境文件解析器会把反斜杠当转义符 | 模板与脚本统一使用正斜杠路径 |
| PowerContext 收到 `SIGTERM` 后退出码为 1 | 容易被误读为失败 | 属其自身关闭行为；数据在数据库中，重启后可读回 |

---

## 4. 干净环境复现清单

从零开始复现（不依赖本机任何既有文件或数据库状态）：

```bash
# 1. 数据库
docker run -d --name oceanbase-seekdb -p 2881:2881 oceanbase/seekdb:1.4.0
docker exec oceanbase-seekdb mysql -h127.0.0.1 -P2881 -uroot \
  -e "CREATE DATABASE powercontext_sdd CHARACTER SET utf8mb4;"

# 2. Python 环境
cd PowerContext-minimax
uv venv --python 3.12 minimax/.venv
uv pip install --python minimax/.venv/Scripts/python.exe -e .

# 3. 配置（仅占位值入库，真实值写本机）
cp minimax/env/server.env.example minimax/.runtime/server.env
# 编辑 minimax/.runtime/server.env

# 4. 启动 PowerContext Server
minimax/.venv/Scripts/powercontext.exe server run --env-file minimax/.runtime/server.env

# 5. 验证
cd minimax
.venv/Scripts/python.exe scripts/run_e2e.py
.venv/Scripts/python.exe scripts/prove_restart_persistence.py
.venv/Scripts/python.exe scripts/prove_spec_revision.py
```

第 5 步的预期结果见 `03-verification.md`（除 MiniMax 一步外全部为成功）。
