# Dify × OceanBase: 记仇的赛博酒保 NPC (基于 PowerContext 记忆上下文)

本项目是面向 **Dify × OceanBase（PowerContext）** 赛题的趣味智能体应用后端服务。

---

## 1. 创意与流程说明

### 1.1 玩法简述（100字以内）
玩家作为夜之城酒吧的顾客与仿生人酒保“老鲍勃”对话。底层借助 **OceanBase PowerContext** 跨轮次持久化玩家的“点单偏好”、“冒犯与恶作剧记录”。酒保会依据黑历史动态改变态度，从热情待客演变为记仇讽刺，累计 3 次恶搞后将拔出等离子霰弹枪下逐客令！

### 1.2 Dify 接入与数据协议
未来接入 Dify 时，在 Dify Workflow / Chatflow 中仅需添加一个 **HTTP Request 节点**：
- **请求方式**：`POST http://<server_ip>:8000/chat`
- **请求体（JSON）**：
  ```json
  {
    "user_id": "{{#sys.user_id#}}",
    "message": "{{#sys.query#}}"
  }
  ```
- **返回结构（JSON）**：
  ```json
  {
    "reply": "酒保老鲍勃的个性化台词",
    "character_state": {
      "attitude": "friendly | alert | spiteful | blacklist",
      "offense_count": 0,
      "orders": ["螺丝起子", "机油马天尼"],
      "last_offense": "说过【退钱】挑衅酒保",
      "bartender_notes": "老鲍勃在小本本上记录的客人特征"
    },
    "powercontext_synced": true
  }
  ```
在 Dify 后续节点中，可直接使用 `{{#http_request.body.reply#}}` 作为输出展示给玩家。

---

## 2. 快速启动

### 2.1 安装依赖
```bash
pip install -r requirements.txt
```

### 2.2 启动服务
```bash
python app.py
```
服务将在 `http://0.0.0.0:8000` 启动，并自动暴露交互文档：`http://localhost:8000/docs`。

---

## 3. 评委验证样例 (连续 4 轮测试用例)

### 步骤 1：初始化玩家记忆 (`POST /init`)
```bash
curl -X POST http://localhost:8000/init \
  -H "Content-Type: application/json" \
  -d '{"user_id": "player_001"}'
```
**预期返回**：
```json
{
  "code": 200,
  "message": "玩家 [player_001] 的记忆档案已在 PowerContext 中初始化成功！酒保老鲍勃已洗净杯子等待接待。",
  "powercontext_synced": false,
  "initial_state": {
    "user_id": "player_001",
    "orders": [],
    "offense_count": 0,
    "attitude": "friendly",
    "last_offense": "",
    "bartender_notes": "初次光临的新客人，系统扫描未发现不良记录。"
  }
}
```

---

### 步骤 2：第 1 轮 - 正常点单 (好感度为 0，态度友善 `friendly`)
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "player_001", "message": "老板，给我来一杯螺丝起子！"}'
```
**预期返回**：
```json
{
  "reply": "哈哈，欢迎光临！老鲍勃为您调制了一杯招牌【螺丝起子】。看你面善，给您加了双倍液氮，请慢用！",
  "character_state": {
    "user_id": "player_001",
    "orders": ["螺丝起子"],
    "offense_count": 0,
    "attitude": "friendly",
    "last_offense": "",
    "bartender_notes": "规规矩矩的体面客人，值得友善招待。"
  },
  "powercontext_synced": false
}
```

---

### 步骤 3：第 2 轮 - 首次冒犯 (冒犯次数=1，态度警惕 `alert`，记入黑名单)
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "player_001", "message": "呸，这酒真难喝，掺水假酒！"}'
```
**预期返回**：
```json
{
  "reply": "啧……我眼里的光学传感器还在闪红光呢。你刚才说过【难喝】挑衅酒保，老鲍勃可全记着。有话快说，有单快点，别找碴。",
  "character_state": {
    "user_id": "player_001",
    "orders": ["螺丝起子"],
    "offense_count": 1,
    "attitude": "alert",
    "last_offense": "说过【难喝】挑衅酒保",
    "bartender_notes": "有轻微不良记录：说过【难喝】挑衅酒保。需保持警惕。"
  },
  "powercontext_synced": false
}
```

---

### 步骤 4：第 3 轮 - 二次冒犯并再次点单 (冒犯次数=2，态度记仇 `spiteful`，回溯历史点单与前科)
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "player_001", "message": "再给我来一杯机油马天尼，不过你们店迟早倒闭，退钱！"}'
```
**预期返回**：
```json
{
  "reply": "呵！还敢点【机油马天尼】？之前点过【螺丝起子、机油马天尼】也就算了，你刚才不仅说过【退钱】挑衅酒保，还真把自己当大爷了？这杯收你三倍价格，喝不喝随你！",
  "character_state": {
    "user_id": "player_001",
    "orders": ["螺丝起子", "机油马天尼"],
    "offense_count": 2,
    "attitude": "spiteful",
    "last_offense": "说过【退钱】挑衅酒保",
    "bartender_notes": "累犯恶劣顾客！历史冒犯2次（包含：说过【退钱】挑衅酒保），列入重点防范对象。"
  },
  "powercontext_synced": false
}
```

---

### 步骤 5：第 4 轮 - 恶作剧升级 (冒犯次数=3，态度拉黑 `blacklist`，触发终极驱逐)
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "player_001", "message": "你们就是黑店垃圾，今天我就赖账不给钱！"}'
```
**预期返回**：
```json
{
  "reply": "【咔嚓！老鲍勃单手给重型等离子霰弹枪上了膛】‘警告！检测到无赖第 3 次挑衅！黑历史记录：说过【赖账】挑衅酒保！’ 本店不伺候你这种混蛋，三秒内从我的酒吧滚出去，否则别怪等离子弹不长眼！",
  "character_state": {
    "user_id": "player_001",
    "orders": ["螺丝起子", "机油马天尼"],
    "offense_count": 3,
    "attitude": "blacklist",
    "last_offense": "说过【赖账】挑衅酒保",
    "bartender_notes": "极度危险恶搞分子！冒犯次数达到 3 次，触发终极驱逐协议。"
  },
  "powercontext_synced": false
}
```

---

### 步骤 6：核验底层 PowerContext 记忆记录 (`GET /status/{user_id}`)
```bash
curl -X GET http://localhost:8000/status/player_001
```
**预期返回**：
```json
{
  "user_id": "player_001",
  "powercontext_endpoint": "http://localhost:8080/v1",
  "stored_memory": {
    "user_id": "player_001",
    "orders": [
      "螺丝起子",
      "机油马天尼"
    ],
    "offense_count": 3,
    "attitude": "blacklist",
    "last_offense": "说过【赖账】挑衅酒保",
    "bartender_notes": "极度危险恶搞分子！冒犯次数达到 3 次，触发终极驱逐协议。"
  }
}
```
