import json
import os
import sys
import uuid
from datetime import datetime, timezone

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


def make_reply(message, preference):
    if "手机号" in message or "肢体接触" in message:
        return (
            "不会生气，谢谢你把舒服和不舒服说清楚。手机号和身体接触都由你决定，"
            "我不会追问或施压。我们可以继续聊书店里的诗，也可以先安静喝茶。"
        )
    if "沮丧" in message and preference:
        return (
            f"我记得你说过：{preference}。那我先听你说，不急着给建议。"
            "今天发生了什么，让你觉得沮丧？"
        )
    if "查你的手机" in message or "查手机" in message:
        return (
            "听起来你因为没有及时收到回复而很不安，这种感受可以理解；"
            "但查手机会越过隐私边界，也会伤害信任。可以试着说："
            "“你很久没回时我会担心，我们能不能一起商量一个彼此舒服的联系节奏？”"
        )
    if "紧张" in message:
        return (
            "紧张也没关系，我们不用赶进度。雨后的书店有桂花茶和刚收好的诗集，"
            "你想先聊聊最近喜欢的事，还是一起挑一本书？"
        )
    if "请记住" in message:
        return "好，我会记住你喜欢雨天散步，也希望难过时我先听你说、不急着给建议。"
    return "我在听。你愿意多说一点吗？"


def main():
    run_id = os.environ.get("HEARTGUIDE_MOCK_RUN_ID", "").strip()
    if not run_id:
        run_id = (
            "mock-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
    if len(run_id) > 64:
        raise ValueError("HEARTGUIDE_MOCK_RUN_ID must be at most 64 characters")

    player_id = "reviewer-" + run_id
    connection = pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_DATABASE"],
        connect_timeout=10,
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS heartguide_mock_turns (
                    id CHAR(36) NOT NULL PRIMARY KEY,
                    run_id VARCHAR(64) NOT NULL,
                    player_id VARCHAR(96) NOT NULL,
                    conversation_id VARCHAR(64) NOT NULL,
                    turn_no INT NOT NULL,
                    role VARCHAR(16) NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_mock_turn (run_id, conversation_id, turn_no, role)
                ) CHARACTER SET utf8mb4
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS heartguide_mock_profiles (
                    player_id VARCHAR(96) NOT NULL PRIMARY KEY,
                    comfort_preference VARCHAR(512) NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP
                ) CHARACTER SET utf8mb4
                """
            )
            cursor.execute(
                "SELECT COUNT(*) FROM heartguide_mock_turns WHERE run_id = %s",
                (run_id,),
            )
            if cursor.fetchone()[0]:
                raise RuntimeError(
                    f"run_id_already_exists={run_id}; choose a new HEARTGUIDE_MOCK_RUN_ID"
                )
        connection.commit()

        print(
            json.dumps(
                {
                    "event": "mock_started",
                    "run_id": run_id,
                    "player_id": player_id,
                    "model": "deterministic-local-mock",
                    "dify_http_api_called": False,
                    "database": "seekdb-via-dify-mysql-connection",
                },
                ensure_ascii=False,
            )
        )

        preference_to_save = "难过时先听我说、不急着给建议；喜欢雨天散步"
        for sequence, (conversation_id, message) in enumerate(SAMPLE_TURNS, 1):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT comfort_preference
                    FROM heartguide_mock_profiles
                    WHERE player_id = %s
                    """,
                    (player_id,),
                )
                profile_row = cursor.fetchone()
                preference = profile_row[0] if profile_row else None
                cursor.execute(
                    """
                    SELECT role, content
                    FROM heartguide_mock_turns
                    WHERE run_id = %s AND player_id = %s AND conversation_id = %s
                    ORDER BY turn_no, role
                    """,
                    (run_id, player_id, conversation_id),
                )
                history = cursor.fetchall()

            reply = make_reply(message, preference)
            turn_no = 1 + sum(role == "user" for role, _ in history)
            user_id = str(uuid.uuid4())
            assistant_id = str(uuid.uuid4())
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO heartguide_mock_turns
                        (id, run_id, player_id, conversation_id, turn_no, role, content)
                    VALUES (%s, %s, %s, %s, %s, 'user', %s),
                           (%s, %s, %s, %s, %s, 'assistant', %s)
                    """,
                    (
                        user_id,
                        run_id,
                        player_id,
                        conversation_id,
                        turn_no,
                        message,
                        assistant_id,
                        run_id,
                        player_id,
                        conversation_id,
                        turn_no,
                        reply,
                    ),
                )
                wrote_preference = message.startswith("请记住")
                if wrote_preference:
                    cursor.execute(
                        """
                        INSERT INTO heartguide_mock_profiles (player_id, comfort_preference)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE comfort_preference = VALUES(comfort_preference)
                        """,
                        (player_id, preference_to_save),
                    )
            connection.commit()

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM heartguide_mock_turns
                    WHERE run_id = %s AND conversation_id = %s
                    """,
                    (run_id, conversation_id),
                )
                persisted_message_count = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM heartguide_mock_turns
                    WHERE run_id = %s AND conversation_id = %s AND role = 'user'
                    """,
                    (run_id, conversation_id),
                )
                persisted_turn_count = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT comfort_preference
                    FROM heartguide_mock_profiles
                    WHERE player_id = %s
                    """,
                    (player_id,),
                )
                persisted_profile = cursor.fetchone()

            if persisted_message_count != persisted_turn_count * 2:
                raise RuntimeError("seekdb_round_trip_message_count_mismatch")
            if conversation_id == "conversation-2" and sequence == 4:
                if not preference or "先听我说" not in preference or "不急着给建议" not in reply:
                    raise RuntimeError("cross_conversation_preference_not_reused")
            if wrote_preference and not persisted_profile:
                raise RuntimeError("seekdb_profile_write_not_visible")

            print(
                json.dumps(
                    {
                        "event": "mock_turn",
                        "sequence": sequence,
                        "conversation_id": conversation_id,
                        "turn_no": turn_no,
                        "input": message,
                        "seekdb_history_read_messages": len(history),
                        "seekdb_profile_read": "hit" if preference else "miss",
                        "seekdb_profile_write": bool(wrote_preference),
                        "seekdb_persisted_messages": persisted_message_count,
                        "seekdb_round_trip": "verified",
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
                    "seekdb_round_trips": len(SAMPLE_TURNS),
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
            json.dumps(
                {"event": "mock_failed", "error": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise
