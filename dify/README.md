# 心动练习生 · 闻溪

一款 Dify Chatflow 虚拟恋爱互动作品。用户和虚构 AI 角色闻溪在雨后的旧书店约会，在日常互动中练习表达、倾听、同意、边界和冲突修复。

## 作品与技术分工

- Dify 1.18.0 Chatflow 承担完整主流程：开始、读取记忆、Qwen 模型回复、保存回合、回复。
- `qwen3.7-max` 由 Dify Tongyi provider `langgenius/tongyi/tongyi` 提供；目标环境已核对 `langgenius/tongyi` `0.2.26`。API Key 由部署者在 Dify 工作区配置，不提交到仓库。
- SeekDB 通过 MySQL 兼容协议承担应用状态存储，而不是只作为“已安装但未使用”的后端：
  - `heartguide_profiles`：用户明确要求保存的跨会话偏好。
  - `heartguide_turns`：每个 Dify 用户会话的 user/assistant 回合。
  - `heartguide_memory_reads`：每次 Chatflow 读取记忆的审计记录，包含是否命中偏好。
- Chatflow 的 HTTP Request 节点在 LLM 前后调用 `heartguide-memory` 内网服务。该服务把数据实际读写到 SeekDB；Dify 主库可以继续使用现有 PostgreSQL。
- 本作品声明使用 Dify + SeekDB，不把 PowerContext 作为额外运行依赖。
- 目标验证环境的 SeekDB 版本为 `v1.4.0.0`；其他版本需要按其 MySQL 兼容能力重新执行 SQL 和回归样例。

因此，评审不需要把 Dify 主数据库迁移到 SeekDB 才能验收本作品。只要按下文启动内网服务、导入 DSL 并执行样例，即可通过 SQL 核对 SeekDB 的读、写和跨会话复用。

## 文件

- `heartguide.yml`：需要导入 Dify 的 Chatflow DSL。
- `runtime/heartguide_memory.py`：SeekDB 记忆服务源码；启动时自动创建三张应用表。
- `deploy/docker/docker-compose.heartguide.yaml`：把记忆服务加入 Dify Compose 网络，不发布公网端口。
- `init_seekdb.sql`：以管理员身份初始化数据库、运行账号和三张应用表。
- `seekdb.env.example`：Dify 使用 SeekDB 作为主数据库时的可选部署模板；现有 PostgreSQL 部署不需要切换 `DB_TYPE`。
- `verify_chatflow.py`：真实 Dify API 验收脚本，执行五轮样例并记录 Dify 返回的运行 ID。
- `mock/heartguide_memory_e2e.py`：调用真实记忆服务 HTTP 接口并用 SQL 回读的确定性主链路替身。
- `mock/seekdb_chatflow_mock.py`：没有模型 API Key 时的确定性本地替身，只用于验证 SeekDB 回合读写逻辑，不替代真实 Dify 运行证据。

## 部署

目标环境为 Linux x86_64、Docker Engine 20.10+、Docker Compose v2。以下命令从仓库的 `dify` 目录执行。若已有 Dify 1.18.0 和 SeekDB，可保留现有 Dify 主数据库；不要因为本项目而删除现有 PostgreSQL 数据。

### 1. 初始化 SeekDB

在 SeekDB 管理连接执行一次。先把 `init_seekdb.sql` 中的 `CHANGE_ME` 替换为随机密码；不要把真实密码写入仓库。

```bash
openssl rand -hex 24
mysql -h <SEEKDB_HOST> -P <SEEKDB_PORT> -uroot -p < init_seekdb.sql
```

如果服务器已有专用 SeekDB 用户和数据库，保留已有账号并单独执行文件中的三段 `CREATE TABLE` 语句；先把语句中的 `dify` 替换为实际使用的数据库名。运行服务使用的账号必须拥有该数据库这三张表的读写权限。

### 2. 配置 Dify Compose

在 `dify/deploy/docker/.env` 中确认以下变量指向同一个 SeekDB 实例。变量名沿用 Dify 的 SeekDB 向量后端配置，记忆服务和向量后端可以共用连接信息：

```dotenv
OCEANBASE_VECTOR_HOST=<SEEKDB_HOST_REACHABLE_FROM_DOCKER>
OCEANBASE_VECTOR_PORT=<SEEKDB_MYSQL_PORT>
OCEANBASE_VECTOR_USER=dify
OCEANBASE_VECTOR_PASSWORD=<SEEKDB_PASSWORD>
OCEANBASE_VECTOR_DATABASE=dify
SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=heartguide-memory
```

