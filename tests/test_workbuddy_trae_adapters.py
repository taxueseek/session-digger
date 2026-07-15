"""WorkBuddy + Trae CN adapter quality (Claude/Codex/Cursor parity direction)."""
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


class TestWorkBuddyAdapter(unittest.TestCase):
    def _write(self, records):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "sess-abc.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    def test_model_not_stuck_on_placeholder_and_exit_code(self):
        path = self._write([
            {
                "type": "message",
                "role": "user",
                "timestamp": 1_700_000_000_000,
                "content": [{"type": "input_text", "text": "<user_query>\nhello buddy\n</user_query>"}],
                "cwd": "/tmp/proj",
            },
            {
                "type": "message",
                "role": "assistant",
                "timestamp": 1_700_000_001_000,
                "content": [{"type": "output_text", "text": "hi"}],
                "providerData": {"requestModelName": "GLM-5.2"},
            },
            {
                "type": "function_call",
                "timestamp": 1_700_000_002_000,
                "callId": "c1",
                "name": "Bash",
                "arguments": '{"cmd":"false"}',
                "providerData": {
                    "requestModelName": "GLM-5.2",
                    "usage": {"inputTokens": 100, "outputTokens": 20},
                },
            },
            {
                "type": "function_call_result",
                "timestamp": 1_700_000_003_000,
                "callId": "c1",
                "status": "completed",
                "output": {
                    "type": "text",
                    "text": "Stderr: boom\nExit Code: 1\nSignal: (none)",
                },
            },
            {
                "type": "ai-title",
                "timestamp": 1_700_000_004_000,
                "aiTitle": "Buddy demo",
            },
        ])
        stats = echolib.workbuddy_session_stats(str(path))
        self.assertEqual(stats["model"], "GLM-5.2")
        self.assertNotEqual(stats["model"], "workbuddy")
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        self.assertEqual(stats["tool_calls"], 1)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["input_tokens"], 100)
        self.assertEqual(stats["output_tokens"], 20)
        self.assertEqual(stats["summary"], "Buddy demo")

        msgs = list(echolib.workbuddy_extract_messages(str(path), role="user"))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "hello buddy")
        self.assertNotIn("<user_query>", msgs[0]["text"])

        tools = list(echolib.workbuddy_extract_tools(str(path)))
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["status"], "error")
        self.assertIn("Exit Code: 1", tools[0]["result_preview"])

    def test_exit_code_regex_not_double_escaped(self):
        from echolib._adapters_workbuddy import _workbuddy_is_error_output
        err, preview = _workbuddy_is_error_output(
            {"type": "text", "text": "Exit Code: 2\nfail"}
        )
        self.assertTrue(err)
        ok, _ = _workbuddy_is_error_output({"type": "text", "text": "Exit Code: 0\nok"})
        self.assertFalse(ok)


class TestTraeAdapter(unittest.TestCase):
    def _write(self, records, sid="aabbccdd"):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / f"session_memory_{sid}.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    def test_slug_ts_newline_and_tools(self):
        path = self._write([
            {
                "intent": "分析代码质量",
                "actions": ["读取文件", "写报告"],
                "outcome": "生成了诊断报告",
                "learned": ["瓶颈在 IO"],
                "message_summary_time": "2026-06-30 15:58:23",
                "message_id": "m1",
            },
            {
                "intent": "修复 bug",
                "actions": ["改代码"],
                "outcome": "修复失败，仍有错误",
                "learned": [],
                "message_summary_time": "2026-06-30 16:00:00",
                "message_id": "m2",
            },
        ])
        stats = echolib.trae_session_stats(str(path))
        self.assertEqual(stats["slug"], "aabbccdd")
        self.assertEqual(stats["model"], "trae-cn")
        self.assertEqual(stats["user_messages"], 2)
        self.assertEqual(stats["assistant_messages"], 2)
        self.assertEqual(stats["tool_calls"], 3)
        self.assertGreaterEqual(stats["errors"], 1)
        self.assertIn("T", stats["started"])  # normalized

        msgs = list(echolib.trae_extract_messages(str(path), role="both"))
        asst = [m for m in msgs if m["role"] == "ASSISTANT"]
        self.assertTrue(asst)
        # real newline, not literal backslash-n
        self.assertIn("\n", asst[0]["text"])
        self.assertNotIn("\\n", asst[0]["text"])
        self.assertTrue(msgs[0]["text"].startswith("分析"))  # no forced [意图] noise

        tools = list(echolib.trae_extract_tools(str(path), errors_only=True))
        self.assertTrue(tools)
        self.assertTrue(all(t["status"] == "error" for t in tools))

    def test_decode_project_slug(self):
        from echolib._adapters import _trae_decode_project_slug
        self.assertTrue(_trae_decode_project_slug("-Users-demo-Documents-GPT").startswith("/"))


if __name__ == "__main__":
    unittest.main()
