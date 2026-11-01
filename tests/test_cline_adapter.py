"""Cline / Roo Code adapter contract and tests."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
from echolib import _adapters_cline as cline_mod  # noqa: E402


class TestClineRegistration(unittest.TestCase):
    def test_registered_in_env_and_adapter_registries(self):
        self.assertIn("cline", echolib.ENV_REGISTRY)
        self.assertIn("cline", echolib.ADAPTER_REGISTRY)
        self.assertEqual(echolib.ENV_REGISTRY["cline"]["adapter"], "cline")

    def test_policy_and_tier(self):
        policy = echolib.get_token_policy("cline")
        self.assertFalse(policy["input_includes_cache"])
        self.assertFalse(policy["cache_is_subset"])
        self.assertTrue(policy["has_token_usage"])
        self.assertEqual(echolib.adapter_tier("cline"), echolib.TIER_TOKEN)


class TestClineParsing(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.task_dir = self.root / "tasks" / "task-abc-123"
        self.task_dir.mkdir(parents=True)
        self.ui_file = self.task_dir / "ui_messages.json"

    def tearDown(self):
        self._td.cleanup()

    def _write_ui_messages(self, items):
        with open(self.ui_file, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)

    def test_session_stats_and_cache(self):
        items = [
            {
                "ts": 1730000000000,
                "type": "say",
                "say": "task",
                "text": "Refactor database migrations",
            },
            {
                "ts": 1730000001000,
                "type": "say",
                "say": "api_req_started",
                "text": json.dumps({
                    "request": {"model": "claude-3-7-sonnet-20250219"},
                    "tokensIn": 1500,
                    "tokensOut": 300,
                    "cacheReads": 1000,
                    "cacheWrites": 200,
                }),
            },
            {
                "ts": 1730000002000,
                "type": "say",
                "say": "text",
                "text": "I will examine the migration files now.",
            },
            {
                "ts": 1730000003000,
                "type": "say",
                "say": "tool",
                "tool": "list_files",
                "text": json.dumps({"tool": "list_files", "path": "migrations"}),
            },
        ]
        self._write_ui_messages(items)

        stats = cline_mod.cline_session_stats(str(self.ui_file))
        self.assertEqual(stats["session_id"], "task-abc-123")
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        self.assertEqual(stats["tool_calls"], 1)
        self.assertEqual(stats["input_tokens"], 1500)
        self.assertEqual(stats["output_tokens"], 300)
        self.assertEqual(stats["cache_read_tokens"], 1000)
        self.assertEqual(stats["model"], "claude-3-7-sonnet-20250219")
        # cache hit rate: 1000 / (1500 + 1000) = 0.4
        self.assertAlmostEqual(stats["cache_hit_rate"], 0.4)

    def test_extract_messages_and_tools(self):
        items = [
            {"ts": 1730000000000, "type": "say", "say": "task", "text": "Fix styling"},
            {"ts": 1730000001000, "type": "say", "say": "text", "text": "Checking CSS..."},
            {"ts": 1730000002000, "type": "say", "say": "tool", "tool": "read_file", "text": json.dumps({"tool": "read_file", "path": "app.css"})},
        ]
        self._write_ui_messages(items)

        msgs = cline_mod.cline_extract_messages(str(self.task_dir))
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["text"], "Fix styling")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["text"], "Checking CSS...")

        tools = cline_mod.cline_extract_tools(str(self.task_dir))
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "read_file")

    def test_list_sessions(self):
        items = [
            {"ts": 1730000000000, "type": "say", "say": "task", "text": "Unit test setup"},
        ]
        self._write_ui_messages(items)

        sessions = cline_mod.cline_list_sessions(root=self.root / "tasks")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "task-abc-123")
        self.assertEqual(sessions[0]["first_prompt"], "Unit test setup")


if __name__ == "__main__":
    unittest.main()
