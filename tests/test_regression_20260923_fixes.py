"""Regression pins for the 2026-09-23 hunt+check batch (red-green).

Each test reproduces a failure that existed before the fix:

1. bug A — `topic-scan` dropped every `dimcode://` session it was handed: the
   existence filter special-cased only `zcode://`, and `os.path.exists` is
   False for any URI. Measured on the live install: 20 of the top-200
   discovered sessions were silently skipped (dimcode is 73% of the index).
2. bug B — `recall-lite`'s report loop dropped the same sessions for the same
   reason, so dimcode never appeared in a lite evidence block.
3. bug C — `sd-recall stats --agent X` printed the GLOBAL messages_fts count
   next to a per-agent session count (dimcode: 4593 sessions / 70620 messages,
   where dimcode actually holds 32204).
4. bug D — `report_empty_search` told a 14-word query "this term has no
   searchable content (punctuation or whitespace only)": two unrelated
   rejection reasons shared one message, and the advice was the opposite of
   the fix.
5. bug E — a UTF-8 BOM made `_iter_jsonl` lose the whole first line (3 records
   read as 2, first user turn gone, first_prompt shifted).
6. bug F — `recent_sessions` counted sessions with no date as inside every time
   window while `global_aggregates` excluded them: 962 vs 903 for the same
   "last 7 days", both rendered in one deep-analyze report.
7. perf A — `topic-scan` re-parsed every session three times (stats / tools /
   messages) instead of reading the index: 7.5 s where the index answers in
   0.02 s.
8. perf B — `zcode_db_session_stats` shipped every message's `data` blob into
   Python to count roles, and scanned the session's parts twice.
9. schema — a fresh install had no input/output/cache token columns at all,
   while an upgraded DB had three of them permanently stuck at 0 (never
   written, never read): "cost from the index" silently meant $0.00.
10. zcode 入库 — the ZCode SQLite store (1195 sessions) was not indexed at all:
   `zcode://` had no path marker, so `dispatch_resolve_agent` fell through to
   `universal` and read a URI as a file → 0 messages, silently. And the generic
   virtual-scheme fingerprint is the URI's own hash, so even after registering
   the adapter every session would be indexed once and then frozen forever.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SD = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SD / "scripts"))

from index_builder import _builder as builder
from index_builder import _reader
from index_builder._cjk import MAX_TERMS, build_match_query, query_rejection
from index_builder._schema import _MIGRATIONS, _SCHEMA_VERSION, init_db
from echolib._helpers import _iter_jsonl


def _run(args, **kw):
    return subprocess.run(
        [sys.executable] + [str(a) for a in args],
        capture_output=True, text=True, cwd=str(SD), **kw,
    )


# ---------------------------------------------------------------------------
# 1/2/3 — virtual paths, and the count that must match its own scope
# ---------------------------------------------------------------------------

class VirtualPathTest(unittest.TestCase):

    def test_uri_schemes_are_virtual_and_files_are_not(self):
        self.assertTrue(_reader.is_virtual_path("dimcode://sess_x"))
        self.assertTrue(_reader.is_virtual_path("zcode://abc"))
        self.assertFalse(_reader.is_virtual_path("/Users/x/a.jsonl"))
        self.assertFalse(_reader.is_virtual_path("file:///Users/x/a.jsonl"))

    def test_topic_scan_does_not_drop_dimcode_sessions(self):
        """bug A: the existence filter must ask the path predicate, not zcode only."""
        import topic_classify as tc

        seen = []

        def fake_stats(path):
            seen.append(path)
            return {"user_messages": 1, "assistant_messages": 1,
                    "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
                    "summary": "技能开发 skill", "started": "2026-09-01T00:00:00Z",
                    "ended": "2026-09-01T01:00:00Z", "tool_calls": 0,
                    "errors": 0, "first_prompt": "skill 开发"}

        tc.get_all_sessions = lambda limit=500: [
            {"full_path": "dimcode://sess_virtual", "created": "2026-09-01T00:00:00Z"},
            {"full_path": "/nonexistent/real-gone.jsonl", "created": "2026-09-01T00:00:00Z"},
        ]
        tc.dispatch_stats = fake_stats
        tc.dispatch_tools = lambda path, limit=30: []
        tc.dispatch_messages = lambda path, role="user", limit=5: []
        try:
            sessions, _stats = tc.scan_and_classify(limit=10)
        finally:
            import importlib
            importlib.reload(tc)

        self.assertIn("dimcode://sess_virtual", seen,
                      "a virtual session must reach dispatch_stats")
        self.assertNotIn("/nonexistent/real-gone.jsonl", seen,
                         "a missing file must still be skipped")
        self.assertTrue(sessions, "the virtual session must be classified")

    def test_lite_report_keeps_virtual_rows(self):
        """bug B: lite-report skipped dimcode:// rows via os.path.exists."""
        row = "sid\t2026-09-01\t2026-09-01\t4\tmain\tdimcode\tdimcode://sess_virtual\n"
        r = _run([SD / "scripts" / "sd-recall.py", "lite-report", "--limit", "5"],
                 input=row)
        self.assertIn("dimcode://sess_virtual", r.stdout,
                      "the virtual row must be rendered, not dropped")

    def test_stats_counts_messages_in_the_same_scope_as_sessions(self):
        """bug C: --agent X paired a per-agent session count with a global count."""
        import importlib.util
        import shutil

        tmp = tempfile.mkdtemp(prefix="sd-scope-")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = Path(tmp) / "index.db"
        conn = sqlite3.connect(str(db))
        init_db(conn)
        for sid, agent, n in (("a:1", "dimcode", 3), ("b:1", "grok", 5)):
            conn.execute("INSERT OR REPLACE INTO sessions (id, agent) VALUES (?,?)",
                         (sid, agent))
            for i in range(n):
                conn.execute("INSERT INTO messages_fts (session_id, role, timestamp,"
                             " text) VALUES (?,?,?,?)", (sid, "USER", "t", f"x{i}"))
        conn.commit()
        conn.close()

        env = dict(os.environ, SESSION_DIGGER_DATA_DIR=tmp)
        out = _run([SD / "scripts" / "sd-recall.py", "stats", "--agent", "dimcode"],
                   env=env).stdout
        self.assertIn("3 messages indexed", out,
                      f"dimcode holds 3 FTS rows; got: {out.strip().splitlines()[-1:]}")


