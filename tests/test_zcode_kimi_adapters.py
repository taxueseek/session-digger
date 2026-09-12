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
            ("sess_subagent_agent_abc-123", "MiMo-v2.5", 200, 30, 100, 0, 5, 230),
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
        self.assertEqual(mu["MiMo-v2.5"]["input_tokens"], 200)
        self.assertEqual(mu["deepseek-v4-flash"]["model_calls"], 2)
        # primary model = highest input
        self.assertEqual(stats["model"], "deepseek-v4-flash")

        agg = echolib.zcode_aggregate_model_usage()
        self.assertEqual(agg["deepseek-v4-flash"]["input_tokens"], 1500)
        self.assertEqual(agg["MiMo-v2.5"]["model_calls"], 1)
        # 1500/1700 ≈ 0.8824 session; deepseek 1200/1500=0.8; mimo 100/200=0.5
        self.assertEqual(stats["cache_hit_rate"], round(1300 / 1700, 4))
        self.assertEqual(mu["deepseek-v4-flash"]["cache_hit_rate"], round(1200 / 1500, 4))
        self.assertEqual(mu["MiMo-v2.5"]["cache_hit_rate"], 0.5)
        self.assertEqual(agg["deepseek-v4-flash"]["cache_hit_rate"], round(1200 / 1500, 4))

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


