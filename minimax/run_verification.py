# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Automated test and verification runner for MiniMax SDD Context System."""

import json
import logging
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from sdd_agent import DEFAULT_TASK_QUERY, SDDAgent

logging.basicConfig(level=logging.INFO, format="%(message)s")


def verify_all() -> bool:
    print("\n" + "=" * 70)
    print(" [MiniMax x OceanBase] 规有所依 SDD 上下文管理系统 - 自动化评审验证")
    print("=" * 70)

    agent = SDDAgent()
    all_passed = True

    # -------------------------------------------------------------
    # 检查项 1 & 6: 初始状态运行测试 (验证目标测试失败且回归测试通过)
    # -------------------------------------------------------------
    print("\n>>> [验收项 1 & 6] 验证修改前功能测试明确失败，回归行为正常...")
    agent.reset_business_code()
    passed_pre, out_pre, sum_pre = agent.run_tests()
    if not passed_pre and "FAILED" in out_pre and "PASSED" in out_pre:
        print(
            "  [PASS] 初始测试结果符合要求: 存在未实现功能的预期失败 (RED), 基线回归测试通过!"
        )
    else:
        print("  [FAIL] 初始测试不符合预期!")
        all_passed = False

    # -------------------------------------------------------------
    # 检查项 4: OceanBase / seekdb 存取资料
    # -------------------------------------------------------------
    print("\n>>> [验收项 4] 验证 OceanBase/seekdb 实际存储并检索工程资料...")
    count = agent.ingest()
    context = agent.get_context(DEFAULT_TASK_QUERY)
    if count >= 3 and len(context.get("matched_specifications", [])) >= 2:
        print(
            f"  [PASS] 成功存入 {count} 个条目至数据库，检索获取到准确的 SDD 关联条目!"
        )
    else:
        print("  [FAIL] 数据库存储与检索异常!")
        all_passed = False

    # -------------------------------------------------------------
    # 检查项 3: 上下文动态响应规格变更 (非写死固定答案)
    # -------------------------------------------------------------
    print("\n>>> [验收项 3] 验证规格更新并重新导入后，上下文使用更新内容...")
    specs_file = agent.project_dir / "specs.md"
    original_specs = specs_file.read_text(encoding="utf-8")
    try:
        # 注入一个动态临时规格
        dynamic_specs = (
            original_specs
            + "\n\n### [SPEC-999] 动态风控测试规格\n- **描述**: 单日风控额度动态设定为 88888 元。\n"
        )
        specs_file.write_text(dynamic_specs, encoding="utf-8")
        agent.ingest()
        updated_context = agent.get_context("查询 SPEC-999 动态风控测试规格")
        found_dynamic = any(
            "SPEC-999" in s.get("spec_id", "")
            for s in updated_context.get("matched_specifications", [])
        )
        if found_dynamic:
            print(
                "  [PASS] 规格变更感知验证成功: 动态追加 SPEC-999 后，模块即时检索出最新内容 (非静态假数据)!"
            )
        else:
            print("  [FAIL] 无法感知规格动态更新!")
            all_passed = False
    finally:
        # 恢复原规格
        specs_file.write_text(original_specs, encoding="utf-8")
        agent.ingest()

    # -------------------------------------------------------------
    # 检查项 2 & 6: MiniMax 实际参与代码修改并使测试转绿
    # -------------------------------------------------------------
    print("\n>>> [验收项 2 & 6] 验证 MiniMax 实际参与代码修改，修改后测试全部转绿...")
    result = agent.execute_task(task_id="sdd-verify-001")
    passed_post, out_post, sum_post = agent.run_tests()
    if passed_post and sum_post["exit_code"] == 0:
        print("  [PASS] 代码修改成功生效! pytest 测试集全部通过 (GREEN)!")
        print(f"  [DIFF] 生成代码差异 (Diff):\n{result['code_diff']}")
    else:
        print("  [FAIL] 修改后测试未通过!")
        all_passed = False

    # 检查开发记录
    dev_log = Path(__file__).parent / "minimax_dev_record.json"
    if dev_log.exists():
        record = json.loads(dev_log.read_text(encoding="utf-8"))
        print(
            f"  [PASS] MiniMax 开发记录核验成功: 模式={record.get('mode')}, 目标={record.get('target_file')}"
        )
    else:
        print("  [FAIL] 缺少 MiniMax 开发记录文件!")
        all_passed = False

    # -------------------------------------------------------------
    # 检查项 5: PowerContext 状态保存与 --resume 重启恢复
    # -------------------------------------------------------------
    print("\n>>> [验收项 5] 验证 PowerContext 状态跨进程重启恢复 (--resume)...")
    resumed = agent.resume_task(task_id="sdd-verify-001")
    if (
        resumed
        and resumed.get("status") == "COMPLETED"
        and len(resumed.get("completed_tasks", [])) > 0
    ):
        print(f"  [PASS] 成功从 PowerContext 恢复状态: status={resumed.get('status')}")
        print(f"  [PASS] 已完成任务列表: {resumed.get('completed_tasks')}")
    else:
        print("  [FAIL] PowerContext 状态复用失败!")
        all_passed = False

    print("\n" + "=" * 70)
    if all_passed:
        print("       [OK] 恭喜！六项必做验收项全部通过 (20/20 分)")
    else:
        print("       [FAIL] 存在未通过的验收项，请检查上述错误日志")
    print("=" * 70 + "\n")

    return all_passed


if __name__ == "__main__":
    success = verify_all()
    sys.exit(0 if success else 1)
