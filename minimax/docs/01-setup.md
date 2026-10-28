# 01 · 环境搭建与配置 (Setup, dependencies and configuration)

本文给出从零开始复现本方案的完整步骤。所有命令都在仓库根目录 `PowerContext-minimax/` 下执行，
参赛资产全部位于 `minimax/` 子目录。

---

## 1. 依赖与版本 (Dependencies and versions)

### 1.1 运行环境

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 11 10.0.26200 (x64) | 也可在 Linux / macOS 上复现 |
| Python | 3.12.14 | 由 `uv` 创建的虚拟环境 `minimax/.venv` |
| uv | 0.12.15 | 虚拟环境与依赖安装工具 |
| Docker | Desktop / Engine | 仅用于运行 seekdb |
| OceanBase seekdb | `1.4.0-100000172026082615`（镜像 `oceanbase/seekdb:1.4.0`） | `VERSION()` 返回 `5.7.25-OceanBase seekdb-v1.4.0.0` |
| PowerContext | `1.4.1.dev180+gcd1b5e3f5` | 以可编辑模式安装自本仓库 `src/powercontext` |

### 1.2 Python 依赖

`minimax/.venv` 中共 132 个发行包。与本方案直接相关的：

| 包 | 版本 | 用途 |
| --- | --- | --- |
| `powercontext` | 1.4.1.dev180+gcd1b5e3f5 | 上下文存储服务与官方 Python SDK |
| `sqlalchemy` | 2.1.6 | PowerContext 的异步 ORM 层 |
| `pyobvector` | 0.2.29 | OceanBase 官方异步方言 `mysql+aoceanbase` |
| `httpx` | 0.28.1 | 访问 MiniMax API |
| `fastapi` | 0.162.0 | PowerContext Server 的 HTTP 层 |
| `uvicorn` | 0.54.0 | PowerContext Server 的 ASGI 服务器 |
| `pydantic` | 2.14.1 | PowerContext 的数据模型 |
| `pytest` | 9.2.0 | 样例项目测试框架 |
| `typer` | 0.27.3 | PowerContext CLI |

完整清单：

```bash
uv pip list --python minimax/.venv/Scripts/python.exe
```

---

## 2. 安装步骤 (Installation)

### 2.1 创建虚拟环境

```bash
cd PowerContext-minimax
uv venv --python 3.12 minimax/.venv
```

### 2.2 安装 PowerContext 与依赖（可编辑模式）

本仓库的 `src/powercontext` 就是 PowerContext 官方源码，以可编辑模式安装后，
`powercontext` 命令与 `import powercontext` 都指向本仓库源码。

```bash
uv pip install --python minimax/.venv/Scripts/python.exe -e .
```

> **网络说明**：若 `pypi.org` 不可达，改用镜像源，例如
> `uv pip install --python minimax/.venv/Scripts/python.exe -e . --index-url https://mirrors.aliyun.com/pypi/simple/`
> （`mirrors.cloud.tencent.com/pypi/simple/` 同样可用）。

安装完成后校验：

```bash
minimax/.venv/Scripts/python.exe -c "from importlib.metadata import version; print(version('powercontext'))"
# 1.4.1.dev180+gcd1b5e3f5
minimax/.venv/Scripts/powercontext.exe --help
```

### 2.3 准备数据库 (seekdb)

```bash
docker run -d --name oceanbase-seekdb -p 2881:2881 oceanbase/seekdb:1.4.0
```

等待就绪并确认版本：

```bash
docker exec oceanbase-seekdb mysql -h127.0.0.1 -P2881 -uroot -N -e "SELECT VERSION();"
# 5.7.25-OceanBase seekdb-v1.4.0.0
```

创建 PowerContext 使用的库（PowerContext 首次启动会自行建表，这里只是显式创建）：

```bash
docker exec oceanbase-seekdb mysql -h127.0.0.1 -P2881 -uroot \
  -e "CREATE DATABASE IF NOT EXISTS powercontext_sdd CHARACTER SET utf8mb4;"
```

### 2.4 配置并启动 PowerContext Server

仓库中只提交**占位值**模板：`minimax/env/server.env.example`。
真实配置复制到被忽略的 `minimax/.runtime/server.env`：

```bash
cp minimax/env/server.env.example minimax/.runtime/server.env
```

编辑 `minimax/.runtime/server.env`，把占位符替换为自己的值：

