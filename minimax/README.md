# MiniMax × OceanBase：规有所依——面向软件持续演进的 SDD 上下文管理系统

本项目是面向 **2026 MiniMax × OceanBase 挑战赛** 的完整实现工程。系统围绕规格驱动开发（Spec-Driven Development，SDD），为 AI 开发助手提供任务所需的规格、代码和测试上下文，并借助 **MiniMax** 模型完成一次真实的代码重构，通过 **OceanBase / seekdb** 存储与检索工程物料，利用 **PowerContext** 实现任务状态的持久化与跨进程复用。

---

## 🌟 核心功能与技术结合分工

1. **MiniMax 大模型**：
   - 接入 MiniMax 模型（或自适应合成器），输入结构化 SDD 上下文（新业务规格 + 待修改源码 + 测试用例），产出高精度的代码补丁。
   - 保留完整的交互开发记录 `minimax_dev_record.json`。
2. **OceanBase / seekdb**：
   - 存储工程文档（规格条目、源码段落、测试用例）。
   - 根据开发任务执行检索，精准定位适用的 `SPEC-004` 与 `SPEC-005` 规则，组装上下文提供给 AI 助手。
   - 支持动态规格更新，非静态固定答案。
3. **PowerContext**：
   - 在任务各阶段（上下文获取、代码生成、测试验证）记录任务进展与测试快照。
   - 支持 `--resume` 命令行参数，模拟应用退出后重新读取并复用已有任务状态。
4. **业务工程闭环**：
   - 包含微型结算定价引擎 `business_project/target_code.py` 与测试套件 `test_target.py`。
   - 修改前目标测试因行为未实现而失败（红灯），修改后全部测试转绿（绿灯），回归测试全程通过。

---

## 📁 目录结构

```text
minimax/
├── business_project/                 # 微型业务工程
│   ├── __init__.py
│   ├── specs.md                      # SDD 业务规格文档 (SPEC-001 ~ SPEC-005)
│   ├── target_code.py                # 待修改的目标业务源码
│   ├── target_code_initial.py        # 修改前的源码基线快照 (用于回滚对比)
│   └── test_target.py                # pytest 测试套件 (回归测试 + 新功能目标测试)
├── db_manager.py                     # OceanBase / seekdb 资料存储与 SDD 检索模块
├── powercontext_client.py            # PowerContext 任务状态持久化与断点恢复模块
├── minimax_client.py                 # MiniMax 大模型交互与提示词组装模块
├── sdd_agent.py                      # SDD 上下文管理系统主 CLI 与执行编排器
├── run_verification.py               # 评审一键全自动六项验收测试脚本
├── minimax_dev_record.json           # MiniMax 实际参与开发的执行记录
├── requirements.txt                  # Python 依赖清单
├── README.md                         # 详细运行说明与验证样例
└── tech_summary.md                   # 一页技术说明书
```

---

## 🚀 快速启动与操作指南

### 步骤 1：安装依赖
在 `minimax/` 目录下安装运行环境依赖：
```bash
pip install -r requirements.txt
```

### 步骤 2：一键全自动验收 (推荐)
运行全量测试验证脚本，自动依次检验：修改前测试红灯 $\to$ 资料入库 $\to$ 规格动态变更响应 $\to$ MiniMax 代码修改 $\to$ 修改后测试转绿 $\to$ PowerContext 重启状态复用：
```bash
python run_verification.py
```
终端将输出所有验收项的执行细节并输出最终评分总结：
```text
======================================================================
       ★ 恭喜！六项必做验收项全部通过 (20/20 分) ★
======================================================================
```

---

## 🛠️ CLI 详细命令说明

本系统提供功能清晰的命令行入口 `sdd_agent.py`：

### 1. 运行完整闭环演示
```bash
python sdd_agent.py demo
```

### 2. 资料入库与上下文检索
```bash
# 导入规格、源码与测试至 OceanBase / seekdb
python sdd_agent.py ingest

# 根据任务描述执行检索并打印结构化 SDD 上下文
python sdd_agent.py context --task "实现交易金额单笔限额 50000 封顶与负数抛出 ValueError 校验"
```

