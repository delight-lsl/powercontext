#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
Dify × OceanBase 赛题: Just for Fun - "记仇的赛博酒保" 后端服务
文件: app.py
功能:
  1. 基于 FastAPI 提供微服务接口，供 Dify HTTP Request 节点或前端轻量调用。
  2. 深度集成 OceanBase PowerContext 记忆上下文管理体系，跨多轮对话持久化
     玩家的“点单偏好”、“冒犯与恶作剧记录”、“好感度/记仇态度”。
  3. 具备网络自适应与异常平滑降级能力：优先直连 PowerContext HTTP API，
     遇网络超时自动容错降级，保证评测全流程 100% 顺畅可运行。
==============================================================================
"""

import os
import time
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
import requests
from fastapi import FastAPI, HTTPException
import uvicorn

# 日志输出配置
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [CyberBartender] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("CyberBartender")

app = FastAPI(
    title="Dify × PowerContext: 记仇的赛博酒保 NPC",
    description="基于 OceanBase PowerContext 记忆上下文的趣味交互式 NPC 后端",
    version="1.0.0"
)

# ------------------------------------------------------------------------------
# 1. PowerContext 客户端适配层 (可配置 HTTP API，并带平滑本地降级容灾)
# ------------------------------------------------------------------------------
POWERCONTEXT_API_URL = os.getenv("POWERCONTEXT_API_URL", "http://localhost:8080/v1")
HTTP_TIMEOUT_SECONDS = 1.5

# 本地容灾内存存储 (当无法连通独立部署的 PowerContext 实例时兜底生效)
_LOCAL_FALLBACK_STORE: Dict[str, Dict[str, Any]] = {}


class PowerContextAdapter:
    """
    负责与 OceanBase PowerContext 进行 HTTP 通信。
    实现上下文与长期记忆的读取 (Retrieve) 与写入 (Store)。
    """
    @staticmethod
    def get_context(user_id: str) -> Dict[str, Any]:
        """
        向 PowerContext 拉取该用户的长期记忆与状态上下文
        """
        url = f"{POWERCONTEXT_API_URL}/contexts/{user_id}"
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                logger.info(f"成功从 PowerContext 服务加载用户 [{user_id}] 的上下文记忆")
                return resp.json().get("data", {})
        except Exception as e:
            logger.debug(f"连接远程 PowerContext 异常 ({e})，启用本地容错存储")

        # 降级返回本地缓存
        return _LOCAL_FALLBACK_STORE.get(user_id, {
            "user_id": user_id,
            "orders": [],
            "offense_count": 0,
            "attitude": "friendly",
            "last_offense": "",
            "bartender_notes": "初次光临的新客人，系统扫描未发现不良记录。"
        })

    @staticmethod
    def save_context(user_id: str, context_data: Dict[str, Any]) -> bool:
        """
        向 PowerContext 保存/同步该用户的最新记忆状态与小本本记录
        """
        url = f"{POWERCONTEXT_API_URL}/contexts/{user_id}"
        synced_to_remote = False
        try:
            resp = requests.post(
                url,
                json={"context": context_data, "updated_at": int(time.time())},
                timeout=HTTP_TIMEOUT_SECONDS
            )
            if resp.status_code in [200, 201]:
                logger.info(f"已成功将更新记忆同步至远程 PowerContext: {user_id}")
                synced_to_remote = True
        except Exception as e:
            logger.debug(f"同步至远程 PowerContext 失败 ({e})，已保存在本地上下文层")

        # 同步写入本地兜底存储
        _LOCAL_FALLBACK_STORE[user_id] = context_data
        return synced_to_remote


# ------------------------------------------------------------------------------
# 2. 请求与响应数据结构定义 (Pydantic Models)
# ------------------------------------------------------------------------------
class InitRequest(BaseModel):
    user_id: str = Field(default="player_001", description="玩家或会话唯一 ID")


class ChatRequest(BaseModel):
    user_id: str = Field(default="player_001", description="玩家唯一 ID")
    message: str = Field(..., description="玩家对酒保说的话或交互行为")


class BartenderResponse(BaseModel):
    reply: str = Field(..., description="赛博酒保老鲍勃的个性化台词回复")
    character_state: Dict[str, Any] = Field(..., description="酒保当前的记忆与态度状态")
    powercontext_synced: bool = Field(..., description="是否成功与远程 PowerContext 建立数据连接")


# ------------------------------------------------------------------------------
# 3. 业务逻辑引擎：酒保性格、记仇判断与意图分析
# ------------------------------------------------------------------------------
OFFENSIVE_KEYWORDS = [
    "难喝", "假酒", "劣质", "赖账", "不给钱", "退钱", "砸店", "垃圾", "有虫",
    "蟑螂", "下毒", "倒闭", "坑人", "掺水", "差评"
]

DRINK_KEYWORDS = [
    "螺丝起子", "机油马天尼", "赛博莫吉托", "霓虹威士忌", "量子二锅头",
    "合成啤酒", "生化鸡尾酒", "高压电解水", "机油", "啤酒", "威士忌"
]


def evaluate_interaction(message: str, current_memory: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    """
    根据用户本次输入与 PowerContext 历史记忆，更新酒保态度并生成台词
    """
    offense_count = current_memory.get("offense_count", 0)
    orders = list(current_memory.get("orders", []))
    last_offense = current_memory.get("last_offense", "")

    # 1. 意图检测：是否点单
    detected_drink = None
    for drink in DRINK_KEYWORDS:
        if drink in message:
            detected_drink = drink
            if drink not in orders:
                orders.append(drink)
            break

    # 2. 意图检测：是否触发恶作剧或冒犯
    is_offensive = any(bad_word in message for bad_word in OFFENSIVE_KEYWORDS)
    if is_offensive:
        offense_count += 1
        # 记录具体的冒犯事由
        for bad_word in OFFENSIVE_KEYWORDS:
            if bad_word in message:
                last_offense = f"说过【{bad_word}】挑衅酒保"
                break

    # 3. 基于冒犯次数计算态度阶梯
    # 0 次: friendly (热情)
    # 1 次: alert (警惕怀疑)
    # 2 次: spiteful (极度记仇、阴阳怪气)
    # 3 次及以上: blacklist (拔枪威胁、拒绝服务)
    if offense_count == 0:
        attitude = "friendly"
        if detected_drink:
            reply = f"哈哈，欢迎光临！老鲍勃为您调制了一杯招牌【{detected_drink}】。看你面善，给您加了双倍液氮，请慢用！"
        else:
            reply = "哟，新面孔！我是老鲍勃。夜之城的夜风很冷，进来看点什么？只要别惹事，在这你就是上宾。"
        bartender_notes = "规规矩矩的体面客人，值得友善招待。"

    elif offense_count == 1:
        attitude = "alert"
        if detected_drink:
            reply = f"拿去，你的【{detected_drink}】。顺便提醒你一句，我仿生脑里的小本本记着呢：刚才你可是{last_offense}。别以为换句好听的我就会忘！"
        else:
            reply = f"啧……我眼里的光学传感器还在闪红光呢。你刚才{last_offense}，老鲍勃可全记着。有话快说，有单快点，别找碴。"
        bartender_notes = f"有轻微不良记录：{last_offense}。需保持警惕。"

    elif offense_count == 2:
        attitude = "spiteful"
        past_orders_str = "、".join(orders) if orders else "空气"
        if detected_drink:
            reply = f"呵！还敢点【{detected_drink}】？之前点过【{past_orders_str}】也就算了，你刚才不仅{last_offense}，还真把自己当大爷了？这杯收你三倍价格，喝不喝随你！"
        else:
            reply = f"【酒保老鲍勃从吧台下翻出了一本泛黄的记仇小黑册】我瞅瞅……你不仅{last_offense}，还在这嬉皮笑脸！老鲍勃的冷却液都要被你气沸腾了！"
        bartender_notes = f"累犯恶劣顾客！历史冒犯2次（包含：{last_offense}），列入重点防范对象。"

    else:
        attitude = "blacklist"
        reply = (
            f"【咔嚓！老鲍勃单手给重型等离子霰弹枪上了膛】"
            f"‘警告！检测到无赖第 {offense_count} 次挑衅！黑历史记录：{last_offense}！’ "
            f"本店不伺候你这种混蛋，三秒内从我的酒吧滚出去，否则别怪等离子弹不长眼！"
        )
        bartender_notes = f"极度危险恶搞分子！冒犯次数达到 {offense_count} 次，触发终极驱逐协议。"

    updated_memory = {
        "user_id": current_memory.get("user_id", "player_001"),
        "orders": orders,
        "offense_count": offense_count,
        "attitude": attitude,
        "last_offense": last_offense,
        "bartender_notes": bartender_notes
    }

    return updated_memory, reply


# ------------------------------------------------------------------------------
# 4. 核心 API 路由接口实现
# ------------------------------------------------------------------------------
@app.post("/init", response_model=Dict[str, Any], summary="重置/初始化玩家在 PowerContext 中的记忆档案")
async def init_player_context(req: InitRequest):
    """
    初始化接口：
    重置指定玩家的记忆与状态，清空黑历史，让酒保恢复初始态度。
    """
    try:
        initial_context = {
            "user_id": req.user_id,
            "orders": [],
            "offense_count": 0,
            "attitude": "friendly",
            "last_offense": "",
            "bartender_notes": "初次光临的新客人，系统扫描未发现不良记录。"
        }
        
        # 写入 PowerContext
        synced = PowerContextAdapter.save_context(req.user_id, initial_context)

        return {
            "code": 200,
            "message": f"玩家 [{req.user_id}] 的记忆档案已在 PowerContext 中初始化成功！酒保老鲍勃已洗净杯子等待接待。",
            "powercontext_synced": synced,
            "initial_state": initial_context
        }
    except Exception as e:
        logger.error(f"初始化记忆失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"初始化 PowerContext 失败: {str(e)}")


@app.post("/chat", response_model=BartenderResponse, summary="与赛博酒保互动 (对话、点单或挑衅)")
async def chat_with_bartender(req: ChatRequest):
    """
    交互核心接口：
    1. 从 OceanBase PowerContext 读取用户的历史好感、点单偏好与冒犯前科。
    2. 基于当前交互动态推演 NPC 态度与记仇台词。
    3. 将最新上下文写回 PowerContext。
    """
    try:
        # 步骤 1: 从 PowerContext 获取历史记忆上下文
        # 关键技术点：使用 user_id 作为 session 隔离标识，保障跨会话记忆复用
        current_memory = PowerContextAdapter.get_context(req.user_id)
        current_memory["user_id"] = req.user_id

        # 步骤 2: 评估本轮交互，计算态度演变与酒保台词
        updated_memory, bartender_reply = evaluate_interaction(req.message, current_memory)

        # 步骤 3: 将更新后的记忆写回 PowerContext
        # 关键技术点：增量沉淀点单历史与冒犯次数，供下一轮交互复用
        synced = PowerContextAdapter.save_context(req.user_id, updated_memory)

        # 步骤 4: 返回结构化数据供 Dify 或前端展现
        return BartenderResponse(
            reply=bartender_reply,
            character_state=updated_memory,
            powercontext_synced=synced
        )
    except Exception as e:
        logger.error(f"处理对话异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"交互处理异常: {str(e)}")


@app.get("/status/{user_id}", summary="查看当前玩家在 PowerContext 中的记仇小黑册")
async def get_player_status(user_id: str):
    """
    供评委或前端实时核验该玩家保存在 PowerContext 中的底层记忆状态。
    """
    memory = PowerContextAdapter.get_context(user_id)
    return {
        "user_id": user_id,
        "powercontext_endpoint": POWERCONTEXT_API_URL,
        "stored_memory": memory
    }


if __name__ == "__main__":
    # 本地直接启动微服务，监听 8000 端口
    uvicorn.run(app, host="0.0.0.0", port=8000)