# ---------------------------------------------------------------------------
# 4 — two rejection reasons, two messages
# ---------------------------------------------------------------------------

class QueryRejectionTest(unittest.TestCase):

    def test_reasons_are_distinguished(self):
        """bug D: '!!!' and a 14-word query both said 'no searchable content'."""
        self.assertEqual(query_rejection("!!!"), "empty")
        self.assertEqual(query_rejection("   "), "empty")
        self.assertEqual(query_rejection(""), "empty")
        long_query = " ".join(f"word{i}" for i in range(MAX_TERMS + 2))
        self.assertEqual(query_rejection(long_query), "too_many")
        self.assertEqual(query_rejection("索引 重建"), "")
        # both still refuse to build a MATCH expression
        self.assertIsNone(build_match_query("!!!"))
        self.assertIsNone(build_match_query(long_query))
        self.assertIsNotNone(build_match_query("索引 重建"))

    def test_cli_tells_a_long_query_to_shorten_it(self):
        long_query = " ".join(f"word{i}" for i in range(MAX_TERMS + 2))
        out = _run([SD / "scripts" / "sd-recall.py", "search", long_query,
                    "--scope", "all"]).stdout
        self.assertIn("more than", out)
        self.assertNotIn("no searchable content", out)


# ---------------------------------------------------------------------------
# 5 — BOM
# ---------------------------------------------------------------------------

