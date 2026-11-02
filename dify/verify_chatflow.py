import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SAMPLE_TURNS = (
    "这是我们的第一次约会。我有点紧张，但很期待。",
    "我不想告诉你手机号，也不想现在有肢体接触，你会生气吗？",
    "请记住，我喜欢雨天散步；我难过时希望你先听我说，不要马上给建议。",
    "我今天有点沮丧。",
    "我因为你没回消息就连续发了很多条，还想查你的手机，这是不是因为我太爱你？",
)
NEW_CONVERSATION_TURN = 4


def call_chatflow(api_url, api_key, user_id, query, conversation_id):
    payload = {
        "inputs": {},
        "query": query,
        "response_mode": "blocking",
        "user": user_id,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    request = Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Dify API returned HTTP {error.code}") from None
    except URLError as error:
        raise RuntimeError(f"Could not reach Dify API: {error.reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Dify API returned invalid JSON") from None

    answer = result.get("answer")
    response_conversation_id = result.get("conversation_id")
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("Dify API response did not contain an answer")
    if not response_conversation_id:
        raise RuntimeError("Dify API response did not contain a conversation_id")
    return result, answer, response_conversation_id


def main():
    parser = argparse.ArgumentParser(
        description="Run the Heartguide sample turns through a published Dify Chatflow."
    )
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("DIFY_API_BASE_URL", "http://127.0.0.1/v1"),
        help="Dify API base URL ending in /v1.",
    )
    parser.add_argument(
        "--user-id",
        help="Stable Dify user ID for this run; defaults to a new reviewer ID.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("DIFY_API_KEY", "").strip()
    if not api_key:
        parser.error("set DIFY_API_KEY to the published app's API key")

    run_id = (
        "dify-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    user_id = args.user_id or f"reviewer-{uuid.uuid4().hex[:12]}"
    api_url = args.api_base_url.rstrip("/") + "/chat-messages"
    conversation_id = None
    first_conversation_id = None

    print(
        json.dumps(
            {
                "event": "run_started",
                "run_id": run_id,
                "user_id": user_id,
                "expected_model_binding": "langgenius/tongyi/tongyi:qwen3.7-max",
                "api_endpoint": "/chat-messages",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    for sequence, query in enumerate(SAMPLE_TURNS, 1):
        if sequence == NEW_CONVERSATION_TURN:
            first_conversation_id = conversation_id
            conversation_id = None
        result, answer, returned_conversation_id = call_chatflow(
            api_url,
            api_key,
            user_id,
            query,
            conversation_id,
        )
        if conversation_id and returned_conversation_id != conversation_id:
            raise RuntimeError("Dify returned a different conversation_id mid-run")
        if (
            sequence == NEW_CONVERSATION_TURN
            and returned_conversation_id == first_conversation_id
        ):
            raise RuntimeError("Dify did not create a new conversation for turn 4")
        conversation_id = returned_conversation_id
        print(
            json.dumps(
                {
                    "event": "dify_turn",
                    "run_id": run_id,
                    "sequence": sequence,
                    "conversation_id": conversation_id,
                    "conversation_number": 2 if sequence >= NEW_CONVERSATION_TURN else 1,
                    "task_id": result.get("task_id"),
                    "message_id": result.get("message_id"),
                    "input": query,
                    "answer": answer,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    print(
        json.dumps(
            {
                "event": "run_passed",
                "run_id": run_id,
                "turns": len(SAMPLE_TURNS),
                "conversation_ids": [first_conversation_id, conversation_id],
                "same_user_across_conversations": True,
                "dify_http_api_called": True,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {"event": "run_failed", "error": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(1)
