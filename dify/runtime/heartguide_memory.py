import json
import os
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


MAX_BODY_BYTES = 256 * 1024
MAX_USER_ID_LENGTH = 128
MAX_CONVERSATION_ID_LENGTH = 128
MAX_USER_MESSAGE_LENGTH = 8000
MAX_ASSISTANT_MESSAGE_LENGTH = 24000
REMEMBER_PATTERN = re.compile(
    r"^(?:请)?(?:帮我)?(?:记住|记一下)(?:这个|我|一下|[,，:：\s]|$)|^please remember\b",
    re.IGNORECASE,
)


def connect_seekdb():
    import pymysql

    return pymysql.connect(
        host=os.environ["SEEKDB_HOST"],
        port=int(os.environ.get("SEEKDB_PORT", "2881")),
        user=os.environ["SEEKDB_USER"],
        password=os.environ["SEEKDB_PASSWORD"],
        database=os.environ.get("SEEKDB_DATABASE", "dify"),
        connect_timeout=5,
        charset="utf8mb4",
        autocommit=False,
    )


def initialize_schema():
    connection = connect_seekdb()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS heartguide_profiles (
                    user_id VARCHAR(128) NOT NULL PRIMARY KEY,
                    preference TEXT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP
                ) CHARACTER SET utf8mb4
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS heartguide_turns (
                    id CHAR(36) NOT NULL PRIMARY KEY,
                    user_id VARCHAR(128) NOT NULL,
                    conversation_id VARCHAR(128) NOT NULL,
                    role VARCHAR(16) NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    KEY idx_heartguide_user_conversation
                        (user_id, conversation_id, created_at)
                ) CHARACTER SET utf8mb4
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS heartguide_memory_reads (
                    id CHAR(36) NOT NULL PRIMARY KEY,
                    user_id VARCHAR(128) NOT NULL,
                    conversation_id VARCHAR(128) NOT NULL,
                    preference_found TINYINT(1) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    KEY idx_heartguide_memory_read_user (user_id, created_at)
                ) CHARACTER SET utf8mb4
                """
            )
        connection.commit()
    finally:
        connection.close()


def validate_id(value, field):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > (MAX_USER_ID_LENGTH if field == "user_id" else MAX_CONVERSATION_ID_LENGTH)
    ):
        raise ValueError(f"invalid_{field}")
    return value


def read_memory(user_id, conversation_id):
    connection = connect_seekdb()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT preference FROM heartguide_profiles WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            preference = row[0] if row else ""
            cursor.execute(
                """
                INSERT INTO heartguide_memory_reads
                    (id, user_id, conversation_id, preference_found)
                VALUES (%s, %s, %s, %s)
                """,
                (str(uuid.uuid4()), user_id, conversation_id, bool(preference)),
            )
        connection.commit()
        return preference
    finally:
        connection.close()


def should_remember(user_message):
    return bool(REMEMBER_PATTERN.search(user_message.strip()))


def write_turn(user_id, conversation_id, user_message, assistant_message):
    connection = connect_seekdb()
    try:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO heartguide_turns
                    (id, user_id, conversation_id, role, content)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    (str(uuid.uuid4()), user_id, conversation_id, "user", user_message),
                    (
                        str(uuid.uuid4()),
                        user_id,
                        conversation_id,
                        "assistant",
                        assistant_message,
                    ),
                ),
            )
            if should_remember(user_message):
                cursor.execute(
                    """
                    INSERT INTO heartguide_profiles (user_id, preference)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE preference = VALUES(preference)
                    """,
                    (user_id, user_message),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def parse_payload(body, content_type):
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/x-www-form-urlencoded":
        try:
            values = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
            )
        except (UnicodeDecodeError, ValueError):
            raise ValueError("invalid_form") from None
        return {key: items[-1] for key, items in values.items() if items}

    if media_type in ("", "application/json"):
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid_json") from None
        if not isinstance(payload, dict):
            raise ValueError("invalid_json")
        return payload

    raise ValueError("unsupported_content_type")


class MemoryHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self._respond(404, "not found", "text/plain")
            return
        try:
            connection = connect_seekdb()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
            finally:
                connection.close()
        except Exception:
            self._respond(503, "seekdb unavailable", "text/plain")
            return
        self._respond(200, "ok", "text/plain")

    def do_POST(self):
        if self.path not in ("/memory/read", "/turns"):
            self._respond(404, "not found", "text/plain")
            return
        try:
            payload = self._read_payload()
            user_id = validate_id(payload.get("user_id"), "user_id")
            if self.path == "/memory/read":
                conversation_id = validate_id(
                    payload.get("conversation_id"), "conversation_id"
                )
                preference = read_memory(user_id, conversation_id)
                self._respond(200, preference or "当前没有已保存的偏好", "text/plain")
                return

            conversation_id = validate_id(
                payload.get("conversation_id"), "conversation_id"
            )
            user_message = payload.get("user_message")
            assistant_message = payload.get("assistant_message")
            if (
                not isinstance(user_message, str)
                or not user_message.strip()
                or len(user_message) > MAX_USER_MESSAGE_LENGTH
                or not isinstance(assistant_message, str)
                or not assistant_message.strip()
                or len(assistant_message) > MAX_ASSISTANT_MESSAGE_LENGTH
            ):
                raise ValueError("invalid_turn")
            write_turn(user_id, conversation_id, user_message, assistant_message)
            self._respond(200, "saved", "text/plain")
        except (ValueError, json.JSONDecodeError):
            self._respond(400, "invalid request", "text/plain")
        except Exception:
            self._respond(500, "seekdb write failed", "text/plain")

    def _read_payload(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid_content_length") from None
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("invalid_content_length")
        return parse_payload(
            self.rfile.read(length),
            self.headers.get("Content-Type", ""),
        )

    def _respond(self, status, body, content_type):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):
        pass


def main():
    initialize_schema()
    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "8090"))), MemoryHandler
    )
    print("heartguide memory service ready; storage=seekdb", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"heartguide memory service failed: {type(error).__name__}", file=sys.stderr)
        raise
