import unittest
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.heartguide_memory import parse_payload, should_remember, validate_id


class HeartguideMemoryTests(unittest.TestCase):
    def test_explicit_remember_request_is_saved(self):
        self.assertTrue(
            should_remember("请记住，我喜欢雨天散步；难过时先听我说。")
        )
        self.assertTrue(should_remember("帮我记一下：不要马上给建议"))

    def test_ordinary_message_is_not_saved_as_preference(self):
        self.assertFalse(should_remember("我今天有点沮丧。"))

    def test_ids_are_required_and_bounded(self):
        self.assertEqual(validate_id("reviewer-1", "user_id"), "reviewer-1")
        with self.assertRaises(ValueError):
            validate_id("", "user_id")
        with self.assertRaises(ValueError):
            validate_id("x" * 129, "conversation_id")

    def test_dsl_http_nodes_use_form_payloads(self):
        dsl = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "heartguide.yml").read_text(
                encoding="utf-8"
            )
        )
        http_nodes = [
            node["data"]
            for node in dsl["workflow"]["graph"]["nodes"]
            if node["data"]["type"] == "http-request"
        ]
        self.assertEqual(len(http_nodes), 2)
        for node in http_nodes:
            self.assertEqual(
                node["headers"],
                "Content-Type: application/x-www-form-urlencoded",
            )
            self.assertEqual(node["body"]["type"], "x-www-form-urlencoded")
            self.assertGreaterEqual(len(node["body"]["data"]), 2)
            self.assertEqual(node["body"]["data"][0]["key"], "user_id")
            self.assertIn("{{#sys.user_id#}}", node["body"]["data"][0]["value"])

    def test_form_payload_preserves_quotes_and_newlines(self):
        payload = parse_payload(
            (
                "user_id=reviewer-1&conversation_id=conversation-1"
                "&user_message=%E8%AF%B7%E8%AE%B0%E4%BD%8F%EF%BC%8C%E4%BB%96%E8%AF%B4%EF%BC%9A%22%E6%85%A2%E4%B8%80%E7%82%B9%22%0A"
                "&assistant_message=%E5%A5%BD%EF%BC%8C%E6%88%91%E4%BC%9A%E8%AE%B0%E4%BD%8F%E3%80%82"
            ).encode("ascii"),
            "application/x-www-form-urlencoded; charset=utf-8",
        )
        self.assertEqual(payload["user_id"], "reviewer-1")
        self.assertEqual(payload["conversation_id"], "conversation-1")
        self.assertIn('"慢一点"\n', payload["user_message"])
        self.assertEqual(payload["assistant_message"], "好，我会记住。")


if __name__ == "__main__":
    unittest.main()
