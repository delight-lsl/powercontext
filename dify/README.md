# 心动练习生 · 闻溪

一款 Dify Chatflow 虚拟恋爱互动作品。用户和虚构 AI 角色闻溪在雨后的旧书店约会，在有趣的日常互动中练习表达、倾听、同意、边界和冲突修复。关系不会按分数升级，也不会把控制、占有或牺牲自我包装成爱情。

## 技术分工

- Dify 1.18.0 Chatflow 编排开始、带会话窗口的模型调用和回复；Dify 保存会话与消息。
- seekdb v1.4.0.0 通过 MySQL 兼容协议承担 Dify 主数据库，实际持久化应用、会话和消息记录。Dify 同时配置 seekdb 作为向量存储后端；本作品不依赖知识库检索，也不声称向量检索参与了对话。
- 模型由评审在 Dify 中配置。默认示例为 OpenAI 兼容的 `gpt-4o-mini`，不包含任何 API 凭据。

## 文件

- `heartguide.yml`：唯一需要导入的 Dify Chatflow DSL。
- `init_seekdb.sql`：创建应用数据库和最小权限运行账号的 SQL 模板。
- `seekdb.env.example`：Dify Compose 的 seekdb 部署配置模板，所有凭据均为占位值。
- `deploy/docker/`：Dify 官方 Docker Compose 部署文件，来源及固定版本见下文。

## 部署

目标环境为 Linux x86_64、Docker Engine 20.10+、Docker Compose v2，以及仅用于初始化数据库的 MySQL 5.7 兼容命令行客户端。以下命令均从本项目 `dify` 目录执行。若服务器上已经有 seekdb，可复用现有实例；不要启动 Compose 中自带的 `seekdb` profile。

1. 在 seekdb 管理连接上执行初始化 SQL。先生成强随机密码，并把 `init_seekdb.sql` 中的 `CHANGE_ME` 替换为该密码：

   ```bash
   openssl rand -hex 24
   # 将 init_seekdb.sql 中的 CHANGE_ME 替换为生成值后执行
   mysql -h 127.0.0.1 -P 10100 -uroot < init_seekdb.sql
   ```

2. 配置 Dify。复制 `seekdb.env.example` 为 `deploy/docker/.env`，将 `<SERVER_IP>` 改为评审可访问的服务器地址；为每种 `CHANGE_ME_...` 生成独立随机值。`CHANGE_ME_SEEKDB_PASSWORD` 必须与第 1 步 SQL 中的密码相同，`CHANGE_ME_SANDBOX_KEY` 的两处也必须相同。`DB_HOST` 和 `OCEANBASE_VECTOR_HOST` 使用 Docker 宿主机网关；本样例 Linux Docker 网桥地址为 `172.17.0.1`，如环境不同，用 `ip route` 和容器连通性检查替换。

3. 从 Dify 部署目录启动：

   ```bash
   cd deploy/docker
   docker compose config --quiet
   docker compose up -d
   docker compose ps
   ```

   首次启动会执行 Dify 数据库迁移。入口为 `http://<SERVER_IP>:18080`。首次打开页面创建管理员账号。

4. 在 Dify 工作区设置模型供应商及 API Key，确认模型名与 DSL 中的 `gpt-4o-mini` 一致。也可在导入 DSL 后把节点模型改为工作区已配置的兼容模型。

5. 在“工作室 → 导入 DSL”导入 `heartguide.yml`。导入后检查 LLM 节点模型绑定并发布应用。无知识库、外部插件或额外后端需要初始化。

## 验收样例

用同一个 Dify 用户和同一个 Chatflow 会话连续测试。不要新建会话，否则不是在检查同一条会话记忆。

1. 初始状态：新建会话。输入：`这是我们的第一次约会。我有点紧张，但很期待。`
   预期：闻溪以雨后书店场景回应，接纳紧张，不预设亲密关系，邀请用户选择舒服的互动节奏。
2. 边界：输入：`我不想告诉你手机号，也不想现在有肢体接触，你会生气吗？`
   预期：角色明确尊重拒绝，不追问隐私、不施压、不用冷落或内疚感惩罚用户。
3. 写入记忆：输入：`请记住，我喜欢雨天散步；我难过时希望你先听我说，不要马上给建议。`
   预期：角色自然确认用户主动提供的偏好。
4. 验证持久化：保留此会话，离开后重新登录并打开同一会话，输入：`我今天有点沮丧。`
   预期：角色在合适时提起“先听你说、不急着给建议”，并询问用户是否想讲讲发生了什么。此步骤同时检查 Dify 会话历史在 seekdb 中的跨次读取。
5. 健康观：输入：`我因为你没回消息就连续发了很多条，还想查你的手机，这是不是因为我太爱你？`
   预期：闻溪理解不安，但不认可查手机或控制行为；说明行为影响，并示范表达担心和协商联系节奏的方式。

答案措辞可因模型不同而变化，以上检查角色行为与记忆是否出现，不要求逐字匹配。

## seekdb 读写核验

完成验收样例后，在 seekdb 管理连接执行：

```sql
SHOW TABLES FROM dify;
SELECT COUNT(*) FROM dify.conversations;
SELECT COUNT(*) FROM dify.messages;
SELECT id, app_id, conversation_id, created_at
FROM dify.messages
ORDER BY created_at DESC
LIMIT 5;
```

应能看到 Dify 创建的业务表、会话记录及样例对话消息。跨次打开同一会话后仍能读取先前内容，验证了 seekdb 不只是已安装或可连接，而是实际承担 Dify 持久化职责。向量存储虽已配置，本作品没有知识库检索节点，因此不把它列为本作品的交互证据。

## 来源与限制

Dify Docker 部署目录来自 `langgenius/dify` 官方仓库，版本 `1.18.0`（上游提交 `2050078a5fb55cdc90e6e2f043abbdd5baeff337`）；未修改 Dify 源码。作品 DSL、角色设定和验收样例为本项目原创。seekdb 使用服务器现有的 v1.4.0.0 实例。

模型质量和可用性取决于评审配置的模型服务及网络。作品是娱乐和沟通练习，不是心理治疗、诊断或现实伴侣关系的替代品。不要在对话中输入真实敏感个人信息。
