"""Regression pins for the 2026-09-19 hunt+check batch (red-green).

Each test reproduces a failure that existed before the fix:

1. bug A  — recall-lite.sh passes --agent auto; without the alias argparse
            rejected the whole lite recall chain (exit 2, 100% failure).
2. bug B  — recall-lite.sh --decisions ran `sys.path.insert(0, dirname(
            ES_SCRIPT_DIR))` without even exporting ES_SCRIPT_DIR, so
            `import echolib` always raised ModuleNotFoundError.
3. bug C  — builder accepted ghost paths (nonexistent files left behind by
            Grok's session_search.sqlite); is_dir() is False for missing
            paths so they slipped through and every build paid a
            fingerprint OSError (permanent errors=4).
4. perf B1 — dimcode list_sessions ran a per-session COUNT over the 590 MB
            messages B-tree even in discovery mode (78% of cross sessions
            wall time).
5. perf B2 — save-summary wrote <id>.jsonl.summary.jsonl next to sessions;
            every "*.jsonl" scan re-listed the sidecar as a session
            (self-contamination loop). One predicate kills the class:
            ".jsonl." in name (same shape as the builder's existing filter).
6. perf C — universal_list_sessions had no cwd parameter, so the CLI's
            fn(cwd=...) call raised TypeError and degraded to an unscoped
            full-$HOME walk (25 s) whose results were then dropped by the
            caller's cwd filter (0 rows).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
from echolib import _adapters as adapters  # noqa: E402
from echolib import _adapters_zcode  # noqa: E402
from echolib import _claude_index  # noqa: E402
from index_builder import _builder  # noqa: E402


def _load_sd_recall():
    # sd-recall.py has a hyphen in its name — load by path.
    spec = importlib.util.spec_from_file_location(
        "sd_recall_under_test", SCRIPTS / "sd-recall.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAgentAutoAlias(unittest.TestCase):
    """bug A: "auto" must resolve to cross instead of dying in argparse."""

    def test_auto_resolves_to_cross(self):
        sd = _load_sd_recall()
        self.assertEqual(sd._resolve_cli_agent("auto"), "cross")

    def test_auto_is_an_advertised_choice(self):
        sd = _load_sd_recall()
        self.assertIn("auto", sd._cli_agent_choices())


class TestRecallLiteDecisionsPath(unittest.TestCase):
    """bug B (re-pinned for the lite-report merge): the --decisions path must
    import echolib and surface decision points. The old heredocs live in
    sd-recall.py now; the pin runs the real entry point instead of regexing
    the shell script."""

    @classmethod
    def setUpClass(cls):
        cls.script = (SCRIPTS / "recall-lite.sh").read_text(encoding="utf-8")

    def test_shell_delegates_evidence_to_single_lite_report_call(self):
        # One python invocation, no per-session heredocs left behind.
        self.assertIn('sd-recall.py" lite-report', self.script)
        self.assertNotIn("PYEOF", self.script)

    def _fixture_session(self, td):
        # Real Claude Code record shape. The old test's plain-string fixture
        # never reached the parser's text_content(), and its assertion
        # ``"决定" in stdout or "decision" in stdout.lower()`` was satisfied
        # vacuously by "(no decision points found)" — pinned green while the
        # decision path surfaced nothing.
        session = Path(td) / "s1.jsonl"
        session.write_text(
            json.dumps({"type": "user", "timestamp": "2026-09-19T10:00:00Z",
                        "message": {"role": "user",
                                    "content": [{"type": "text",
                                                 "text": "我们决定改用 SQLite 索引"}]}}) + "\n" +
            json.dumps({"type": "assistant", "timestamp": "2026-09-19T10:00:05Z",
                        "message": {"role": "assistant",
                                    "content": [{"type": "text",
                                                 "text": "好的，已经切换到 SQLite"}]}}) + "\n",
            encoding="utf-8")
        return session

    def test_lite_report_decisions_surfaces_decision_points(self):
        with tempfile.TemporaryDirectory() as td:
            session = self._fixture_session(td)
            row = f"s1\t2026-09-19\t2026-09-19 10:00\t2\tmain\tclaude\t{session}\n"
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "sd-recall.py"), "lite-report",
                 "--query", "索引", "--limit", "3", "--decisions", "--no-summary"],
                input=row, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("ModuleNotFoundError", r.stderr)
            self.assertIn("--- Decision points ---", r.stdout)
            # The decision line itself, not just the section header.
            self.assertIn("USER: 我们决定改用 SQLite 索引", r.stdout)
            self.assertNotIn("(no decision points found)", r.stdout)
            self.assertIn("0 cached, 1 parsed", r.stdout)

    def test_lite_report_cached_hit_skips_reparse(self):
        with tempfile.TemporaryDirectory() as td:
            session = self._fixture_session(td)
            echolib.save_analysis_result(str(session), "既定结论：用 SQLite",
                                         "索引", "claude", memory_tier="periodic")
            row = f"s1\t2026-09-19\t2026-09-19 10:00\t2\tmain\tclaude\t{session}\n"
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "sd-recall.py"), "lite-report",
                 "--query", "索引", "--limit", "3"],
                input=row, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("[CACHED]", r.stdout)
            self.assertIn("1 cached, 0 parsed", r.stdout)

    def test_lite_report_ignores_non_data_rows(self):
        # The sessions trailer ("--- N session(s) ---") can land in MATCHES
        # when fewer rows than --limit exist; it must not render a block.
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "sd-recall.py"), "lite-report",
             "--query", "x", "--limit", "3"],
            input="--- 0 session(s) ---\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 session(s) inspected", r.stdout)


class TestBuilderGhostFilter(unittest.TestCase):
    """bug C: nonexistent paths and dirs must never enter the build."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        real = Path(self.td.name) / "real.jsonl"
        real.write_text('{"type":"user","content":"hi"}\n', encoding="utf-8")
        ghost = str(Path(self.td.name) / "deleted-session")  # never created
        adir = Path(self.td.name) / "dir-session"
        adir.mkdir()
        self.saved_registry = dict(echolib.ADAPTER_REGISTRY)
        echolib.ADAPTER_REGISTRY["__ghosttest__"] = {
            "list_sessions": lambda limit=50, **kw: [
                {"session_id": "r", "full_path": str(real)},
                {"session_id": "g", "full_path": ghost},
                {"session_id": "d", "full_path": str(adir)},
            ],
            "display_name": "GhostTest",
        }

    def tearDown(self):
        echolib.ADAPTER_REGISTRY.clear()
        echolib.ADAPTER_REGISTRY.update(self.saved_registry)
        self.td.cleanup()

    def test_only_the_existing_file_survives(self):
        rows = _builder._scan_via_adapter("__ghosttest__", "__ghosttest__", limit=10)
        paths = [p for _sid, p, _a in rows]
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("real.jsonl"))