class TestZCodeFamilyAndAnalytics(unittest.TestCase):
    """Tests for zcode_aggregate_model_usage (family mode), zcode_family_usage_report,
    zcode_tool_usage_stats, and zcode_turn_usage_stats."""

    def _make_db(self, td, sessions, model_rows, tool_rows=None, turn_rows=None):
        """Create a temp ZCode DB with sessions, model_usage, and optional tool/turn data."""
        import sqlite3 as _sqlite3
        db_path = Path(td) / "db.sqlite"
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row
        conn.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT,
                parent_id TEXT, slug TEXT, directory TEXT, path TEXT,
                title TEXT, version TEXT, share_url TEXT,
                summary_additions INT, summary_deletions INT, summary_files INT,
                summary_diffs TEXT, revert TEXT, permission TEXT,
                time_created INT, time_updated INT, time_compacting INT,
                time_archived INT, task_type TEXT DEFAULT 'interactive',
                title_source TEXT DEFAULT 'default', title_message_id TEXT,
                time_title_updated INT, trace_id TEXT
            );
            CREATE TABLE model_usage (
                id TEXT PRIMARY KEY, logical_request_id TEXT, attempt_index INT DEFAULT 0,
                session_id TEXT, turn_id TEXT, trace_id TEXT, span_id TEXT,
                assistant_message_id TEXT, parent_user_message_id TEXT,
                query_source TEXT, provider_id TEXT, model_id TEXT,
                variant TEXT, agent TEXT, mode TEXT, task_type TEXT,
                status TEXT, started_at INT, first_token_at INT, completed_at INT,
                duration_ms INT, time_to_first_token_ms INT, finish_reason TEXT,
                tool_call_count INT DEFAULT 0,
                input_tokens INT DEFAULT 0, output_tokens INT DEFAULT 0,
                reasoning_tokens INT DEFAULT 0,
                cache_creation_input_tokens INT DEFAULT 0,
                cache_read_input_tokens INT DEFAULT 0,
                provider_total_tokens INT, computed_total_tokens INT DEFAULT 0,
                retry_count INT DEFAULT 0, retryable INT DEFAULT 0,
                cancelled_by_user INT DEFAULT 0, context_exceeded INT DEFAULT 0,
                error_type TEXT, error_code TEXT, error_message TEXT,
                raw_usage_json TEXT, provider_metadata_json TEXT
            );
            CREATE TABLE tool_usage (
                id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
                trace_id TEXT, tool_call_id TEXT, tool_name TEXT,
                side_effect_scope TEXT, read_only INT, destructive INT,
                approval_status TEXT, status TEXT, started_at INT,
                first_output_at INT, completed_at INT, duration_ms INT,
                time_to_first_output_ms INT, exit_code INT,
                output_bytes INT DEFAULT 0, stdout_bytes INT DEFAULT 0,
                stderr_bytes INT DEFAULT 0, truncated INT DEFAULT 0,
                retry_count INT DEFAULT 0, retryable INT DEFAULT 0,
                cancelled_by_user INT DEFAULT 0,
                error_type TEXT, error_code TEXT, error_message TEXT
            );
            CREATE TABLE turn_usage (
                session_id TEXT, turn_id TEXT, trace_id TEXT,
                user_message_id TEXT, status TEXT, started_at INT,
                first_model_start_at INT, first_token_at INT,
                completed_at INT, duration_ms INT, time_to_first_token_ms INT,
                model_request_count INT DEFAULT 0, model_retry_count INT DEFAULT 0,
                tool_call_count INT DEFAULT 0, tool_error_count INT DEFAULT 0,
                input_tokens INT DEFAULT 0, output_tokens INT DEFAULT 0,
                reasoning_tokens INT DEFAULT 0,
                cache_creation_input_tokens INT DEFAULT 0,
                cache_read_input_tokens INT DEFAULT 0,
                computed_total_tokens INT DEFAULT 0, retryable INT DEFAULT 0,
                cancelled_by_user INT DEFAULT 0, context_exceeded INT DEFAULT 0,
                error_type TEXT, error_code TEXT,
                PRIMARY KEY(session_id, turn_id)
            );
        """)
        for s in sessions:
            conn.execute(
                "INSERT INTO session (id, title, task_type, parent_id, time_created, time_updated) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (s["id"], s.get("title", ""), s.get("task_type", "interactive"),
                 s.get("parent_id"), s.get("tc", 0), s.get("tu", 0)),
            )
        for i, r in enumerate(model_rows):
            conn.execute(
                "INSERT INTO model_usage (id, session_id, model_id, input_tokens, output_tokens, "
                "cache_read_input_tokens, cache_creation_input_tokens, computed_total_tokens, "
                "status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', 0)",
                (f"mu_{i}", r[0], r[1], r[2], r[3], r[4], r[5], r[6]),
            )
        if tool_rows:
            for i, r in enumerate(tool_rows):
                conn.execute(
                    "INSERT INTO tool_usage (id, session_id, tool_call_id, tool_name, status, "
                    "started_at, duration_ms, read_only, destructive) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (f"tu_{i}", r[0], f"tc_{i}", r[1], r[2], r[3],
                     r[4] if len(r) > 4 else 0, r[5] if len(r) > 5 else 0),
                )
        if turn_rows:
            for i, r in enumerate(turn_rows):
                conn.execute(
                    "INSERT INTO turn_usage (session_id, turn_id, status, started_at, "
                    "duration_ms, input_tokens, output_tokens, reasoning_tokens, "
                    "tool_call_count, tool_error_count) "
                    "VALUES (?, ?, 'completed', 0, ?, ?, ?, ?, ?, ?)",
                    (r[0], f"turn_{i}", r[1], r[2], r[3], r[4], r[5], r[6]),
                )
        conn.commit()
        conn.close()
        return db_path

    def _patch_db(self, db_path):
        """Temporarily patch _ZCODE_DB to point at our test DB."""
        import echolib._adapters_zcode as zc
        old = zc._ZCODE_DB
        zc._ZCODE_DB = db_path
        self.addCleanup(lambda: setattr(zc, "_ZCODE_DB", old))

    def test_aggregate_family_mode(self):
        """Family mode: parent + children merged; children skipped."""
        import tempfile
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))

        sessions = [
            {"id": "sess_parent", "title": "Parent", "task_type": "interactive", "tc": 1, "tu": 2},
            {"id": "sess_child1", "title": "Child 1", "task_type": "subagent_child",
             "parent_id": "sess_parent", "tc": 1, "tu": 2},
            {"id": "sess_child2", "title": "Child 2", "task_type": "subagent_child",
             "parent_id": "sess_parent", "tc": 1, "tu": 2},
            {"id": "sess_standalone", "title": "Solo", "task_type": "interactive", "tc": 3, "tu": 4},
        ]
        model_rows = [
            ("sess_parent", "deepseek-v4-flash", 5000, 200, 4000, 0, 5200),
            ("sess_child1", "deepseek-v4-flash", 3000, 100, 2500, 0, 3100),
            ("sess_child2", "mimo-v2.5", 1000, 50, 800, 0, 1050),
            ("sess_standalone", "deepseek-v4-flash", 2000, 80, 1500, 0, 2080),
        ]
        db_path = self._make_db(td, sessions, model_rows)
        self._patch_db(db_path)

        # Family mode: parent includes children, standalone counted separately
        agg = echolib.zcode_aggregate_model_usage(mode="family")
        # deepseek: parent(5000) + child1(3000) + standalone(2000) = 10000
        self.assertEqual(agg["deepseek-v4-flash"]["input_tokens"], 10000)
        # mimo: child2(1000) — normalized to MiMo-v2.5
        self.assertEqual(agg["MiMo-v2.5"]["input_tokens"], 1000)

        # Session mode: skip children
        agg_s = echolib.zcode_aggregate_model_usage(mode="session")
        self.assertEqual(agg_s["deepseek-v4-flash"]["input_tokens"], 7000)  # parent + standalone
        self.assertNotIn("MiMo-v2.5", agg_s)  # child2 skipped

        # Raw mode: everything counted (double-count)
        agg_r = echolib.zcode_aggregate_model_usage(mode="raw")
        self.assertEqual(agg_r["deepseek-v4-flash"]["input_tokens"], 10000)

    def test_family_usage_report_rollup(self):
        """Rollup: parent usage >= children → parent is family-wide."""
        import tempfile
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))

        sessions = [
            {"id": "sess_parent", "title": "Parent", "task_type": "interactive", "tc": 1, "tu": 2},
            {"id": "sess_child1", "title": "Child 1", "task_type": "subagent_child",
             "parent_id": "sess_parent", "tc": 1, "tu": 2},
        ]
        # Parent input (5000) > child input (3000) → rollup
        model_rows = [
            ("sess_parent", "deepseek-v4-flash", 5000, 200, 4000, 0, 5200),
            ("sess_child1", "deepseek-v4-flash", 3000, 100, 2500, 0, 3100),
        ]
        db_path = self._make_db(td, sessions, model_rows)
        self._patch_db(db_path)

        report = echolib.zcode_family_usage_report("sess_parent")
        self.assertEqual(report["accounting"], "rollup")
        self.assertEqual(report["subagent_count"], 1)
        self.assertEqual(report["parent"]["input_tokens"], 5000)
        self.assertEqual(report["family_total"]["input_tokens"], 5000)  # parent is family-wide
        self.assertEqual(report["subagents_total"]["input_tokens"], 3000)
        # main_only = parent - children = 2000
        self.assertEqual(report["main_only"]["input_tokens"], 2000)

    def test_family_usage_report_separate(self):
        """Separate: child input > parent input for a model → separate."""
        import tempfile
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))

        sessions = [
            {"id": "sess_parent", "title": "Parent", "task_type": "interactive", "tc": 1, "tu": 2},
            {"id": "sess_child1", "title": "Child 1", "task_type": "subagent_child",
             "parent_id": "sess_parent", "tc": 1, "tu": 2},
        ]
        # Parent input (1000) < child input (3000) → separate
        model_rows = [
            ("sess_parent", "deepseek-v4-flash", 1000, 50, 800, 0, 1050),
            ("sess_child1", "deepseek-v4-flash", 3000, 100, 2500, 0, 3100),
        ]
        db_path = self._make_db(td, sessions, model_rows)
        self._patch_db(db_path)

        report = echolib.zcode_family_usage_report("sess_parent")
        self.assertEqual(report["accounting"], "separate")
        self.assertEqual(report["family_total"]["input_tokens"], 4000)  # parent + child
        self.assertEqual(report["main_only"]["input_tokens"], 1000)

    def test_family_usage_report_standalone(self):
        """Standalone: no children."""
        import tempfile
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))

        sessions = [
            {"id": "sess_alone", "title": "Alone", "task_type": "interactive", "tc": 1, "tu": 2},
        ]
        model_rows = [
            ("sess_alone", "deepseek-v4-flash", 2000, 80, 1500, 0, 2080),
        ]
        db_path = self._make_db(td, sessions, model_rows)
        self._patch_db(db_path)

        report = echolib.zcode_family_usage_report("sess_alone")
        self.assertEqual(report["accounting"], "standalone")
        self.assertEqual(report["subagent_count"], 0)
        self.assertEqual(report["family_total"]["input_tokens"], 2000)
        self.assertEqual(report["main_only"]["input_tokens"], 2000)

    def test_tool_usage_stats(self):
        """Tool usage stats aggregation."""
        import tempfile
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))

        sessions = [{"id": "sess_t1", "title": "T1", "task_type": "interactive", "tc": 1, "tu": 2}]
        tool_rows = [
            ("sess_t1", "Bash", "completed", 1500, 0, 0),
            ("sess_t1", "Bash", "completed", 2000, 0, 0),
            ("sess_t1", "Bash", "error", 500, 0, 0),
            ("sess_t1", "Read", "completed", 10, 1, 0),
            ("sess_t1", "Read", "completed", 5, 1, 0),
        ]
        db_path = self._make_db(td, sessions, [], tool_rows=tool_rows)
        self._patch_db(db_path)

        stats = echolib.zcode_tool_usage_stats(session_id="sess_t1")
        self.assertEqual(stats["total_calls"], 5)
        self.assertEqual(stats["error_count"], 1)
        self.assertEqual(len(stats["tools"]), 2)

        bash = next(t for t in stats["tools"] if t["name"] == "Bash")
        self.assertEqual(bash["calls"], 3)
        self.assertEqual(bash["errors"], 1)
        self.assertAlmostEqual(bash["error_rate"], 1 / 3, places=2)
        self.assertEqual(bash["p50_duration_ms"], 1500)
        self.assertEqual(bash["read_only_count"], 0)

        read = next(t for t in stats["tools"] if t["name"] == "Read")
        self.assertEqual(read["calls"], 2)
        self.assertEqual(read["errors"], 0)
        self.assertEqual(read["read_only_count"], 2)

    def test_tool_usage_stats_global(self):
        """Tool usage stats without session_id → global."""
        import tempfile
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))

        sessions = [
            {"id": "s1", "title": "S1", "task_type": "interactive", "tc": 1, "tu": 2},
            {"id": "s2", "title": "S2", "task_type": "interactive", "tc": 3, "tu": 4},
        ]
        tool_rows = [
            ("s1", "Bash", "completed", 100, 0, 0),
            ("s2", "Bash", "completed", 200, 0, 0),
            ("s2", "Read", "completed", 10, 1, 0),
        ]
        db_path = self._make_db(td, sessions, [], tool_rows=tool_rows)
        self._patch_db(db_path)

        stats = echolib.zcode_tool_usage_stats()
        self.assertEqual(stats["total_calls"], 3)
        bash = next(t for t in stats["tools"] if t["name"] == "Bash")
        self.assertEqual(bash["calls"], 2)

    def test_turn_usage_stats(self):
        """Turn usage stats aggregation."""
        import tempfile
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))

        sessions = [{"id": "sess_turn", "title": "T", "task_type": "interactive", "tc": 1, "tu": 2}]
        turn_rows = [
            ("sess_turn", 5000, 1500, 0, 0, 3, 0),
            ("sess_turn", 8000, 10000, 0, 0, 5, 1),
            ("sess_turn", 3000, 3500, 0, 0, 1, 0),
        ]
        db_path = self._make_db(td, sessions, [], turn_rows=turn_rows)
        self._patch_db(db_path)

        stats = echolib.zcode_turn_usage_stats(session_id="sess_turn")
        self.assertEqual(stats["total_turns"], 3)
        self.assertEqual(stats["error_turns"], 1)
        self.assertEqual(len(stats["turns"]), 3)
        # avg tokens = (1500+10000+3500)/3 = 5000
        self.assertEqual(stats["avg_tokens_per_turn"], 5000)
        self.assertAlmostEqual(stats["avg_tool_calls_per_turn"], 3.0, places=1)

    def test_aggregate_scoped_to_session_ids(self):
        """Aggregate scoped to specific session_ids."""
        import tempfile
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))

        sessions = [
            {"id": "s1", "title": "S1", "task_type": "interactive", "tc": 1, "tu": 2},
            {"id": "s2", "title": "S2", "task_type": "interactive", "tc": 3, "tu": 4},
        ]
        model_rows = [
            ("s1", "deepseek-v4-flash", 1000, 50, 800, 0, 1050),
            ("s2", "mimo-v2.5", 2000, 80, 1500, 0, 2080),
        ]
        db_path = self._make_db(td, sessions, model_rows)
        self._patch_db(db_path)

        agg = echolib.zcode_aggregate_model_usage(session_ids=["s1"], mode="session")
        self.assertEqual(agg["deepseek-v4-flash"]["input_tokens"], 1000)
        self.assertNotIn("mimo-v2.5", agg)


if __name__ == "__main__":
    unittest.main()
