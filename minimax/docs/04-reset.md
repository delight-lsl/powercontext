# 04 · 重置方法 (Reset)

本方案的所有状态都落在三个地方：**仓库工作区**、**PowerContext / seekdb 数据库**、
**`minimax/artifacts/` 产物目录**。下面的命令按从轻到重的顺序列出。

所有命令在仓库根目录 `PowerContext-minimax/` 执行。

---

## 1. 还原样例项目到改动前状态（最常用）

端到端脚本在运行结束时会自动还原工作区。手动还原：

```bash
cd minimax
.venv/Scripts/python.exe -m sddctx apply --help   # 查看用法
# 直接用快照覆盖：
rm -rf sample/src sample/specs sample/tests
cp -r artifacts/snapshots/before/src artifacts/snapshots/before/specs \
      artifacts/snapshots/before/tests sample/
```

核对（应输出 `no differences`）：

```bash
diff -r artifacts/snapshots/before/src sample/src
diff -r artifacts/snapshots/before/specs sample/specs
diff -r artifacts/snapshots/before/tests sample/tests
```

期望结果：改动前的 6 个功能测试失败、20 个回归测试通过：

```bash
cd sample && ../.venv/Scripts/python.exe -m pytest -q
# 6 failed, 20 passed
```

---

## 2. 清空产物目录

```bash
cd minimax
rm -rf artifacts
mkdir -p artifacts
```

产物会由 `scripts/run_e2e.py`、`scripts/prove_restart_persistence.py`、
`scripts/prove_spec_revision.py` 重新生成。

---

## 3. 重置 PowerContext 作用域（只清语料与进度，保留数据库）

作用域是 PowerContext 里的一个命名空间。PowerContext **没有提供删除作用域的 HTTP 端点**
（`/v1/scopes/{scope_id}` 只支持 `GET` 与 `PUT`），因此重置直接在存储层完成：
删除该作用域在所有带 `scope_id` 列的表中的行，然后重新导入。

```bash
cd minimax
# 查看现有作用域
.venv/Scripts/python.exe scripts/reset.py --list-scopes

# 删除作用域并重新导入
.venv/Scripts/python.exe scripts/reset.py --scope minimax-sdd
# deleted scope scp_xxxxxxxx, recreated it as scp_yyyyyyyy and re-imported 47 records

# 清理验证脚本产生的临时作用域
.venv/Scripts/python.exe scripts/reset.py --prune-demo-scopes
```

验证脚本每次运行会新建一个带时间戳的临时作用域（`minimax-sdd-restart-proof-<ts>`、
`minimax-sdd-spec-revision-<ts>`），用 `--prune-demo-scopes` 一次性清掉；
该命令同时清掉接口探测时留下的 `Probe` 作用域。

> 删除只针对指定的 `scope_id`，不影响 `powercontext_sdd` 库中的其它作用域。

---

## 4. 完全重置数据库

```bash
# 停止 PowerContext Server 后再操作
docker exec oceanbase-seekdb mysql -h127.0.0.1 -P2881 -uroot \
  -e "DROP DATABASE IF EXISTS powercontext_sdd;"

# 重新建库；PowerContext 首次启动会自行建表
docker exec oceanbase-seekdb mysql -h127.0.0.1 -P2881 -uroot \
  -e "CREATE DATABASE powercontext_sdd CHARACTER SET utf8mb4;"

# 重新启动 PowerContext Server
minimax/.venv/Scripts/powercontext.exe server run --env-file minimax/.runtime/server.env

# 重新导入
cd minimax && .venv/Scripts/python.exe -m sddctx ingest --project sample
```

彻底重建 seekdb 容器：

```bash
docker rm -f oceanbase-seekdb
docker run -d --name oceanbase-seekdb -p 2881:2881 oceanbase/seekdb:1.4.0
```

---

## 5. 清理运行时目录

`minimax/.runtime/` 已被 `.gitignore` 忽略，包含本机配置、服务器日志与密钥材料：

```bash
rm -rf minimax/.runtime/home minimax/.runtime/home-restart-proof
rm -f  minimax/.runtime/server-restart-proof.env minimax/.runtime/server-restart-proof.log
```

保留 `minimax/.runtime/server.env`（本机真实配置，不入库）。
若要完全重建：

```bash
cp minimax/env/server.env.example minimax/.runtime/server.env
# 编辑占位符
```

---

## 6. 清理 Python 缓存

```bash
find minimax -name "__pycache__" -type d -prune -exec rm -rf {} +
find minimax -name ".pytest_cache" -type d -prune -exec rm -rf {} +
```

---

## 7. 恢复到「干净克隆」状态

```bash
cd PowerContext-minimax
git status
# minimax/ 下的参赛资产是新增文件，尚未纳入版本控制时：
git clean -nd minimax/          # 先看会删什么
git clean -fd minimax/          # 确认后再执行（会删除 minimax/ 下所有未跟踪文件）
```

> **谨慎**：`git clean -fd minimax/` 会删除 `minimax/` 下所有未被 Git 跟踪的文件，
> 包括 `minimax/artifacts/` 中的验证证据。执行前先确认 `git status` 输出，
> 或先备份 `minimax/artifacts/`。

---

## 8. 一键重置脚本

`minimax/scripts/reset.py` 封装了上面最常用的几项：

```bash
cd minimax
.venv/Scripts/python.exe scripts/reset.py --help
.venv/Scripts/python.exe scripts/reset.py --list-scopes
.venv/Scripts/python.exe scripts/reset.py --scope minimax-sdd   # 重置作用域 + 重新导入
.venv/Scripts/python.exe scripts/reset.py --prune-demo-scopes    # 清理临时作用域
.venv/Scripts/python.exe scripts/reset.py --worktree             # 还原样例项目
.venv/Scripts/python.exe scripts/reset.py --artifacts            # 清空产物
.venv/Scripts/python.exe scripts/reset.py --database             # 重建数据库
```
