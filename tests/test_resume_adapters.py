"""Regression tests for Claude / Codex / Cursor adapter upgrades.

Locks in path detection, UUID extraction, Cursor user_query stripping,
and zstd-transparent JSONL iteration — capabilities borrowed from Grok
Build resume-session, adapted to session-digger's 5-method contract.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent / "scripts"
_ECHOLIB = _ROOT / "echolib" / "__init__.py"
_spec = importlib.util.spec_from_file_location("echolib", str(_ECHOLIB))
echolib = importlib.util.module_from_spec(_spec)
sys.modules["echolib"] = echolib
# Ensure package submodules resolve under scripts/
sys.path.insert(0, str(_ROOT))
_spec.loader.exec_module(echolib)

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample-session.jsonl"


class TestDetectAgentTypePath(unittest.TestCase):
    def test_claude_fixture_still_resolves(self):
        self.assertEqual(echolib.dispatch_resolve_agent(str(SAMPLE)), "claude")

    def test_codex_rollout_name_cue(self):
        fake = Path("/tmp/not-under-home/rollout-2026-01-01T00-00-00-019f5be8-7715-70d0-b8e0-f52d267ba6be.jsonl")
        self.assertEqual(echolib.detect_agent_type(str(fake)), "codex")

    def test_cursor_agent_transcripts_path_cue(self):
        fake = (
            Path.home()
            / ".cursor"
            / "projects"
            / "demo"
            / "agent-transcripts"
            / "0904e0e3-bf7d-4b43-b0f1-db09d227be01"
            / "0904e0e3-bf7d-4b43-b0f1-db09d227be01.jsonl"
        )
        self.assertEqual(echolib.detect_agent_type(str(fake)), "cursor")

    def test_cursor_registered(self):
        self.assertIn("cursor", echolib.ADAPTER_REGISTRY)
        self.assertIn("cursor", echolib.ENV_REGISTRY)


class TestCodexIdAndZst(unittest.TestCase):
    def test_codex_id_from_full_uuid(self):
        from echolib._adapters import _codex_id_from_path
        name = "rollout-2026-07-13T22-36-29-019f5be8-7715-70d0-b8e0-f52d267ba6be.jsonl"
        self.assertEqual(
            _codex_id_from_path(name),
            "019f5be8-7715-70d0-b8e0-f52d267ba6be",
        )

    def test_codex_id_from_zst(self):
        from echolib._adapters import _codex_id_from_path
        name = "rollout-2026-07-13T22-36-29-019f5be8-7715-70d0-b8e0-f52d267ba6be.jsonl.zst"
        self.assertEqual(
            _codex_id_from_path(name),
            "019f5be8-7715-70d0-b8e0-f52d267ba6be",
        )

    def test_iter_jsonl_plain(self):
        rows = list(echolib._iter_jsonl(SAMPLE))
        self.assertGreaterEqual(len(rows), 3)

    def test_local_shell_counted_as_tool(self):
        """local_shell_call must increment tool_calls (resume-session parity)."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout-2026-01-01T00-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
            records = [
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {"type": "local_shell_call", "call_id": "c1", "action": {"cmd": "ls"}},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "output": "ok\nExit Code: 0",
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            stats = echolib.codex_session_stats_dedicated(str(path))
            self.assertEqual(stats["tool_calls"], 1)
            tools = list(echolib.codex_extract_tools(str(path)))
            self.assertEqual(len(tools), 1)
            self.assertEqual(tools[0]["name"], "local_shell")


class TestCursorParsing(unittest.TestCase):
    def test_user_query_strip_and_tool_use(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl"
            records = [
                {
                    "role": "user",
                    "message": {
                        "content": [
                            {"type": "text", "text": "<user_query>\nHello cursor\n</user_query>"}
                        ]
                    },
                },
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Sure"},
                            {"type": "tool_use", "name": "Read", "input": {"path": "a.py"}},
                        ]
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

            # Path detection needs .cursor/agent-transcripts cue; call extractors directly
            from echolib._adapters_cursor import (
                cursor_extract_messages,
                cursor_extract_tools,
                cursor_session_stats,
            )
            stats = cursor_session_stats(str(path))
            self.assertEqual(stats["user_messages"], 1)
            self.assertEqual(stats["assistant_messages"], 1)
            self.assertEqual(stats["tool_calls"], 1)

            msgs = list(cursor_extract_messages(str(path), role="user"))
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0]["text"], "Hello cursor")
            self.assertNotIn("<user_query>", msgs[0]["text"])

            tools = list(cursor_extract_tools(str(path)))
            self.assertEqual(len(tools), 1)
            self.assertEqual(tools[0]["name"], "Read")

    def test_content_detect_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "orphan-cursor.jsonl"
            records = [
                {
                    "role": "user",
                    "message": {
                        "content": [
                            {"type": "text", "text": "<user_query>\nX\n</user_query>"}
                        ]
                    },
                },
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Shell", "input": {}},
                        ]
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            detected = echolib._detect_format_from_content(str(path))
            self.assertEqual(detected, "cursor")


class TestOutputTextBlock(unittest.TestCase):
    def test_output_text_extracted(self):
        text = echolib._extract_content_text(
            [{"type": "output_text", "text": "from codex"}]
        )
        self.assertEqual(text, "from codex")


if __name__ == "__main__":
    unittest.main()
