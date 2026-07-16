"""ZCode + Kimi Code adapter quality (parity direction with Claude/Codex/Cursor)."""
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


class TestZCodeAdapter(unittest.TestCase):
    def _write_transcript(self, records):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        # mimic agent path for slug
        agent = Path(td.name) / "sess_demo" / "agent_abc-123"
        agent.mkdir(parents=True)
        path = agent / "transcript.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    def test_model_ref_dict_not_tool_name(self):
        path = self._write_transcript([
            {
                "type": "turn_started",
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": {"input": "hello zcode"},
            },
            {
                "type": "model_request",
                "timestamp": "2026-01-01T00:00:01Z",
                "payload": {
                    "modelRef": {"providerId": "x", "modelId": "deepseek-v4-flash"},
                },
            },
            {
                "type": "tool_call_scheduled",
                "timestamp": "2026-01-01T00:00:02Z",
                "payload": {"toolCallId": "c1", "toolName": "Bash", "input": {"cmd": "ls"}},
            },
            {
                "type": "tool_batch_complete",
                "timestamp": "2026-01-01T00:00:03Z",
                "payload": {"toolCallIds": ["c1"], "successCount": 0, "errorCount": 1},
            },
            {
                "type": "model_complete",
                "timestamp": "2026-01-01T00:00:04Z",
                "payload": {
                    "content": "",
                    "usage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 10},
                },
            },
        ])
        stats = echolib.zcode_session_stats(str(path))
        self.assertEqual(stats["model"], "deepseek-v4-flash")
        self.assertNotEqual(stats["model"], "Bash")
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)
        self.assertEqual(stats["tool_calls"], 1)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["input_tokens"], 100)
        self.assertEqual(stats["output_tokens"], 20)
        self.assertTrue(stats["slug"].startswith("agent_"))

        tools = list(echolib.zcode_extract_tools(str(path)))
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["status"], "error")

    def test_zcode_db_model_usage_preferred(self):
        """Official model_usage table fills tokens + per-model map."""
        import echolib._adapters_zcode as zc

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db_path = Path(td.name) / "db.sqlite"
        conn = __import__("sqlite3").connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE model_usage (
                session_id TEXT, model_id TEXT,
                input_tokens INT, output_tokens INT,
                cache_read_input_tokens INT, cache_creation_input_tokens INT,
                reasoning_tokens INT, computed_total_tokens INT
            )
            """
        )
        rows = [
            ("sess_subagent_agent_abc-123", "deepseek-v4-flash", 1000, 50, 800, 0, 10, 1050),
            ("sess_subagent_agent_abc-123", "deepseek-v4-flash", 500, 20, 400, 0, 0, 520),
            ("sess_subagent_agent_abc-123", "mimo-v2.5", 200, 30, 100, 0, 5, 230),
        ]
        conn.executemany(
            "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        conn.close()

        agent = Path(td.name) / "sess_demo" / "agent_abc-123"
        agent.mkdir(parents=True)
        (agent / "metadata.json").write_text(
            json.dumps({
                "childSessionId": "sess_subagent_agent_abc-123",
                "parentSessionId": "sess_demo",
            }),
            encoding="utf-8",
        )
        # transcript without usage — DB must still win
        (agent / "transcript.jsonl").write_text(
            json.dumps({
                "type": "turn_started",
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": {"input": "hi"},
            }) + "\n",
            encoding="utf-8",
        )

        old_db = zc._ZCODE_DB
        zc._ZCODE_DB = db_path
        self.addCleanup(lambda: setattr(zc, "_ZCODE_DB", old_db))

        stats = echolib.zcode_session_stats(str(agent / "transcript.jsonl"))
        self.assertEqual(stats["input_tokens"], 1700)
        self.assertEqual(stats["output_tokens"], 100)
        self.assertEqual(stats["cache_read_tokens"], 1300)
        self.assertEqual(stats["user_messages"], 1)
        mu = stats.get("model_usage") or {}
        self.assertEqual(mu["deepseek-v4-flash"]["input_tokens"], 1500)
        self.assertEqual(mu["mimo-v2.5"]["input_tokens"], 200)
        self.assertEqual(mu["deepseek-v4-flash"]["model_calls"], 2)
        # primary model = highest input
        self.assertEqual(stats["model"], "deepseek-v4-flash")

        agg = echolib.zcode_aggregate_model_usage()
        self.assertEqual(agg["deepseek-v4-flash"]["input_tokens"], 1500)
        self.assertEqual(agg["mimo-v2.5"]["model_calls"], 1)

    def test_streaming_text_reassembly(self):
        path = self._write_transcript([
            {
                "type": "turn_started",
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": {"input": "hi"},
            },
            {
                "type": "model_streaming",
                "timestamp": "2026-01-01T00:00:01Z",
                "payload": {"kind": "start"},
            },
            {
                "type": "model_streaming",
                "timestamp": "2026-01-01T00:00:02Z",
                "payload": {"kind": "text_delta", "delta": "Hel"},
            },
            {
                "type": "model_streaming",
                "timestamp": "2026-01-01T00:00:03Z",
                "payload": {"kind": "text_delta", "delta": "lo!"},
            },
            {
                "type": "model_streaming",
                "timestamp": "2026-01-01T00:00:04Z",
                "payload": {"kind": "finish", "done": True},
            },
            {
                "type": "model_complete",
                "timestamp": "2026-01-01T00:00:05Z",
                "payload": {"content": ""},  # empty complete — stream is source of truth
            },
        ])
        msgs = list(echolib.zcode_extract_messages(str(path), role="assistant", thinking_limit=-1))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "Hello!")


class TestKimiCodeAdapter(unittest.TestCase):
    def _write_session(self, records, state=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name) / "wd_proj" / "session_aabbccdd-1111-2222-3333-444444444444"
        wire = root / "agents" / "main"
        wire.mkdir(parents=True)
        (root / "state.json").write_text(
            json.dumps(state or {
                "title": "demo",
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T01:00:00Z",
                "workDir": "/tmp/project",
                "lastPrompt": "hello",
            }),
            encoding="utf-8",
        )
        (wire / "wire.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        return root

    def test_stats_count_text_not_think_parts(self):
        root = self._write_session([
            {"type": "turn.prompt", "time": "2026-01-01T00:00:00Z",
             "input": [{"type": "text", "text": "Q1"}]},
            {"type": "llm.request", "time": "2026-01-01T00:00:01Z",
             "model": "LongCat-2.0"},
            {"type": "context.append_loop_event", "time": "2026-01-01T00:00:02Z",
             "event": {"type": "content.part", "turnId": "t1",
                       "part": {"type": "think", "text": "reasoning..."}}},
            {"type": "context.append_loop_event", "time": "2026-01-01T00:00:03Z",
             "event": {"type": "content.part", "turnId": "t1",
                       "part": {"type": "text", "text": "Answer one"}}},
            {"type": "context.append_loop_event", "time": "2026-01-01T00:00:04Z",
             "event": {"type": "tool.call", "toolCallId": "c1", "name": "Read",
                       "args": {"path": "a.py"}}},
            {"type": "context.append_loop_event", "time": "2026-01-01T00:00:05Z",
             "event": {"type": "tool.result", "toolCallId": "c1",
                       "result": {"isError": True, "output": "fail"}}},
            {"type": "usage.record", "time": "2026-01-01T00:00:06Z",
             "model": "longcat/LongCat-2.0",
             "usage": {"inputOther": 50, "output": 10, "inputCacheRead": 5}},
        ])
        stats = echolib.kimi_code_session_stats(str(root))
        self.assertEqual(stats["user_messages"], 1)
        self.assertEqual(stats["assistant_messages"], 1)  # not 2 (think+text)
        self.assertEqual(stats["tool_calls"], 1)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["model"], "LongCat-2.0")
        self.assertEqual(stats["slug"], "aabbccdd-1111-2222-3333-444444444444")
        self.assertEqual(stats["input_tokens"], 50)
        self.assertEqual(stats["output_tokens"], 10)

        msgs = list(echolib.kimi_code_extract_messages(str(root), role="both", thinking_limit=-1))
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles.count("USER"), 1)
        self.assertEqual(roles.count("ASSISTANT"), 1)
        self.assertIn("Answer one", msgs[-1]["text"])
        self.assertNotIn("[THINKING]", msgs[-1]["text"])

    def test_path_detect(self):
        fake = Path.home() / ".kimi-code" / "sessions" / "wd_x" / "session_y" / "agents" / "main" / "wire.jsonl"
        self.assertEqual(echolib.detect_agent_type(str(fake)), "kimi_code")
        zfake = Path.home() / ".zcode" / "cli" / "agents" / "sess_x" / "agent_y" / "transcript.jsonl"
        self.assertEqual(echolib.detect_agent_type(str(zfake)), "zcode")


if __name__ == "__main__":
    unittest.main()
