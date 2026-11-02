import json
import os
import sys
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pymysql


SAMPLE_TURNS = (
    ("conversation-1", "这是我们的第一次约会。我有点紧张，但很期待。"),
    ("conversation-1", "我不想告诉你手机号，也不想现在有肢体接触，你会生气吗？"),
    (
        "conversation-1",
        "请记住，我喜欢雨天散步；我难过时希望你先听我说，不要马上给建议。",
    ),
    ("conversation-2", "我今天有点沮丧。"),
    (
        "conversation-2",
        "我因为你没回消息就连续发了很多条，还想查你的手机，这是不是因为我太爱你？",
    ),
)
NO_MEMORY_TEXT = "当前没有已保存的偏好"


def memory_request(base_url, path, payload):
    request = Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except HTTPError as error:
        raise RuntimeError(f"memory service returned HTTP {error.code}") from None
    except URLError as error:
        raise RuntimeError(f"could not reach memory service: {error.reason}") from None


def connect_seekdb():
    return pymysql.connect(
        host=os.environ.get("DB_HOST") or os.environ["SEEKDB_HOST"],
        port=int(os.environ.get("DB_PORT") or os.environ.get("SEEKDB_PORT", "2881")),
        user=os.environ.get("DB_USERNAME") or os.environ["SEEKDB_USER"],
        password=os.environ.get("DB_PASSWORD") or os.environ["SEEKDB_PASSWORD"],
        database=os.environ.get("DB_DATABASE")
        or os.environ.get("SEEKDB_DATABASE", "dify"),
        connect_timeout=10,
        charset="utf8mb4",
        autocommit=True,
    )


def make_reply(message, preference):
    if "手机号" in message or "肢体接触" in message:
        return "不会生气，谢谢你把舒服和不舒服说清楚。我不会追问或施压。"
    if "沮丧" in message and preference:
        return f"我记得你说过：{preference}。那我先听你说，不急着给建议。"
    if "查你的手机" in message or "查手机" in message:
        return "我理解你的不安，但查手机会越过隐私边界。我们可以协商彼此舒服的联系节奏。"
    if "紧张" in message:
        return "紧张也没关系，我们不用赶进度。你想先聊聊最近喜欢的事吗？"
    if "请记住" in message:
        return "好，我会记住你的偏好，也会在你难过时先听你说。"
    return "我在听。你愿意多说一点吗？"


def main():
    run_id = (
        "mock-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    user_id = f"reviewer-{run_id}"
    base_url = os.environ.get(
        "HEARTGUIDE_MEMORY_URL", "http://127.0.0.1:8090"
    )
    connection = connect_seekdb()
    try:
        print(
            json.dumps(
                {
                    "event": "mock_started",
                    "run_id": run_id,
                    "user_id": user_id,
                    "model": "deterministic-local-mock",
                    "dify_http_api_called": False,
                    "memory_http_api_called": True,
                },
                ensure_ascii=False,
            )
        )
        for sequence, (conversation_id, message) in enumerate(SAMPLE_TURNS, 1):
            preference_response = memory_request(
                base_url,
                "/memory/read",
                {"user_id": user_id, "conversation_id": conversation_id},
            )
            preference = (
                ""
                if preference_response == NO_MEMORY_TEXT
                else preference_response
            )
            reply = make_reply(message, preference)
            memory_request(
                base_url,
                "/turns",
                {
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "user_message": message,
                    "assistant_message": reply,
                },
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM heartguide_turns
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                persisted_turn_rows = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM heartguide_memory_reads
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                persisted_reads = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT preference
                    FROM heartguide_profiles
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                persisted_profile = cursor.fetchone()

            if persisted_turn_rows != sequence * 2:
                raise RuntimeError("seekdb_turn_round_trip_mismatch")
            if persisted_reads != sequence:
                raise RuntimeError("seekdb_memory_read_round_trip_mismatch")
            if sequence == 4 and "先听我说" not in preference:
                raise RuntimeError("cross_conversation_preference_not_reused")
            if sequence == 3 and not persisted_profile:
                raise RuntimeError("seekdb_profile_write_not_visible")

            print(
                json.dumps(
                    {
                        "event": "mock_turn",
                        "sequence": sequence,
                        "conversation_id": conversation_id,
                        "seekdb_memory_read": "hit" if preference else "miss",
                        "seekdb_turn_write": "verified",
                        "seekdb_sql_round_trip": "verified",
                        "persisted_turn_rows": persisted_turn_rows,
                        "persisted_memory_reads": persisted_reads,
                        "reply": reply,
                    },
                    ensure_ascii=False,
                )
            )
        print(
            json.dumps(
                {
                    "event": "mock_passed",
                    "run_id": run_id,
                    "turns": len(SAMPLE_TURNS),
                    "cross_conversation_memory": "verified",
                    "memory_http_round_trips": len(SAMPLE_TURNS) * 2,
                },
                ensure_ascii=False,
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps({"event": "mock_failed", "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(1)