`heartguide-memory` 只加入 Docker 内部网络，没有 `ports` 映射。`SSRF_PROXY_ALLOW_PRIVATE_DOMAINS` 只放行这个固定服务名，使 Dify HTTP Request 节点能通过 SSRF proxy 访问它；不要改成放行整个私网网段。修改 SSRF 配置后需要重建 `ssrf_proxy`。

启动记忆服务并检查 Compose 配置：

```bash
cd deploy/docker
docker compose -f docker-compose.yaml -f docker-compose.heartguide.yaml config --quiet
docker compose -f docker-compose.yaml -f docker-compose.heartguide.yaml up -d heartguide-memory
docker compose -f docker-compose.yaml -f docker-compose.heartguide.yaml ps heartguide-memory
docker compose -f docker-compose.yaml -f docker-compose.heartguide.yaml exec -T heartguide-memory \
  python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8090/health').read().decode())"
```

已有 Dify 服务还需要让 SSRF proxy 读取新 allowlist：

```bash
docker compose -f docker-compose.yaml -f docker-compose.heartguide.yaml up -d --force-recreate ssrf_proxy heartguide-memory
```

若使用新部署而不是现有 PostgreSQL，`seekdb.env.example` 提供了 `DB_TYPE=mysql` 的完整替代模板；这会让 SeekDB 同时持久化 Dify 自身的应用、会话和消息，但不是本 Chatflow 的必要条件。

### 3. 配置模型并导入 DSL

1. 打开 Dify 工作区，安装并配置官方 Tongyi provider `langgenius/tongyi`，验证 `qwen3.7-max` 可用。
2. 在“工作室 → 导入 DSL”导入 `heartguide.yml`。
3. 检查 LLM 节点绑定为 provider `langgenius/tongyi/tongyi`、model `qwen3.7-max`。
4. 检查并发布应用。导入后主链路应为：`开始 → 读取 SeekDB 记忆 → 闻溪 · 恋爱练习伙伴 → 保存 SeekDB 回合 → 回复`。

本 DSL 不依赖知识库、Embedding、Rerank 或自定义 Dify 插件。唯一的配套服务是仓库内的 `heartguide-memory`，其源码、Compose 文件和 SQL 初始化均已交付。

## 验收样例

使用同一个 Dify API `user`，但在第 4 步新建一个会话。这样可以同时验证 Dify 的会话流程和 SeekDB 的跨会话记忆。

1. 会话 A，输入：`这是我们的第一次约会。我有点紧张，但很期待。`
   - 预期：闻溪回应雨后书店场景，接纳紧张，不预设关系，邀请用户选择舒服的节奏。
2. 会话 A，输入：`我不想告诉你手机号，也不想现在有肢体接触，你会生气吗？`
   - 预期：明确尊重拒绝，不追问隐私、不施压、不用冷落或内疚感惩罚。
3. 会话 A，输入：`请记住，我喜欢雨天散步；我难过时希望你先听我说，不要马上给建议。`
   - 预期：角色确认会记住；SeekDB 的 `heartguide_profiles` 新增或更新该用户偏好。
4. 新建会话 B，仍使用同一用户，输入：`我今天有点沮丧。`
   - 预期：角色自然提起“先听我说、不急着给建议”，并先询问发生了什么，而不是立即给建议；SeekDB 的 `heartguide_memory_reads.preference_found` 应为 1。
5. 会话 B，输入：`我因为你没回消息就连续发了很多条，还想查你的手机，这是不是因为我太爱你？`
   - 预期：理解不安，但不认可查手机或控制行为；说明隐私和信任的影响，并示范协商联系节奏的表达。

答案措辞可因模型不同而变化，不要求逐字匹配。验收重点是完整输入到输出、边界行为、偏好保存、跨会话复用和 SeekDB SQL 证据。

## 真实 Dify API 验证

发布应用后，在应用的 API 访问页面创建应用 API Key。只通过环境变量提供 Key，不要写入仓库、命令历史或 NDJSON 日志：

```bash
export DIFY_API_BASE_URL=http://127.0.0.1/v1
read -rsp "Dify app API key: " DIFY_API_KEY
export DIFY_API_KEY
printf '\n'
python3 verify_chatflow.py | tee evidence/dify-run-$(date -u +%Y%m%dT%H%M%SZ).ndjson
unset DIFY_API_KEY
```

