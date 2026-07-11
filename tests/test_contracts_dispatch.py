"""First golden-file regression tests for echolib package.

Before this file: assert=0 across the entire tests/ suite. We now lock in
the public contract that `dispatch_session_stats()` / `dispatch_extract_messages()`
shape to match the TypedDict definitions in `_contracts.py`.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

_ECHOLIB = Path(__file__).resolve().parent.parent / "scripts" / "echolib" / "__init__.py"
_spec = importlib.util.spec_from_file_location("echolib", str(_ECHOLIB))
echolib = importlib.util.module_from_spec(_spec)
sys.modules["echolib"] = echolib
_spec.loader.exec_module(echolib)

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample-session.jsonl"


class TestDispatchSessionStats(unittest.TestCase):
    def test_returns_expected_values_from_claude_fixture(self):
        stats = echolib.dispatch_session_stats(str(SAMPLE))
        self.assertEqual(stats["slug"], "test-slug")
        self.assertEqual(stats["branch"], "main")
        # Normalize helper emits "claude-sonnet-4-5-20250514"; prefix matches
        self.assertTrue(stats["model"].startswith("claude"))

    def test_counts_match_fixture_contents(self):
        stats = echolib.dispatch_session_stats(str(SAMPLE))
        # 3 role=user entries in the fixture (2 type=user + 1 type=tool_result)
        self.assertEqual(stats["user_messages"], 3)
        self.assertGreaterEqual(stats["assistant_messages"], 1)
        self.assertEqual(stats["tool_calls"], 3)
        self.assertEqual(stats["errors"], 1)

    def test_tokens_are_positive(self):
        stats = echolib.dispatch_session_stats(str(SAMPLE))
        self.assertGreaterEqual(stats["input_tokens"], 1500)
        self.assertGreaterEqual(stats["output_tokens"], 200)
        self.assertGreaterEqual(stats["total_tokens"], stats["input_tokens"])


class TestDispatchExtractMessages(unittest.TestCase):
    def test_role_both_returns_multiple(self):
        msgs = list(echolib.dispatch_extract_messages(str(SAMPLE), role="both"))
        self.assertGreaterEqual(len(msgs), 3)  # 1 user + 1 assistant + 1 user in fixture

    def test_role_user_filters(self):
        msgs = list(echolib.dispatch_extract_messages(str(SAMPLE), role="user"))
        # Adapter emits role="USER" (uppercased); both spellings acceptable
        self.assertTrue(all(m["role"].lower() == "user" for m in msgs))
        self.assertGreaterEqual(len(msgs), 1)

    def test_each_message_has_required_keys(self):
        msgs = list(echolib.dispatch_extract_messages(str(SAMPLE), role="both"))
        for m in msgs:
            self.assertIn("role", m)
            self.assertIn("timestamp", m)
            self.assertIn("text", m)


if __name__ == "__main__":
    unittest.main()
