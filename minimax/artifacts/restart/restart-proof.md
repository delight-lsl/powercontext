# PowerContext 重启持久化验证记录 / Restart persistence proof

- 生成时间 / generated at: 2026-10-28T13:22:04.927069+00:00
- 验证作用域 / proof scope: `scp_5g953q9t9wxgr0x0czazvg3crr`
- 服务端口 / server port: 8011
- 重启前后进程号 / server pids: 15088 -> 47344

## 结论 / Verdict

| 检查项 / check | 结果 / result |
| --- | --- |
| `empty_state_confirmed` | PASS |
| `progress_survived_restart` | PASS |
| `server_process_was_restarted` | PASS |
| `progress_reused_in_next_context` | PASS |
| `passed` | PASS |

## 步骤 / Steps

### start-server-1

- 命令 / command: `powercontext server run --env-file D:\.hyper\project\OceanBase\PowerContext-minimax\minimax\.runtime\server-restart-proof.env --port 8011`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:21:53.795757+00:00
- 详情 / detail:
```json
{
  "pid": 15088,
  "server_info": {
    "schema_version": {
      "major": 1,
      "minor": 0
    },
    "product": "powercontext",
    "server_id": "cf07ba4d-6b18-49d2-929f-975ab929ead5",
    "package_version": "1.4.1.dev180+gcd1b5e3f5",
    "api_contract_version": {
      "major": 1,
      "minor": 4
    },
    "feature_contracts": {
      "access.principal": {
        "version": {
          "major": 1,
          "minor": 0
        },
        "operations": [
          "get_access_principal"
        ]
      },
      "scope.selection": {
        "version": {
          "major": 1,
          "minor": 0
        },
        "operations": [
          "list_scopes",
          "get_scope",
          "get_default_scope"
        ]
      },
      "memory.explicit": {
        "version": {
          "major": 1,
          "minor": 0
        },
        "operations": [
          "remember_memory",
          "search_memory",
          "get_memory_entry"
        ]
      }
    }
  },
  "instance_identity": {
    "service_instance_id": "0550d2eeb77c49d4bfceb2431f590e23",
    "service_version": "1.4.1.dev180+gcd1b5e3f5"
  }
}
```

### empty-state-before-write

- 命令 / command: `POST /v1/memory/entries/list (scope=scp_5g953q9t9wxgr0x0czazvg3crr)`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:21:57.294880+00:00
- 条目 / entries:

### ingest-corpus-into-proof-scope

- 命令 / command: `POST /v1/sources/content (scope=scp_5g953q9t9wxgr0x0czazvg3crr)`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:21:57.294880+00:00
- 详情 / detail:
```json
{
  "record_count": 47,
  "first_source_id": "sddctx:v1:spec_section:SPEC-BILL-001#背景-background:5d4baa04af01"
}
```

### write-progress

- 命令 / command: `POST /v1/memory/remember (scope=scp_5g953q9t9wxgr0x0czazvg3crr)`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:21:57.294880+00:00
- 写入 / written:
  - `progress` artifact=`art_74e6eaf60a4b4ce3bf3afeea5b804485` revision=1: SDD 任务进度：在 billing.pricing.compute_invoice_totals 中实现促销码折扣，折扣上限取自促销码目录，税额按折后金额计算。
  - `verification` artifact=`art_b94e2150447743a788fd25536e9ea909` revision=1: 验证结果：改动前目标功能测试失败 (promo 能力未实现)，回归测试通过；改动后目标功能测试与回归测试全部通过。

### read-back-before-restart

- 命令 / command: `POST /v1/memory/entries/list (scope=scp_5g953q9t9wxgr0x0czazvg3crr)`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:21:57.294880+00:00
- 条目 / entries:
  - `progress` artifact=`art_74e6eaf60a4b4ce3bf3afeea5b804485` revision=1 state=active: SDD 任务进度：在 billing.pricing.compute_invoice_totals 中实现促销码折扣，折扣上限取自促销码目录，税额按折后金额计算。
  - `verification` artifact=`art_b94e2150447743a788fd25536e9ea909` revision=1 state=active: 验证结果：改动前目标功能测试失败 (promo 能力未实现)，回归测试通过；改动后目标功能测试与回归测试全部通过。

### sql-check-before-restart

- 命令 / command: `docker exec oceanbase-seekdb mysql ... SELECT ... FROM powercontext_sdd.pc_atomic_memory_current`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:21:57.444154+00:00
- 数据库直查 / direct SQL rows:
```
art_74e6eaf60a4b4ce3bf3afeea5b804485	1	progress
art_b94e2150447743a788fd25536e9ea909	1	verification
```

### stop-server