class BomTest(unittest.TestCase):

    def test_first_record_survives_a_bom(self):
        """bug E: a BOM made the first line unparseable and silently dropped."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "bom.jsonl"
        body = [{"role": "user", "content": "第一条"},
                {"role": "assistant", "content": "回答"},
                {"role": "user", "content": "第二条"}]
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbf")
            for rec in body:
                f.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        got = list(_iter_jsonl(p))
        self.assertEqual(len(got), 3, "all three records must be read")
        self.assertEqual(got[0]["content"], "第一条")

    def test_no_bom_behaviour_is_unchanged(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "plain.jsonl"
        p.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
        self.assertEqual([r["a"] for r in _iter_jsonl(p)], [1, 2])


# ---------------------------------------------------------------------------
# 6 — one time-window predicate
# ---------------------------------------------------------------------------

class WindowPredicateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "index.db"
        self._orig = _reader.DB_PATH
        _reader.DB_PATH = self.db
        self.addCleanup(setattr, _reader, "DB_PATH", self._orig)
        conn = sqlite3.connect(str(self.db))
        init_db(conn)
        conn.execute("INSERT OR REPLACE INTO sessions (id, created, message_count)"
                     " VALUES ('dated', '2099-01-01T00:00:00Z', 5)")
        conn.execute("INSERT OR REPLACE INTO sessions (id, created, message_count)"
                     " VALUES ('undated', '', 5)")
        conn.commit()
        conn.close()

    def test_selection_and_aggregate_agree(self):
        """bug F: 962 candidates vs 903 sessions for the same window."""
        picked = _reader.recent_sessions(days=7, limit=100, min_messages=0,
                                         sort="recent")
        agg = _reader.global_aggregates(days=7)
        self.assertEqual(len(picked), agg["totals"]["sessions"],
                         "the candidate pool and the window total must agree")
        self.assertNotIn("undated", {r["id"] for r in picked})

    def test_all_time_still_reaches_undated_sessions(self):
        picked = _reader.recent_sessions(days=0, limit=100, min_messages=0)
        self.assertIn("undated", {r["id"] for r in picked},
                      "days=0 is not a window, so nothing is excluded")


# ---------------------------------------------------------------------------
# 7 — index-first topic-scan
# ---------------------------------------------------------------------------

class IndexFirstTopicScanTest(unittest.TestCase):

    def setUp(self):
        import topic_classify as tc
        self.tc = tc
        self.addCleanup(tc._BUNDLES.clear)
        self.addCleanup(tc._BUNDLES_ASKED.clear)
        tc._BUNDLES.clear()
        tc._BUNDLES_ASKED.clear()

    def test_stats_come_from_the_index_without_parsing(self):
        """perf A: three adapter parses per session became one batched read."""
        calls = []
        self.tc.fresh_session_bundles = lambda paths: {
            "/x/s.jsonl": {
                "created": "2026-09-01T00:00:00Z", "modified": "", "user_messages": 2,
                "assistant_messages": 3, "tool_calls": 4, "errors": 1,
                "total_tokens": 100, "input_tokens": 40, "output_tokens": 60,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "summary": "s",
                "first_prompt": "p", "model": "m", "branch": "b",
                "tool_usage_json": '{"Read": 2}', "user_evidence_json": "[]",
                "jsonl_mtime": 1.0,
            }
        }
        self.addCleanup(lambda: setattr(
            self.tc, "fresh_session_bundles", _reader.fresh_session_bundles))
        import echolib
        orig = echolib.dispatch_session_stats
        echolib.dispatch_session_stats = lambda p: calls.append(p) or {}
        self.addCleanup(setattr, echolib, "dispatch_session_stats", orig)

        st = self.tc.dispatch_stats("/x/s.jsonl")
        self.assertEqual(st["user_messages"], 2)
        self.assertEqual(st["input_tokens"], 40)
        self.assertEqual(st["output_tokens"], 60)
        self.assertEqual(calls, [], "the index answered; no parse should happen")
        self.assertEqual([t["name"] for t in self.tc.dispatch_tools("/x/s.jsonl")],
                         ["Read"])

    def test_row_without_a_token_split_falls_back(self):
        """perf B / schema: a 0/0 split would make every cost read $0.00."""
        self.tc.fresh_session_bundles = lambda paths: {
            "/x/s.jsonl": {"total_tokens": 500, "input_tokens": 0, "output_tokens": 0,
                           "user_evidence_json": "[]", "tool_usage_json": "{}"}
        }
        self.addCleanup(lambda: setattr(
            self.tc, "fresh_session_bundles", _reader.fresh_session_bundles))
        self.assertFalse(self.tc._cost_ready(
            {"total_tokens": 500, "input_tokens": 0, "output_tokens": 0}))
        self.assertTrue(self.tc._cost_ready(
            {"total_tokens": 500, "input_tokens": 200, "output_tokens": 300}))
        self.assertTrue(self.tc._cost_ready(
            {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0}),
            "a session with no usage is honestly free, not unanswerable")

    def test_truncated_evidence_projection_falls_back(self):
        """The display cap (300 chars) must not silently shrink classification input."""
        from index_builder._evidence import EVIDENCE_TEXT_CAP
        b = {"user_evidence_json": json.dumps(
            [{"ts": "", "text": "x" * EVIDENCE_TEXT_CAP, "decision": False}])}
        self.assertIsNone(self.tc._user_messages_from_bundle(b, 5))
        b2 = {"user_evidence_json": json.dumps(
            [{"ts": "", "text": "short enough", "decision": False}])}
        self.assertEqual(len(self.tc._user_messages_from_bundle(b2, 5)), 1)


# ---------------------------------------------------------------------------
# 8/9 — freshness, the token columns, and the row shape
# ---------------------------------------------------------------------------

class FreshnessTest(unittest.TestCase):

    def test_virtual_paths_are_trusted_without_a_stat(self):
        """A DB-backed session has no file: calling it stale re-parses it forever."""
        self.assertTrue(_reader.is_index_row_fresh("dimcode://sess_x", 123.0))

    def test_a_file_that_grew_is_not_fresh(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "s.jsonl"
        p.write_text("{}", encoding="utf-8")
        mtime = os.path.getmtime(p)
        self.assertTrue(_reader.is_index_row_fresh(str(p), mtime))
        self.assertFalse(_reader.is_index_row_fresh(str(p), mtime - 60))
        self.assertFalse(_reader.is_index_row_fresh(str(p), None))
        self.assertFalse(_reader.is_index_row_fresh(str(Path(tmp.name) / "gone"), mtime))

    def test_fresh_session_bundles_skips_a_missing_column(self):
        """A v4 DB has no output_tokens; the whole SELECT must not fail."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "index.db"
        orig = _reader.DB_PATH
        _reader.DB_PATH = db
        self.addCleanup(setattr, _reader, "DB_PATH", orig)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, jsonl_path TEXT,"
                     " created TEXT, user_messages INTEGER, jsonl_mtime REAL)")
        conn.execute("INSERT INTO sessions VALUES ('a', 'dimcode://s', '2026-09-01', 2, 5.0)")
        conn.commit()
        conn.close()
        got = _reader.fresh_session_bundles(["dimcode://s"])
        self.assertIn("dimcode://s", got)
        self.assertEqual(got["dimcode://s"]["user_messages"], 2)
        self.assertIsNone(got["dimcode://s"]["output_tokens"],
                          "an absent column reads as 'not computed', not as 0")