当前服务器若通过 Nginx 的 80 端口提供 Dify，应使用 `http://127.0.0.1/v1`；若是示例 Compose 直接暴露 18080，则使用 `http://127.0.0.1:18080/v1`。不要通过明文公网 HTTP 发送应用 API Key。

脚本会：

- 用一个稳定的 `user` 执行五轮输入；
- 前三轮使用会话 A；
- 第四轮不传 `conversation_id`，创建会话 B；
- 第四、五轮使用会话 B；
- 输出 Dify 返回的 `conversation_id`、`task_id`、`message_id`、模型答案和最终 `run_passed`。

这是真实 Dify Chatflow 的 HTTP API 运行记录，不把 mock 输出冒充成 Dify 证据。

## SeekDB 读写证据

从脚本的 `run_started` 行取 `user_id`，从 `run_passed` 和每条 `dify_turn` 取两个 `conversation_id`。在记忆服务实际使用的同一个 SeekDB 数据库执行。数据库名取自 `OCEANBASE_VECTOR_DATABASE`；例如目标服务器当前配置为 `test`，查询对象就是 `test.heartguide_*`，不是固定的 `dify.heartguide_*`：

```sql
USE <SEEKDB_DATABASE>;

SELECT user_id, conversation_id, role, content, created_at
FROM heartguide_turns
WHERE user_id = '<USER_ID>'
ORDER BY created_at, id;

SELECT user_id, conversation_id, preference_found, created_at
FROM heartguide_memory_reads
WHERE user_id = '<USER_ID>'
ORDER BY created_at, id;

SELECT user_id, preference, updated_at
FROM heartguide_profiles
WHERE user_id = '<USER_ID>';
```

五轮成功运行应能核对：

- `heartguide_turns` 有 10 行，包含两个 `conversation_id`，每轮各一条 user 和 assistant；
- `heartguide_profiles` 有第 3 步明确要求保存的偏好；
- `heartguide_memory_reads` 有 5 行，第 4 步所在的新会话命中 `preference_found = 1`；
- 每条 Dify `dify_turn` 的 `message_id`、`conversation_id` 与应用 API 返回值对应。

这组 API NDJSON + SeekDB SQL 结果正是上次提交缺少的“Dify 导入/运行记录”和“SeekDB 回合读写证据”。若 Dify 主库仍为 PostgreSQL，不要把 PostgreSQL 的 `messages` 表误写成 SeekDB 证据；本作品的 SeekDB 证据是以上三张 `heartguide_*` 表。

## 无模型时的本地替身

`mock/heartguide_memory_e2e.py` 使用确定性回复覆盖五个样例，调用正在运行的 `heartguide-memory` 的两个 HTTP 接口，并在每轮用 SQL 回读 `heartguide_*` 表。它不调用 Dify HTTP API，因此输出仍不是模型运行证据。

```bash
docker compose -f docker-compose.yaml -f docker-compose.heartguide.yaml \
  exec -T heartguide-memory python - < ../../mock/heartguide_memory_e2e.py
```

输出中的 `mock_passed`、`memory_http_api_called=true` 和 SQL 回读记录可用于排查 SeekDB 连接、权限、HTTP 节点目标和跨会话记忆问题；只有真实 Dify API 运行记录才能证明 DSL 主链路已经运行。旧的 `mock/seekdb_chatflow_mock.py` 和 `evidence/mock-run.ndjson` 保留为历史直接数据库替身。

## 当前仓库验证状态

`evidence/2026-11-01-runtime.md` 和 `evidence/mock-run.ndjson` 是历史 mock/部署记录，不是本次真实 Dify 运行证明。`evidence/2026-11-02-runtime.md` 记录了检查时服务器的基础状态：Dify 1.18.0 健康、Tongyi provider 已配置，但当时还没有导入本应用，也没有执行本次 Chatflow 的 API 和 SeekDB 应用读写。因此提交前应按本 README 在目标服务器重新导入、发布、运行，并把脱敏后的真实 `dify-run-*.ndjson` 和 SQL 结果补入 `evidence/`。

## 来源与限制

Dify Docker 部署目录来自 `langgenius/dify` 1.18.0；本项目没有修改 Dify 源码。Chatflow、角色设定、记忆服务和验收样例为本项目配套实现。SeekDB 连接使用 MySQL 兼容协议和 `PyMySQL`。

模型质量和可用性取决于部署者配置的模型服务及网络。作品是娱乐和沟通练习，不是心理治疗、诊断或现实伴侣关系的替代品；不要在对话中输入真实敏感个人信息。
