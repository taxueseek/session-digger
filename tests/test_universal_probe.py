"""Universal SchemaProbe + confidence-gated dispatch (no free-text model traps)."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_ROOT))
_ECHOLIB = _ROOT / "echolib" / "__init__.py"
_spec = importlib.util.spec_from_file_location("echolib", str(_ECHOLIB))
echolib = importlib.util.module_from_spec(_spec)
sys.modules["echolib"] = echolib
_spec.loader.exec_module(echolib)

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample-session.jsonl"


class TestDispatchNoFreeTextTrap(unittest.TestCase):
    def test_mentioning_sonnet_does_not_force_claude(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "chat.jsonl"
        path.write_text(
            "\n".join([
                json.dumps({
                    "role": "user",
                    "content": "请用 sonnet 和 claude 比较一下",
                    "timestamp": 1000,
                }),
                json.dumps({
                    "role": "assistant",
                    "content": "好的，关于 sonnet…",
                    "timestamp": 1001,
                    "model": "other-model",
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        # Path not under ~/.claude → must not become claude via free text
        self.assertEqual(echolib.detect_agent_type(str(path)), "unknown")
        self.assertEqual(echolib.dispatch_resolve_agent(str(path)), "universal")
        stats = echolib.dispatch_session_stats(str(path))
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        msgs = list(echolib.dispatch_extract_messages(str(path), role="user"))
        self.assertEqual(msgs[0]["text"], "请用 sonnet 和 claude 比较一下")

    def test_claude_fixture_still_routes_structurally(self):
        self.assertEqual(echolib.dispatch_resolve_agent(str(SAMPLE)), "claude")
        stats = echolib.dispatch_session_stats(str(SAMPLE))
        self.assertGreaterEqual(stats["tool_calls"], 1)


class TestUniversalFamilies(unittest.TestCase):
    def _write(self, records, name="s.jsonl"):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / name
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    def test_history_display_family(self):
        path = self._write([
            {"display": "介绍下你的能力", "timestamp": 1, "project": "/tmp"},
            {"display": "第二个问题", "timestamp": 2, "project": "/tmp"},
        ])
        schema = echolib._probe_schema(str(path))
        self.assertEqual(schema["style"], "history_display")
        stats = echolib.universal_session_stats(str(path))
        self.assertEqual(stats["user_messages"], 2)
        self.assertEqual(stats["assistant_messages"], 0)
        msgs = list(echolib.universal_extract_messages(str(path), role="user"))
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["text"], "介绍下你的能力")

    def test_flat_role_and_user_query_strip(self):
        path = self._write([
            {
                "role": "user",
                "content": "<user_query>\n真正的问题\n</user_query>",
                "timestamp": 10,
            },
            {"role": "assistant", "content": "回答", "timestamp": 11},
            {"role": "system", "content": "ignore me", "timestamp": 9},
        ])
        schema = echolib._probe_schema(str(path))
        self.assertEqual(schema["style"], "flat_role")
        stats = echolib.universal_session_stats(str(path))
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        msgs = list(echolib.universal_extract_messages(str(path), role="user"))
        self.assertEqual(msgs[0]["text"], "真正的问题")

    def test_summary_card_family(self):
        path = self._write([
            {
                "intent": "分析项目",
                "actions": ["读文件", "写报告"],
                "outcome": "完成分析",
                "learned": ["IO 是瓶颈"],
                "message_summary_time": "2026-01-01 12:00:00",
            }
        ])
        schema = echolib._probe_schema(str(path))
        self.assertEqual(schema["style"], "summary_card")
        stats = echolib.universal_session_stats(str(path))
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        tools = list(echolib.universal_extract_tools(str(path)))
        self.assertEqual(len(tools), 2)


if __name__ == "__main__":
    unittest.main()
