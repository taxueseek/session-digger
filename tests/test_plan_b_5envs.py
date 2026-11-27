"""Tests for Plan B optimizations across the 5 core environments and MECE sub-skills."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
from echolib import _adapters_antigravity as ag  # noqa: E402
from echolib import _adapters_dsh as dsh  # noqa: E402
from echolib import _adapters_dimcode as dc  # noqa: E402
from echolib import _adapters_zcode as zc  # noqa: E402
from index_builder import _builder as builder  # noqa: E402
from index_builder._schema import init_db  # noqa: E402


class TestAntigravityPlanB(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.logs = Path(self._td.name) / "sess-ag-1" / ".system_generated" / "logs"
        self.logs.mkdir(parents=True)
        self.tpath = self.logs / "transcript.jsonl"

    def test_model_from_planner_response_no_hardcode_and_clean_prompt(self):
        records = [
            {
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-11-27T10:00:00Z",
                "content": (
                    "<USER_REQUEST>\n分析缓存命中率\n</USER_REQUEST>\n"
                    "<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-27\n</ADDITIONAL_METADATA>\n"
                    "<USER_SETTINGS_CHANGE>changed</USER_SETTINGS_CHANGE>"
                ),
            },
            {
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "model": "gemini-4-neon-high",
                "created_at": "2026-11-27T10:00:05Z",
                "input_tokens": 100,
                "cache_read_tokens": 400,
                "output_tokens": 50,
                "content": "好的",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "echo ok"}}],
            },
            {
                "type": "GENERIC",
                "source": "MODEL",
                "status": "DONE",
                "created_at": "2026-11-27T10:00:06Z",
                "content": "Command exited with code 0.\n0 errors, no error found in lint",
            },
        ]
        self.tpath.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        parsed = ag.antigravity_parse_session(self.tpath)
        st = parsed["stats"]
        self.assertEqual(st["model"], "gemini-4-neon-high")
        self.assertEqual(st["first_prompt"], "分析缓存命中率")
        self.assertNotIn("ADDITIONAL_METADATA", st["first_prompt"])
        self.assertNotIn("The current local time is:", st["first_prompt"])
        # Healthy output mentioning "error" word must NOT be flagged as error tool
        self.assertEqual(st["errors"], 0)
        self.assertEqual(parsed["tools"][0]["status"], "success")

    def test_absent_model_returns_empty_not_hardcoded_38(self):
        self.tpath.write_text(
            json.dumps({"type": "USER_INPUT", "source": "USER", "content": "hi"}) + "\n"
            + json.dumps({"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "hello"}) + "\n",
            encoding="utf-8",
        )
        with mock.patch.object(ag, "_get_default_model", return_value=""):
            st = ag.antigravity_session_stats(self.tpath)
        self.assertEqual(st["model"], "")


class TestDshPlanB(unittest.TestCase):
    def test_single_pass_tools_and_message_iserror_and_real_preview(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = Path(td.name) / "session.v2.jsonl"
        records = [
            {"type": "session", "time": 1790000000000, "cwd": "/tmp/proj", "agentPreset": "default"},
            {"type": "user/message", "time": 1790000001000,
             "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": "优化查询"}]}},
            {"type": "tool/call", "time": 1790000002000,
             "data": {"callId": "c1", "name": "exec", "arguments": {"cmd": "pytest"}}},
            {"type": "tool/result", "time": 1790000003000,
             "data": {"message": {"role": "tool", "isError": True,
                                  "source": {"kind": "tool", "callId": "c1"},
                                  "content": [{"type": "text", "text": "Permission denied: /etc/shadow"}]}}},
        ]
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        calls = 0
        orig = dsh._iter_jsonl

        def counting_iter(path):
            nonlocal calls
            calls += 1
            return orig(path)

        with mock.patch.object(dsh, "_iter_jsonl", side_effect=counting_iter):
            tools = list(dsh.dsh_extract_tools(p))
        self.assertEqual(calls, 1, "dsh_extract_tools must decompress/read only once")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["status"], "error")
        self.assertIn("Permission denied", tools[0]["result_preview"])

        parsed = dsh.dsh_parse_session(p)
        self.assertEqual(parsed["stats"]["errors"], 1)
        self.assertEqual(parsed["stats"]["session_role"], "main")
        self.assertEqual(parsed["tools"][0]["result_preview"], "Permission denied: /etc/shadow")


class TestZcodeAndDimcodePlanB(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def test_zcode_db_batched_no_limit5_and_role_and_tool_facet(self):
        db = self.tmp / "zcode.sqlite"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE session (id TEXT PRIMARY KEY, slug TEXT, title TEXT, task_type TEXT,
                                  time_created INTEGER, time_updated INTEGER, parent_id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE model_usage (session_id TEXT, model_id TEXT, input_tokens INT, output_tokens INT,
                                      cache_read_input_tokens INT, cache_creation_input_tokens INT,
                                      reasoning_tokens INT, computed_total_tokens INT);
            CREATE TABLE turn_usage (session_id TEXT, input_tokens INT, output_tokens INT);
        """)
        conn.execute("INSERT INTO session VALUES ('s_sub','sub','Sub task','subagent_child',1700000000000,1700000005000,'s_parent','/tmp/myproj')")
        conn.execute("INSERT INTO message VALUES ('m1','s_sub',1700000001000,?)", (json.dumps({"role": "user"}),))
        conn.execute("INSERT INTO message VALUES ('m2','s_sub',1700000002000,?)", (json.dumps({"role": "assistant"}),))
        # 7 text parts on m2 to ensure LIMIT 5 truncation is gone
        for i in range(7):
            conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                         (f"p{i}", "m2", "s_sub", 1700000002000 + i, json.dumps({"type": "text", "text": f"part-{i}"})))
        conn.execute("INSERT INTO part VALUES ('p_user','m1','s_sub',1700000001000,?)",
                     (json.dumps({"type": "text", "text": "优化性能"}),))
        conn.execute("INSERT INTO part VALUES ('p_tool','m2','s_sub',1700000003000,?)",
                     (json.dumps({"type": "tool", "tool": "bash",
                                  "state": {"status": "error", "input": {"cmd": "ls"}, "error": "ENOENT missing"}}),))
        conn.commit()
        conn.close()

        orig_zdb = zc._ZCODE_DB
        zc._ZCODE_DB = db
        self.addCleanup(lambda: setattr(zc, "_ZCODE_DB", orig_zdb))

        msgs = zc.zcode_db_extract_messages("zcode://s_sub", role="assistant", limit=0)
        self.assertEqual(len(msgs), 1)
        self.assertIn("part-6", msgs[0]["text"], "all 7 parts must survive without LIMIT 5 truncation")

        parsed = zc.zcode_db_parse_session("zcode://s_sub")
        self.assertEqual(parsed["stats"]["session_role"], "subagent")
        self.assertEqual(parsed["stats"]["project"], "myproj")
        self.assertEqual(parsed["stats"]["errors"], 1)
        self.assertEqual(parsed["tools"][0]["result_preview"], "ENOENT missing")

    def test_dimcode_tool_use_result_relations_compactions_and_skill_events(self):
        db = self.tmp / "dimcode.sqlite"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE sessions (sessionId TEXT PRIMARY KEY, title TEXT, cwd TEXT, createdAt TEXT, updatedAt TEXT, status TEXT);
            CREATE TABLE messages (messageId TEXT PRIMARY KEY, sessionId TEXT, role TEXT, parts TEXT, toolMetadata TEXT, orderKey INT, createdAt TEXT);
            CREATE TABLE session_relations (relationId TEXT, sourceSessionId TEXT, targetSessionId TEXT, relationType TEXT);
            CREATE TABLE compaction_states (sessionId TEXT);
            CREATE TABLE usage_run_stats (sessionId TEXT, modelId TEXT, inputTokens INT, outputTokens INT, cacheReadTokens INT, cacheWriteTokens INT);
            CREATE TABLE session_states (sessionId TEXT, selectedModelId TEXT);
            CREATE TABLE usage_skill_events (skillName TEXT, sessionId TEXT, createdAt TEXT);
        """)
        conn.execute("INSERT INTO sessions VALUES ('sess_dc1','DimCode Test','/tmp/demo','2026-11-27T01:00:00Z','2026-11-27T01:05:00Z','idle')")
        conn.execute("INSERT INTO messages VALUES ('m1','sess_dc1','user',?,?,1,'2026-11-27T01:00:00Z')",
                     (json.dumps([{"type": "text", "text": "帮我排查报错"}]), None))
        conn.execute("INSERT INTO messages VALUES ('m2','sess_dc1','assistant',?,?,2,'2026-11-27T01:01:00Z')",
                     (json.dumps([{"type": "tool_use", "id": "c1", "name": "exec", "input": {"command": "pytest"}}]), None))
        conn.execute("INSERT INTO messages VALUES ('m3','sess_dc1','tool_result',?,?,3,'2026-11-27T01:02:00Z')",
                     (json.dumps([{"type": "tool_result", "tool_use_id": "c1", "content": "ENOENT: no such file", "is_error": True}]),
                      json.dumps({"toolCallId": "c1", "toolName": "exec", "status": "error"})))
        conn.execute("INSERT INTO session_relations VALUES ('r1','sess_parent','sess_dc1','subagent')")
        conn.execute("INSERT INTO compaction_states VALUES ('sess_dc1')")
        conn.execute("INSERT INTO usage_run_stats VALUES ('sess_dc1','deepseek-v4.1-flash',1000,100,800,0)")
        conn.execute("INSERT INTO usage_skill_events VALUES ('session-digger','sess_dc1','2026-11-27T01:00:00Z')")
        conn.commit()
        conn.close()

        orig_dc = dc.DIMCODE_DB_PATH
        dc.DIMCODE_DB_PATH = db
        self.addCleanup(lambda: setattr(dc, "DIMCODE_DB_PATH", orig_dc))

        parsed = dc.dimcode_parse_session("dimcode://sess_dc1")
        st = parsed["stats"]
        self.assertEqual(st["session_role"], "subagent")
        self.assertEqual(st["compactions"], 1)
        self.assertEqual(st["model"], "deepseek-v4.1-flash")
        self.assertEqual(st["tool_calls"], 1)
        self.assertEqual(st["errors"], 1)
        self.assertEqual(len(parsed["tools"]), 1)
        self.assertEqual(parsed["tools"][0]["status"], "error")
        self.assertIn("ENOENT", parsed["tools"][0]["result_preview"])

        events = dc.dimcode_skill_events()
        self.assertEqual(events[0]["skill"], "session-digger")
        self.assertEqual(events[0]["calls"], 1)

    def test_agent_family_expansion_and_none_stats_safety(self):
        self.assertEqual(echolib.expand_agent_filter("zcode"), ("zcode", "zcode_db", "zcode_v2"))
        self.assertEqual(echolib.expand_agent_filter("dim"), ("dim", "dimcode"))
        self.assertEqual(echolib.expand_agent_filter("zcode_db"), ("zcode_db",))
        self.assertIsNone(echolib.expand_agent_filter("cross"))

        tmp_data = self.tmp / "data"
        tmp_data.mkdir()
        conn = sqlite3.connect(str(tmp_data / "index.db"))
        init_db(conn)
        conn.execute("INSERT INTO sessions (id, agent, modified) VALUES ('s-null', NULL, 'not-ts')")
        conn.commit()
        conn.close()
        env = dict(os.environ, SESSION_DIGGER_DATA_DIR=str(tmp_data))
        res = subprocess.run(
            [sys.executable, str(SCRIPTS / "index-builder.py"), "stats"],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("unknown", res.stdout)


class TestSubskillsAndHealthPlanB(unittest.TestCase):
    def test_deep_analysis_supports_antigravity_and_text(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        logs = Path(td.name) / "ag-sess" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        tp = logs / "transcript.jsonl"
        tp.write_text(
            "\n".join([
                json.dumps({
                    "type": "USER_INPUT", "source": "USER_EXPLICIT",
                    "created_at": "2026-11-27T00:00:00Z",
                    "content": "<USER_REQUEST>为什么测试失败报错</USER_REQUEST><ADDITIONAL_METADATA>ignore</ADDITIONAL_METADATA>",
                }),
                json.dumps({
                    "type": "PLANNER_RESPONSE", "source": "MODEL",
                    "created_at": "2026-11-27T00:00:01Z",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest"}}],
                }),
                json.dumps({
                    "type": "GENERIC", "source": "MODEL", "status": "ERROR",
                    "created_at": "2026-11-27T00:00:02Z",
                    "content": "Command exited with code 1.\nPermission denied: /tmp/locked",
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        erc = REPO_ROOT / "skills" / "deep-analysis" / "scripts" / "error-root-cause.py"
        ic = REPO_ROOT / "skills" / "deep-analysis" / "scripts" / "intent-classify.py"

        r1 = subprocess.run([sys.executable, str(erc), "--jsonl", str(tp)], capture_output=True, text=True)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        d1 = json.loads(r1.stdout)
        self.assertEqual(d1["agent"], "antigravity")
        self.assertEqual(d1["total_errors"], 1)
        self.assertEqual(d1["errors"][0]["error_type"], "PERMISSION_DENIED")

        r2 = subprocess.run([sys.executable, str(ic), "--jsonl", str(tp)], capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        d2 = json.loads(r2.stdout)
        self.assertEqual(d2["intent"], "DEBUGGING")

    def test_skill_health_zero_missing_routes_and_5_subskills(self):
        res = subprocess.run(
            [sys.executable, str(SCRIPTS / "skill-health.py"), "--root", str(REPO_ROOT)],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        report = json.loads(res.stdout)
        self.assertEqual(report["missing_routes"], [])
        self.assertEqual(report["combo_missing"], [])
        self.assertEqual(report["hardcode_hits"], 0)

        sub_dirs = sorted(
            d.name for d in (REPO_ROOT / "skills").iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        self.assertEqual(
            sub_dirs,
            ["deep-analysis", "experience-synthesis", "jsonl-core", "memory-management", "skill-insight"],
        )


if __name__ == "__main__":
    unittest.main()
