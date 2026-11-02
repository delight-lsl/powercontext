import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "verify_chatflow.py"


class MockDifyHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        self.requests.append(payload)
        if payload.get("conversation_id"):
            conversation_id = payload["conversation_id"]
        else:
            conversation_id = f"conversation-{len(self.requests)}"
        result = {
            "answer": "我在听，也会尊重你的边界。",
            "conversation_id": conversation_id,
            "task_id": f"task-{len(self.requests)}",
            "message_id": f"message-{len(self.requests)}",
        }
        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class VerifyChatflowTests(unittest.TestCase):
    def test_cli_runs_five_turns_across_two_conversations_for_one_user(self):
        MockDifyHandler.requests = []
        server = HTTPServer(("127.0.0.1", 0), MockDifyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = os.environ.copy()
            env["DIFY_API_KEY"] = "test-api-key"
            env["DIFY_API_BASE_URL"] = (
                f"http://127.0.0.1:{server.server_port}/v1"
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--user-id", "test-reviewer"],
                capture_output=True,
                check=False,
                env=env,
                text=True,
                timeout=30,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("test-api-key", result.stdout)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(events[-1]["event"], "run_passed")
        self.assertEqual(events[-1]["turns"], 5)
        self.assertTrue(events[-1]["same_user_across_conversations"])
        self.assertEqual(len(MockDifyHandler.requests), 5)
        self.assertNotIn("conversation_id", MockDifyHandler.requests[0])
        self.assertNotIn("conversation_id", MockDifyHandler.requests[3])
        self.assertTrue(
            all(
                request["conversation_id"] == "conversation-1"
                for request in MockDifyHandler.requests[1:3]
            )
        )
        self.assertEqual(
            MockDifyHandler.requests[4]["conversation_id"], "conversation-4"
        )
        self.assertNotEqual(
            events[1]["conversation_id"], events[4]["conversation_id"]
        )
        self.assertTrue(
            all(
                request["response_mode"] == "blocking"
                for request in MockDifyHandler.requests
            )
        )


if __name__ == "__main__":
    unittest.main()