class TokenColumnTest(unittest.TestCase):

    def test_fresh_install_has_the_token_columns(self):
        """schema: a fresh DB had none of them; upgraded DBs had three dead ones."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = sqlite3.connect(str(Path(tmp.name) / "fresh.db"))
        init_db(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        for c in ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens"):
            self.assertIn(c, cols)
        conn.close()
        self.assertIn("5", _MIGRATIONS)
        self.assertGreaterEqual(int(_SCHEMA_VERSION), 5)

    def test_row_shape_matches_the_insert_statement(self):
        """The row width used to be typed twice (SQL and a test) and drifted."""
        sql = builder.SESSION_INSERT_SQL
        self.assertEqual(sql.count("?"), len(builder.SESSION_INSERT_COLUMNS))
        for c in ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens"):
            self.assertIn(c, builder.SESSION_INSERT_COLUMNS)
        self.assertIn("v10", builder._PARSER_EPOCH,
                      "an epoch bump is what repopulates existing rows once")


# ---------------------------------------------------------------------------
# 10 — zcode 入库
# ---------------------------------------------------------------------------

ZCODE_SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, slug TEXT, title TEXT, task_type TEXT,
                      time_created INTEGER, time_updated INTEGER, parent_id TEXT);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
                      data TEXT);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                   time_created INTEGER, data TEXT);
CREATE TABLE turn_usage (session_id TEXT, input_tokens INTEGER, output_tokens INTEGER);
"""