- 命令 / command: `kill -TERM 15088`
- 退出码 / exit code: `1`
- 时间 / timestamp: 2026-10-28T13:21:57.446794+00:00
- 详情 / detail:
```json
{
  "stopped": true,
  "signal": "SIGTERM",
  "exit_code": 1,
  "pid": 15088,
  "started_at": "2026-10-28T13:21:46.145726+00:00",
  "stopped_at": "2026-10-28T13:21:57.446794+00:00"
}
```

### start-server-2

- 命令 / command: `powercontext server run --env-file D:\.hyper\project\OceanBase\PowerContext-minimax\minimax\.runtime\server-restart-proof.env --port 8011`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:22:04.591327+00:00
- 详情 / detail:
```json
{
  "pid": 47344,
  "server_info": {
    "schema_version": {
      "major": 1,
      "minor": 0
    },
    "product": "powercontext",
    "server_id": "cf07ba4d-6b18-49d2-929f-975ab929ead5",
    "package_version": "1.4.1.dev180+gcd1b5e3f5",
    "api_contract_version": {
      "major": 1,
      "minor": 4
    },
    "feature_contracts": {
      "access.principal": {
        "version": {
          "major": 1,
          "minor": 0
        },
        "operations": [
          "get_access_principal"
        ]
      },
      "scope.selection": {
        "version": {
          "major": 1,
          "minor": 0
        },
        "operations": [
          "list_scopes",
          "get_scope",
          "get_default_scope"
        ]
      },
      "memory.explicit": {
        "version": {
          "major": 1,
          "minor": 0
        },
        "operations": [
          "remember_memory",
          "search_memory",
          "get_memory_entry"
        ]
      }
    }
  },
  "instance_identity": {
    "service_instance_id": "60e84d3d1c174aeba42722d8a7e274a0",
    "service_version": "1.4.1.dev180+gcd1b5e3f5"
  }
}
```

### read-back-after-restart

- 命令 / command: `POST /v1/memory/entries/list (scope=scp_5g953q9t9wxgr0x0czazvg3crr)`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:22:04.669615+00:00
- 条目 / entries:
  - `progress` artifact=`art_74e6eaf60a4b4ce3bf3afeea5b804485` revision=1 state=active: SDD 任务进度：在 billing.pricing.compute_invoice_totals 中实现促销码折扣，折扣上限取自促销码目录，税额按折后金额计算。
  - `verification` artifact=`art_b94e2150447743a788fd25536e9ea909` revision=1 state=active: 验证结果：改动前目标功能测试失败 (promo 能力未实现)，回归测试通过；改动后目标功能测试与回归测试全部通过。

### sql-check-after-restart

- 命令 / command: `docker exec oceanbase-seekdb mysql ... SELECT ... FROM powercontext_sdd.pc_atomic_memory_current`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:22:04.869087+00:00
- 数据库直查 / direct SQL rows:
```
art_74e6eaf60a4b4ce3bf3afeea5b804485	1	progress
art_b94e2150447743a788fd25536e9ea909	1	verification
```

### reuse-progress-in-next-context

- 命令 / command: `python -m sddctx query --task '实现发票促销码折扣并保持金额不变量'`
- 退出码 / exit code: `0`
- 时间 / timestamp: 2026-10-28T13:22:04.924292+00:00
- 复用上下文 / reused progress: corpus=47 files_to_modify=['src/billing/pricing.py'] spec_sections=12 functional_tests=6 regression_tests=20
  - `progress` SDD 任务进度：在 billing.pricing.compute_invoice_totals 中实现促销码折扣，折扣上限取自促销码目录，税额按折后金额计算。
  - `verification` 验证结果：改动前目标功能测试失败 (promo 能力未实现)，回归测试通过；改动后目标功能测试与回归测试全部通过。

### stop-server-2

- 命令 / command: `kill -TERM`
- 退出码 / exit code: `1`
- 时间 / timestamp: 2026-10-28T13:22:04.927069+00:00
- 详情 / detail:
```json
{
  "stopped": true,
  "signal": "SIGTERM",
  "exit_code": 1,
  "pid": 47344,
  "started_at": "2026-10-28T13:21:57.452977+00:00",
  "stopped_at": "2026-10-28T13:22:04.927069+00:00"
}
```

## 说明 / Notes

- 服务进程收到 SIGTERM 后以退出码 1 结束，这是 PowerContext Server 自身的关闭行为，不代表数据丢失；数据保存在 seekdb 的 `pc_atomic_memory_current` 表中。
- `server_id` 与 `service_instance_id` 中的服务实例号在重启后改变，证明服务确实被重新启动，而不是复用了旧进程。