```dotenv
POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1
POWERCONTEXT_SERVER_HTTP_PORT=8010
POWERCONTEXT_SERVER_DATABASE_KIND=oceanbase
POWERCONTEXT_SERVER_DATABASE_URL=mysql+aoceanbase://<db-user>:<db-password>@<db-host>:<db-port>/<db-name>?charset=utf8mb4
POWERCONTEXT_SERVER_ACCESS_MODE=disabled
POWERCONTEXT_SERVER_MCP_ENABLED=false
POWERCONTEXT_SERVER_DASHBOARD_ENABLED=false
POWERCONTEXT_HOME=<powercontext-home>
```

配置说明：

| 变量 | 说明 |
| --- | --- |
| `POWERCONTEXT_SERVER_DATABASE_KIND` | `oceanbase` 同时适用于 OceanBase MySQL 模式集群与独立 seekdb 1.4。PowerContext 通过 `VERSION()` 识别独立 seekdb 构建并跳过多租户兼容校验。 |
| `POWERCONTEXT_SERVER_DATABASE_URL` | **必须**使用 `mysql+aoceanbase` 协议头（官方异步 OceanBase 方言），必须带显式端口，必须带 `charset=utf8mb4`。 |
| `POWERCONTEXT_SERVER_ACCESS_MODE` | `disabled` 表示仅监听回环地址且不鉴权（本地验证脚本使用）。共享部署请改为 `enforced` 并设置 `POWERCONTEXT_SERVER_AUTH_TOKEN`。 |
| `POWERCONTEXT_HOME` | 运行时数据目录（游标签名密钥等）。**必须写成正斜杠路径**：环境文件解析器会把反斜杠当转义符处理。 |

启动：

```bash
minimax/.venv/Scripts/powercontext.exe server run \
  --env-file minimax/.runtime/server.env
```

校验：

```bash
curl -s http://127.0.0.1:8010/v1/server-info
# {"product":"powercontext","package_version":"1.4.1.dev180+gcd1b5e3f5", ...}
```

### 2.5 配置 MiniMax

密钥**只**从环境变量读取，不写入任何被提交的文件：

```bash
# Windows 用户级环境变量（推荐，重开终端后生效）
setx MINIMAX_API_KEY "<your-key>"
```

也可以只对当前会话生效：

```bash
export MINIMAX_API_KEY="<your-key>"      # Linux / macOS
$env:MINIMAX_API_KEY="<your-key>"         # PowerShell
```

`minimax/sddctx/minimax_client.py` 的 `resolve_api_key()` 依次读取进程环境变量与
Windows 用户注册表 `HKCU\Environment\MINIMAX_API_KEY`，**从不**把密钥写入日志或产物。

可选环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MINIMAX_BASE_URL` | `https://api.minimaxi.com/v1` | 国内站地址。国际站请改为 `https://api.minimax.io/v1`；两种密钥不通用。 |
| `MINIMAX_MODEL` | `MiniMax-M3` | 可用模型为 `MiniMax-M3` 与 `MiniMax-M3.1-Flash`。 |
| `MINIMAX_API_STYLE` | `openai` | 设为 `anthropic` 时改用 `<base>/anthropic/v1/messages`。 |
| `MINIMAX_TIMEOUT` | `180` | 单次请求超时（秒）。 |

### 2.6 初始化：导入语料 (Initialization)

```bash
cd minimax
.venv/Scripts/python.exe -m sddctx ingest --project sample \
  --json artifacts/ingest-report.json
```

期望输出：

```
ingested 47 records into scope scp_... (12 spec sections, 9 code symbols, 26 test cases)
```

---

## 3. 常用命令速查 (Command reference)

| 目的 | 命令 |
| --- | --- |
| 导入项目 | `python -m sddctx ingest --project sample` |
| 检索开发上下文 | `python -m sddctx query --task "为发票总价增加促销码折扣能力"` |
| 渲染发给模型的提示词 | `python -m sddctx prompt --task "..." --out prompt.md` |
| 写入进度 | `python -m sddctx state save --kind progress --text "..."` |
| 读取进度 | `python -m sddctx state list` |
| 检索进度 | `python -m sddctx state search --text "回归"` |
| 调用 MiniMax 生成代码 | `python -m sddctx minimax --task "..." --apply` |
| 应用已保存的模型回答 | `python -m sddctx apply --response artifacts/minimax/response.json --project sample` |
| 端到端验证 | `python scripts/run_e2e.py` |
| 重启持久化验证 | `python scripts/prove_restart_persistence.py` |
| 规格修订验证 | `python scripts/prove_spec_revision.py` |

所有命令都支持 `--server-url`（PowerContext Server 地址，默认 `http://127.0.0.1:8010`）
与 `--scope-title`（作用域标题，默认 `minimax-sdd`）。