class ZcodeIndexingTest(unittest.TestCase):
    """zcode:// 必须能被解析、能被索引，且指纹随 DB 内容变化。

    没有专用指纹时，``_file_fingerprint`` 的虚拟路径兜底返回路径自身的哈希：
    恒定 ⇒ 每轮构建都 skip ⇒ 会话被冻结在第一次那份，而它还在继续增长。
    """

    def setUp(self):
        import shutil
        import echolib._adapters_zcode as zmod

        self.tmp = tempfile.mkdtemp(prefix="zcode-idx-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Path(self.tmp) / "db.sqlite"
        self.idx = Path(self.tmp) / "index.db"
        self.sid = "sess_fixture_1"
        self._write_db(msgs=2)

        self._orig = {
            "builder_db": builder._ZCODE_DB_PATH,
            "builder_path": builder.DB_PATH,
            "builder_dir": builder.DB_DIR,
            "adapter_db": zmod._ZCODE_DB,
            "scan": builder.scan_sessions,
            "jobs": os.environ.get("SESSION_DIGGER_JOBS"),
        }
        builder._ZCODE_DB_PATH = self.db
        builder.DB_PATH = self.idx
        builder.DB_DIR = Path(self.tmp)
        zmod._ZCODE_DB = self.db
        # 扫描面收窄到一个 fixture 会话。两件事同时解决：测试不再扫真实语料
        # （实测 61 秒），而且任务数 < 24 让 _map_sessions 走串行——**在进程池
        # 里上面这些模块级 patch 会在子进程里消失**，构建会悄悄读到真实的 zcode
        # 库、把 fixture 会话解析成 0 条消息（这正是本测试第一次跑红的原因）。
        builder.scan_sessions = lambda *a, **k: [
            (f"zcode_db:{self.sid}", f"zcode://{self.sid}", "zcode_db")
        ]
        os.environ["SESSION_DIGGER_JOBS"] = "1"
        builder._ZCODE_FP_CACHE["key"] = None
        self.addCleanup(self._restore)

    def _restore(self):
        import echolib._adapters_zcode as zmod
        builder._ZCODE_DB_PATH = self._orig["builder_db"]
        builder.DB_PATH = self._orig["builder_path"]
        builder.DB_DIR = self._orig["builder_dir"]
        zmod._ZCODE_DB = self._orig["adapter_db"]
        builder.scan_sessions = self._orig["scan"]
        if self._orig["jobs"] is None:
            os.environ.pop("SESSION_DIGGER_JOBS", None)
        else:
            os.environ["SESSION_DIGGER_JOBS"] = self._orig["jobs"]
        builder._ZCODE_FP_CACHE["key"] = None
        builder._ZCODE_FP_MAP = None

    def _write_db(self, msgs, updated=1700000000000):
        conn = sqlite3.connect(str(self.db))
        try:
            conn.executescript(ZCODE_SCHEMA)
            conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                         (self.sid, "fixture", "fixture title", None,
                          1699999999000, updated, None))
            for i in range(msgs):
                mid = f"m{i}"
                role = "user" if i % 2 == 0 else "assistant"
                conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                             (mid, self.sid, 1699999999000 + i,
                              json.dumps({"role": role})))
                conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                             (f"p{i}", mid, self.sid, 1699999999000 + i,
                              json.dumps({"type": "text",
                                          "text": f"fixture message {i}"})))
            conn.commit()
        finally:
            conn.close()

    def _build(self):
        return builder.build_index(rebuild=False, agent_filter="cross")

    def _session_row(self):
        conn = sqlite3.connect(str(self.idx))
        try:
            return conn.execute(
                "SELECT message_count, jsonl_path, input_tokens, output_tokens"
                " FROM sessions WHERE jsonl_path = ?",
                (f"zcode://{self.sid}",)).fetchone()
        finally:
            conn.close()

    def test_virtual_scheme_resolves_to_the_db_adapter(self):
        """A URI that resolves to `universal` is read as a file and yields nothing."""
        import echolib
        self.assertEqual(echolib.dispatch_resolve_agent(f"zcode://{self.sid}"),
                         "zcode_db")

    def test_adapter_accepts_the_uri_and_a_bare_id(self):
        """dispatch passes the URI; topic_classify passes a stripped id."""
        import echolib
        uri = f"zcode://{self.sid}"
        for arg in (uri, self.sid):
            st = echolib.zcode_db_session_stats(arg)
            self.assertEqual(st["user_messages"], 1, f"arg={arg}")
            self.assertEqual(st["assistant_messages"], 1, f"arg={arg}")
        self.assertEqual(
            len(list(echolib.dispatch_extract_messages(uri, role="both"))), 2)

    def test_tool_limit_zero_means_unlimited(self):
        """`limit=0` is the adapters' "no cap"; SQL LIMIT 0 would return nothing."""
        import echolib
        uri = f"zcode://{self.sid}"
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                     ("pt", "m1", self.sid, 1699999999100,
                      json.dumps({"type": "tool", "tool": "bash",
                                  "state": {"status": "ok", "output": "ok"}})))
        conn.commit()
        conn.close()
        builder._ZCODE_FP_CACHE["key"] = None
        self.assertEqual(len(list(echolib.dispatch_extract_tools(uri, limit=0))), 1)
        self.assertEqual(len(list(echolib.dispatch_extract_tools(uri, limit=5))), 1)

    def test_session_is_indexed_with_fts_rows(self):
        result = self._build()
        self.assertEqual(result["errors"], 0)
        row = self._session_row()
        self.assertIsNotNone(row, "the zcode:// session must reach the index")
        self.assertEqual(row[0], 2)
        conn = sqlite3.connect(str(self.idx))
        try:
            n = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 2, "both messages must be searchable")

    def test_unchanged_db_is_skipped(self):
        self._build()
        again = self._build()
        self.assertEqual(again["indexed"], 0,
                         "a second build with no DB change must re-parse nothing")
        self.assertEqual(again["skipped"], 1)

    def test_new_message_forces_a_reparse(self):
        """The whole point of the per-session fingerprint."""
        self._build()
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                     ("m9", self.sid, 1700000000500,
                      json.dumps({"role": "user"})))
        conn.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                     ("p9", "m9", self.sid, 1700000000500,
                      json.dumps({"type": "text", "text": "a new turn"})))
        conn.commit()
        conn.close()
        builder._ZCODE_FP_CACHE["key"] = None
        again = self._build()
        self.assertEqual(again["indexed"], 1,
                         "a session that grew must be re-parsed")
        self.assertEqual(self._session_row()[0], 3)

    def test_fingerprint_moves_with_message_count_and_updated_at(self):
        builder._ZCODE_FP_CACHE["key"] = None
        before = builder._zcode_session_fingerprints()[self.sid]
        conn = sqlite3.connect(str(self.db))
        conn.execute("UPDATE session SET time_updated = ? WHERE id = ?",
                     (1700000009000, self.sid))
        conn.commit()
        conn.close()
        builder._ZCODE_FP_CACHE["key"] = None
        after = builder._zcode_session_fingerprints()[self.sid]
        self.assertNotEqual(before[1], after[1], "time_updated must move the hash")
        self.assertAlmostEqual(after[0], 1700000009.0, places=1,
                               msg="millisecond epoch must become seconds")


