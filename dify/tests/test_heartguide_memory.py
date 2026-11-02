import unittest
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.heartguide_memory import should_remember, validate_id


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

    def test_dsl_http_nodes_use_json_text_payloads(self):
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
            self.assertEqual(node["headers"], "Content-Type: application/json")
            self.assertEqual(node["body"]["type"], "json")
            self.assertEqual(node["body"]["data"][0]["key"], "")
            self.assertIn("{{#sys.user_id#}}", node["body"]["data"][0]["value"])


if __name__ == "__main__":
    unittest.main()