class TestDimcodeDiscoveryOnly(unittest.TestCase):
    """perf B1: discovery mode must not COUNT the messages B-tree."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        db = Path(self.td.name) / "dimcode.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE sessions (sessionId TEXT, title TEXT,"
                     " cwd TEXT, createdAt TEXT, status TEXT)")
        conn.execute("CREATE TABLE messages (sessionId TEXT, content TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('s1','t','/tmp','2026-09-01','ok')")
        for i in range(3):
            conn.execute("INSERT INTO messages VALUES ('s1', ?)", (f"m{i}",))
        conn.commit()
        conn.close()
        self.saved = _adapters_zcode.DIMCODE_DB_PATH
        _adapters_zcode.DIMCODE_DB_PATH = db

    def tearDown(self):
        _adapters_zcode.DIMCODE_DB_PATH = self.saved
        self.td.cleanup()

    def test_full_mode_counts(self):
        rows = _adapters_zcode.dimcode_list_sessions(limit=5)
        self.assertEqual(rows[0]["message_count"], 3)

    def test_discovery_mode_skips_count(self):
        with echolib.discovery_only():
            rows = _adapters_zcode.dimcode_list_sessions(limit=5)
        self.assertEqual(rows[0]["message_count"], 0)
        self.assertEqual(rows[0]["session_id"], "s1")  # list itself intact


class TestSummarySidecarExcluded(unittest.TestCase):
    """perf B2: <id>.jsonl.summary.jsonl is a cache file, never a session."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        d = Path(self.td.name)
        (d / "aaaa-1111.jsonl").write_text(
            json.dumps({"type": "user", "timestamp": "1", "content": "hello"}) + "\n" +
            json.dumps({"type": "assistant", "timestamp": "2", "content": "hi"}) + "\n",
            encoding="utf-8")
        (d / "aaaa-1111.jsonl.summary.jsonl").write_text(
            json.dumps({"type": "summary", "analyzed_at": "2026-09-19"}) + "\n",
            encoding="utf-8")
        self.d = d

    def tearDown(self):
        self.td.cleanup()

    def test_scan_project_dir_skips_sidecar(self):
        got = list(_claude_index._scan_project_dir(self.d))
        self.assertEqual([Path(p).name for _s, p, _m in got], ["aaaa-1111.jsonl"])

    def test_fallback_index_skips_sidecar(self):
        idx = _claude_index.build_fallback_index(str(self.d))
        self.assertIsNotNone(idx)
        entries = json.loads(Path(idx).read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertFalse(any(".jsonl." in e["full_path"] for e in entries))

    def test_fast_find_jsonl_skips_sidecar(self):
        found = _claude_index._fast_find_jsonl(self.d)
        self.assertEqual([e.name for e in found], ["aaaa-1111.jsonl"])

    def test_universal_list_skips_sidecar(self):
        env = Path(self.td.name) / "env"
        (env / "sessions").mkdir(parents=True)
        shutil.copy(self.d / "aaaa-1111.jsonl", env / "sessions")
        shutil.copy(self.d / "aaaa-1111.jsonl.summary.jsonl", env / "sessions")
        rows = adapters.universal_list_sessions(cwd=str(env), limit=10)
        paths = [r.full_path for r in rows]
        self.assertTrue(all(".jsonl.summary" not in p for p in paths), paths)
        self.assertTrue(any(p.endswith("aaaa-1111.jsonl") for p in paths), paths)


class TestUniversalHonorsCwd(unittest.TestCase):
    """perf C: a cwd-scoped universal call must scan only that tree."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        env = Path(self.td.name) / "unknownenv"
        (env / "sessions").mkdir(parents=True)
        (env / "sessions" / "conv-1.jsonl").write_text(
            json.dumps({"type": "user", "content": "x" * 80}) + "\n",
            encoding="utf-8")
        self.env = env

    def tearDown(self):
        self.td.cleanup()

    def test_cwd_scans_only_that_tree(self):
        rows = adapters.universal_list_sessions(cwd=str(self.env), limit=10)
        self.assertTrue(rows, "cwd-scoped scan returned nothing")
        for r in rows:
            self.assertTrue(str(r.full_path).startswith(str(self.env)),
                            f"escaped the cwd scope: {r.full_path}")

    def test_registry_signature_accepts_cwd(self):
        # The CLI calls fn(cwd=..., limit=..., keyword=...) for scope="current";
        # a TypeError here silently degrades to an unscoped $HOME walk.
        import inspect
        sig = inspect.signature(adapters.universal_list_sessions)
        self.assertIn("cwd", sig.parameters)


if __name__ == "__main__":
    unittest.main()