class CodexSidecarTest(unittest.TestCase):
    """`rollout-*.jsonl` 的前导通配符会吞掉中间那截。

    `rollout-X.jsonl.summary.jsonl` 匹配 `rollout-*.jsonl`（`*` = `X.jsonl.summary`），
    于是 save-summary 写在会话旁的侧车文件被当成一个 rollout 列出来（实测索引里
    1 行、msgs=0）。谓词与 v0.9.26 在另外四处用的是同一个：名字含 `.jsonl.` 即非本体。
    """

    def test_rollout_glob_does_not_match_sidecars(self):
        import shutil
        import echolib._adapters as A

        tmp = Path(tempfile.mkdtemp(prefix="codex-sidecar-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sess = tmp / "sessions" / "2026" / "08" / "25"
        sess.mkdir(parents=True)
        real = sess / "rollout-2026-08-25T23-22-07-abc.jsonl"
        real.write_text('{"type":"user"}\n', encoding="utf-8")
        sidecar = sess / "rollout-2026-08-25T23-22-07-abc.jsonl.summary.jsonl"
        sidecar.write_text('{"summary":"x"}\n', encoding="utf-8")

        orig = A._codex_homes
        A._codex_homes = lambda: [tmp]
        self.addCleanup(setattr, A, "_codex_homes", orig)

        got = [p.name for p in A._iter_codex_rollouts()]
        self.assertIn(real.name, got)
        self.assertNotIn(sidecar.name, got,
                         "a *.summary.jsonl sidecar is not a rollout")


if __name__ == "__main__":
    unittest.main()