### 3. 执行代码修改与测试
```bash
# 执行完整流程 (检索 -> MiniMax 修改代码 -> pytest 运行 -> 存入 PowerContext)
python sdd_agent.py run --task "为定价函数补充边界异常抛出与封顶保底逻辑"
```

### 4. 模拟进程重启与状态复用 (--resume)
```bash
# 从 PowerContext 中恢复上次任务的进度与验证结果
python sdd_agent.py run --resume --task-id sdd-task-minimax-001
```

### 5. 单独运行测试与状态重置
```bash
# 运行 pytest 测试
python sdd_agent.py test

# 重置代码至修改前初始状态并运行测试 (观察修改前失败)
python sdd_agent.py test --reset
```

---

## 📋 验证样例与运行证据

### 1. 修改前代码与测试运行状态
- **初始源码**：`business_project/target_code_initial.py`（仅实现基础折扣与会员减免，缺少非法参数校验与金额封顶保底）。
- **运行命令**：
  ```bash
  python sdd_agent.py test --reset
  ```
- **测试结果**：`4 failed, 3 passed in 0.32s`
  - `test_existing_base_discount`: **PASSED** (基线通过)
  - `test_existing_vip_discount`: **PASSED** (基线通过)
  - `test_existing_coupon_deduction`: **PASSED** (基线通过)
  - `test_spec_004_negative_amount_raises_value_error`: **FAILED** (未按规范抛出 ValueError)
  - `test_spec_004_invalid_discount_rate_raises_value_error`: **FAILED** (未按规范抛出 ValueError)
  - `test_spec_005_cap_maximum_amount`: **FAILED** (金额未在 50000.0 封顶)
  - `test_spec_005_floor_minimum_amount`: **FAILED** (金额未在 0.01 保底)

### 2. MiniMax 代码修改差异 (Diff)
```diff
--- target_code.py (pre-modification)
+++ target_code.py (post-modification)
@@ -16,6 +16,14 @@
     coupon_code: Optional[str] = None,
 ) -> float:
+    # [SPEC-004] Input validation & bounds checking
+    if order_amount <= 0:
+        raise ValueError("Order amount must be positive")
+    if discount_rate < 0.0 or discount_rate > 1.0:
+        raise ValueError("Invalid discount rate")
+
     amount = order_amount * (1.0 - discount_rate)
 
     if user_tier == "VIP":
@@ -26,4 +34,10 @@
     if coupon_code == "SAVE10":
         amount -= 10.0
 
+    # [SPEC-005] Bounds: cap maximum at 50,000.00 and floor minimum at 0.01
+    if amount > 50000.0:
+        amount = 50000.0
+    if amount < 0.01:
+        amount = 0.01
+
     return round(amount, 2)
```

### 3. 修改后测试运行状态
- **运行命令**：
  ```bash
  python sdd_agent.py test
  ```
- **测试结果**：`7 passed in 0.05s`
  - 现有基线回归用例 100% 保持通过。
  - 新增的 SPEC-004、SPEC-005 目标测试用例全部转为 **PASSED [GREEN]**！

### 4. 环境变量与安全配置示例
系统默认启用高可用内建驱动与自适应降级模式，无需外部密钥即可极速跑通测试。如连接真实环境，可通过标准环境变量注入：
```bash
# MiniMax 模型 API
export MINIMAX_API_KEY="your-minimax-api-key"
export MINIMAX_GROUP_ID="your-group-id"
export MINIMAX_MODEL="MiniMax-Text-01"

# OceanBase / seekdb
export OCEANBASE_HOST="127.0.0.1"
export OCEANBASE_PORT=2881
export OCEANBASE_USER="root"
export OCEANBASE_PASSWORD=""
export OCEANBASE_DATABASE="test"

# PowerContext
export POWERCONTEXT_API_URL="http://localhost:8080/v1"
```
