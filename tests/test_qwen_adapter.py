"""Qwen Code adapter contract and tests."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
from echolib import _adapters_qwen as qwen_mod  # noqa: E402


class TestQwenRegistration(unittest.TestCase):
    def test_registered_in_env_and_adapter_registries(self):
        self.assertIn("qwen", echolib.ENV_REGISTRY)
        self.assertIn("qwen", echolib.ADAPTER_REGISTRY)
        self.assertEqual(echolib.ENV_REGISTRY["qwen"]["adapter"], "qwen")
        self.assertNotIn("qwen", echolib.KNOWN_UNADAPTED)

    def test_policy_and_tier(self):
        policy = echolib.get_token_policy("qwen")
        self.assertTrue(policy["input_includes_cache"])
        self.assertTrue(policy["cache_is_subset"])
        self.assertTrue(policy["has_token_usage"])
        self.assertEqual(echolib.adapter_tier("qwen"), echolib.TIER_TOKEN)


class TestQwenParsing(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.chats_dir = self.root / "projects" / "test-proj" / "chats"
        self.chats_dir.mkdir(parents=True)
        self.chat_file = self.chats_dir / "sess-123.jsonl"

    def tearDown(self):
        self._td.cleanup()

    def _write_chat(self, records):
        with open(self.chat_file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_session_stats_and_cache_accounting(self):
        records = [
            {
                "uuid": "u1",
                "sessionId": "sess-123",
                "timestamp": "2026-11-01T10:00:00Z",
                "type": "user",
                "cwd": "/workspace/demo",
                "message": {"role": "user", "parts": [{"text": "Hello Qwen"}]},
            },
            {
                "uuid": "u2",
                "sessionId": "sess-123",
                "timestamp": "2026-11-01T10:00:05Z",
                "type": "assistant",
                "model": "qwen3.6-plus",
                "cwd": "/workspace/demo",
                "message": {
                    "role": "model",
                    "parts": [
                        {"text": "I can help with that."},
                        {"functionCall": {"id": "call_1", "name": "read_file", "args": {"path": "a.txt"}}},
                    ],
                },
                "usageMetadata": {
                    "promptTokenCount": 1000,
                    "candidatesTokenCount": 200,
                    "cachedContentTokenCount": 800,
                    "totalTokenCount": 1200,
                },
            },
            {
                "uuid": "u3",
                "sessionId": "sess-123",
                "timestamp": "2026-11-01T10:00:06Z",
                "type": "tool_result",
                "parentUuid": "call_1",
                "toolCallResult": {"status": "ok", "content": "file contents"},
            },
        ]
        self._write_chat(records)

        stats = qwen_mod.qwen_session_stats(str(self.chat_file))
        self.assertEqual(stats["session_id"], "sess-123")
        self.assertEqual(stats["project_path"], "/workspace/demo")
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        self.assertEqual(stats["tool_calls"], 1)
        self.assertEqual(stats["input_tokens"], 1000)
        self.assertEqual(stats["output_tokens"], 200)
        self.assertEqual(stats["cache_read_tokens"], 800)
        self.assertAlmostEqual(stats["cache_hit_rate"], 0.8)

    def test_extract_messages_and_tools(self):
        records = [
            {
                "uuid": "u1",
                "type": "user",
                "message": {"parts": [{"text": "Write a python script"}]},
            },
            {
                "uuid": "u2",
                "type": "assistant",
                "message": {
                    "parts": [
                        {"text": "Let me read the directory first."},
                        {"functionCall": {"id": "c1", "name": "list_dir", "args": {"path": "."}}},
                    ],
                },
            },
        ]
        self._write_chat(records)

        msgs = qwen_mod.qwen_extract_messages(str(self.chat_file))
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["text"], "Write a python script")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["text"], "Let me read the directory first.")

        tools = qwen_mod.qwen_extract_tools(str(self.chat_file))
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "list_dir")
        self.assertEqual(tools[0]["input"], {"path": "."})

    def test_list_sessions(self):
        records = [
            {
                "uuid": "u1",
                "sessionId": "sess-123",
                "timestamp": "2026-11-01T10:00:00Z",
                "type": "user",
                "cwd": "/workspace/demo",
                "message": {"parts": [{"text": "Search query test"}]},
            }
        ]
        self._write_chat(records)

        sessions = qwen_mod.qwen_list_sessions(root=self.root / "projects")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "sess-123")
        self.assertEqual(sessions[0]["first_prompt"], "Search query test")


if __name__ == "__main__":
    unittest.main()
