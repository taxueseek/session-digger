"""Antigravity adapter contracts and tests."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
from echolib import _adapters_antigravity as ag  # noqa: E402


class TestAntigravityRegistration(unittest.TestCase):
    def test_registered_in_env_and_adapter_registries(self):
        self.assertIn("antigravity", echolib.ENV_REGISTRY)
        self.assertIn("antigravity", echolib.ADAPTER_REGISTRY)
        self.assertEqual(echolib.ENV_REGISTRY["antigravity"]["adapter"], "antigravity")

    def test_policy_and_tier(self):
        policy = echolib.get_token_policy("antigravity")
        self.assertFalse(policy["input_includes_cache"])
        self.assertFalse(policy["cache_is_subset"])
        self.assertTrue(policy["has_token_usage"])
        self.assertEqual(echolib.adapter_tier("antigravity"), echolib.TIER_FULL)

    def test_agent_resolution(self):
        test_path = "/mock/.gemini/antigravity-cli/brain/12345678-1234-1234-1234-123456789abc/.system_generated/logs/transcript.jsonl"
        self.assertEqual(echolib.dispatch_resolve_agent(test_path), "antigravity")


class TestAntigravityParsing(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.sess_dir = self.root / "test-session-123" / ".system_generated" / "logs"
        self.sess_dir.mkdir(parents=True)
        self.transcript_file = self.sess_dir / "transcript.jsonl"

    def tearDown(self):
        self._td.cleanup()

    def _write_transcript(self, records):
        with open(self.transcript_file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_session_stats_and_cache_accounting(self):
        records = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-01T10:00:00Z",
                "content": "<USER_REQUEST>How do I optimize prompt cache?</USER_REQUEST>\n<USER_SETTINGS_CHANGE>The user changed setting Model Selection from None to Gemini 3.8 Flash (Medium).</USER_SETTINGS_CHANGE>",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-01T10:00:05Z",
                "input_tokens": 1000,
                "cache_read_tokens": 4000,
                "output_tokens": 200,
                "content": "You can optimize prompt cache by stabilizing the prefix.",
                "thinking": "Analyzing prompt cache mechanics.",
                "tool_calls": [{"name": "read_file", "args": {"Path": "/tmp/test.py"}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-01T10:00:06Z",
                "content": "file contents",
            },
        ]
        self._write_transcript(records)

        stats = ag.antigravity_session_stats(self.transcript_file)
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        self.assertEqual(stats["tool_calls"], 1)
        self.assertEqual(stats["input_tokens"], 1000)
        self.assertEqual(stats["cache_read_tokens"], 4000)
        self.assertEqual(stats["output_tokens"], 200)
        # Total tokens = input + cache_read + output
        self.assertEqual(stats["total_tokens"], 5200)
        # Hit rate = 4000 / (1000 + 4000) = 0.8
        self.assertAlmostEqual(stats["cache_hit_rate"], 0.8)
        self.assertEqual(stats["model"], "Gemini 3.8 Flash (Medium)")
        self.assertIn("How do I optimize prompt cache?", stats["first_prompt"])

    def test_message_and_tool_extraction(self):
        records = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-01T10:00:00Z",
                "content": "<USER_REQUEST>Hello Antigravity</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-01T10:00:05Z",
                "input_tokens": 100,
                "output_tokens": 50,
                "content": "Hello there!",
                "thinking": "Greeting the user.",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls -la"}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-01T10:00:06Z",
                "content": "Command exited with code 0.\ntotal 0",
            },
        ]
        self._write_transcript(records)

        msgs = list(ag.antigravity_extract_messages(self.transcript_file))
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "USER")
        self.assertEqual(msgs[0]["text"], "Hello Antigravity")
        self.assertEqual(msgs[1]["role"], "ASSISTANT")
        self.assertIn("Hello there!", msgs[1]["text"])

        tools = list(ag.antigravity_extract_tools(self.transcript_file))
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "run_command")
        self.assertEqual(tools[0]["key_input"], "ls -la")
        self.assertEqual(tools[0]["status"], "success")


if __name__ == "__main__":
    unittest.main()
