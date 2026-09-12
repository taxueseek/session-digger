#!/usr/bin/env python3
"""Core unit + integration tests for wechat-digger (offline-safe)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "sample_messages.json"
sys.path.insert(0, str(SCRIPTS))

from analyze import (  # noqa: E402
    ANALYSIS_MODES,
    activity_heatmap,
    build_graph,
    build_profiles,
    extract_decisions,
    extract_keywords,
    reciprocity_stats,
    run_pipeline,
    segment_messages,
    speaker_ranking,
)
from normalize import normalize_message, normalize_messages  # noqa: E402
from paths import acquire_dir, acquire_tool, digger_root, find_skill_root, vault_cli_path  # noqa: E402
from source_registry import SOURCE_REGISTRY, detect_summary  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_aliases_and_types(self):
        m = normalize_message(
            {
                "msgId": "1",
                "fromUser": "u1",
                "fromName": "小明",
                "content": "hello",
                "createTime": 1720605600,
                "type": 1,
            },
            source="vault",
        )
        self.assertEqual(m["id"], "1")
        self.assertEqual(m["sender"], "u1")
        self.assertEqual(m["nickname"], "小明")
        self.assertEqual(m["text"], "hello")
        self.assertEqual(m["msg_type"], "text")
        self.assertEqual(m["ts"], 1720605600)

    def test_ms_timestamp(self):
        m = normalize_message({"id": "x", "text": "t", "timestamp": 1720605600000})
        self.assertEqual(m["ts"], 1720605600)

    def test_envelope(self):
        raw = {"messages": [{"id": "a", "text": "1", "sender": "s", "createTime": 1}]}
        out = normalize_messages(raw, source="export")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "export")


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.msgs = normalize_messages(json.loads(FIXTURE.read_text(encoding="utf-8")), source="fixture")

    def test_pipeline_full(self):
        r = run_pipeline(self.msgs, mode="full")
        self.assertGreater(r["stats"]["totalMessages"], 0)
        self.assertTrue(r.get("topics") is not None)
        self.assertTrue(r.get("profiles"))
        self.assertTrue(r.get("sentiment"))
        self.assertTrue(r.get("graph"))
        self.assertTrue(r.get("ranking"))
        self.assertTrue(r.get("keywords"))
        self.assertTrue(r.get("activity"))
        self.assertTrue(r.get("reciprocity"))
        # decision keywords present in fixture
        self.assertTrue(len(r.get("decisions") or []) >= 1)

    def test_pipeline_lab_and_dyad(self):
        lab = run_pipeline(self.msgs, mode="lab")
        self.assertIn("keywords", lab)
        self.assertIn("activity", lab)
        self.assertNotIn("graph", lab)  # lab 不含 graph 块
        dyad = run_pipeline(self.msgs, mode="dyad")
        self.assertIn("reciprocity", dyad)
        self.assertIn("pair", dyad["reciprocity"])

    def test_ranking_keywords_activity(self):
        rank = speaker_ranking(self.msgs)
        self.assertGreaterEqual(len(rank), 2)
        self.assertAlmostEqual(sum(r["share"] for r in rank), 1.0, places=2)
        kws = extract_keywords(self.msgs, top_n=10)
        self.assertTrue(kws)
        act = activity_heatmap(self.msgs)
        self.assertEqual(len(act["byHour"]), 24)
        rec = reciprocity_stats(self.msgs)
        self.assertEqual(len(rec["pair"]), 2)

    def test_analysis_modes_table(self):
        self.assertIn("lab", ANALYSIS_MODES)
        self.assertIn("dyad", ANALYSIS_MODES)
        self.assertIn("full", ANALYSIS_MODES)

    def test_segments_and_profiles(self):
        segs = segment_messages(self.msgs)
        self.assertGreaterEqual(len(segs), 1)
        profiles = build_profiles(self.msgs)
        names = {p["nickname"] for p in profiles}
        self.assertIn("Alice", names)

    def test_graph_has_nodes(self):
        g = build_graph(self.msgs, min_edge_weight=1)
        self.assertGreaterEqual(len(g["nodes"]), 2)


class TestRegistry(unittest.TestCase):
    def test_registry_keys(self):
        for name in ("vault", "wxcli", "export", "fixture"):
            self.assertIn(name, SOURCE_REGISTRY)
            self.assertIn("detect", SOURCE_REGISTRY[name])
            self.assertIn("priority", SOURCE_REGISTRY[name])

    def test_fixture_detect(self):
        os.environ["WECHAT_DIGGER_FIXTURE"] = str(FIXTURE)
        try:
            s = detect_summary(allow_fixture=True)
            self.assertIn("fixture", s["available"])
        finally:
            os.environ.pop("WECHAT_DIGGER_FIXTURE", None)

    def test_unknown_preferred_source(self):
        s = detect_summary(preferred="no_such_source", allow_fixture=True)
        # preferred missing → primary may still be something else only if not forcing empty
        # router.primary with preferred missing returns None
        from source_registry import SourceRouter

        r = SourceRouter(preferred="no_such_source", allow_fixture=True)
        self.assertIsNone(r.primary())


class TestWxBridge(unittest.TestCase):
    def test_unwrap_payload(self):
        from wx_bridge import unwrap_payload

        self.assertEqual(unwrap_payload({"results": [1, 2]}), [1, 2])
        self.assertEqual(unwrap_payload({"messages": [{"a": 1}]}), [{"a": 1}])
        self.assertEqual(unwrap_payload([{"x": 1}]), [{"x": 1}])
        err = {"error": "daemon down"}
        self.assertEqual(unwrap_payload(err), err)

    def test_capability_matrix_has_rich_ops(self):
        from wx_bridge import CAPABILITY_MATRIX, WX_SURFACE

        for op in ("biz-articles", "sns-feed", "attachments", "history"):
            self.assertIn(op, CAPABILITY_MATRIX)
        self.assertIn("wxcli", CAPABILITY_MATRIX["biz-articles"])
        self.assertNotIn("--format", str(WX_SURFACE))  # surface is argv only
        self.assertIn("sns-feed", WX_SURFACE)

    def test_wx_bin_or_none(self):
        from wx_bridge import wx_bin

        b = wx_bin()
        self.assertTrue(b is None or os.path.isfile(b))


class TestPaths(unittest.TestCase):
    def test_digger_root(self):
        self.assertTrue((digger_root() / "scripts" / "wd.py").is_file())

    def test_find_skill_optional(self):
        find_skill_root("wechat-local-vault", "scripts/vault_cli.py")

    def test_acquire_bundled_self_contained(self):
        ad = acquire_dir()
        self.assertIsNotNone(ad)
        self.assertEqual(ad.name, "acquire")
        # public build ships read-only helpers only
        for tool in ("vault_cli", "export_chat"):
            p = acquire_tool(tool)
            self.assertIsNotNone(p, tool)
            self.assertTrue(p.is_file(), tool)
        # key/decrypt stack intentionally not distributed
        for tool in ("decrypt", "keys"):
            self.assertIsNone(acquire_tool(tool), tool)
        self.assertTrue(vault_cli_path().is_file())

    def test_acquire_dir_unknown_env_fallback(self):
        # invalid override must fall back to bundled, not crash
        os.environ["WECHAT_ACQUIRE_DIR"] = "/tmp/wechat-digger-no-such-acquire"
        try:
            ad = acquire_dir()
            self.assertIsNotNone(ad)
            self.assertTrue((ad / "vault_cli.py").is_file())
        finally:
            os.environ.pop("WECHAT_ACQUIRE_DIR", None)


class TestCLI(unittest.TestCase):
    def _run(self, *args: str, env=None):
        cmd = [sys.executable, str(SCRIPTS / "wd.py"), *args]
        e = os.environ.copy()
        if env:
            e.update(env)
        return subprocess.run(cmd, capture_output=True, text=True, env=e, timeout=60)

    def test_doctor(self):
        r = self._run("doctor")
        self.assertIn(r.returncode, (0, 2))
        data = json.loads(r.stdout)
        self.assertIn("checks", data)
        ids = {c["id"]: c for c in data["checks"]}
        self.assertTrue(ids["acquire_bundled"]["ok"])
        self.assertTrue(ids["self_contained"]["ok"])

    def test_acquire_info(self):
        r = self._run("acquire-info")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertTrue(data.get("bundled"))
        self.assertTrue(data["tools"]["vault_cli"]["ready"])
        # public build: key/decrypt stack intentionally absent
        self.assertFalse(data["tools"]["decrypt"]["ready"])
        self.assertFalse(data["tools"]["keys"]["ready"])

    def test_vault_status_bundled(self):
        r = self._run("vault", "status", "--format", "json")
        # may be text-only status; code 0 if vault works
        self.assertIn(r.returncode, (0, 1), r.stderr + r.stdout[:200])

    def test_wx_info(self):
        r = self._run("wx", "info")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertIn("surface", data)
        self.assertIn("biz-articles", data["surface"])

    def test_detect_includes_engines(self):
        r = self._run("detect")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertIn("engines_by_op", data)
        self.assertIn("wx", data)
        # vault ready → history should list vault
        if "vault" in (data.get("available") or []):
            self.assertIn("vault", data["engines_by_op"].get("history") or [])

    def test_analyze_fixture(self):
        r = self._run(
            "--allow-fixture",
            "analyze",
            "--chat",
            "演示项目群",
            "--input",
            str(FIXTURE),
            "--mode",
            "full",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["stats"]["totalMessages"], 8)

    def test_digest_and_index(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            out = Path(td) / "digests"
            r1 = self._run("index", "--chat", "演示项目群", "--input", str(FIXTURE), "--db", str(db))
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = self._run(
                "digest",
                "--chat",
                "演示项目群",
                "--input",
                str(FIXTURE),
                "--output-dir",
                str(out),
                "--version",
                "normal",
            )
            self.assertEqual(r2.returncode, 0, r2.stderr)
            data = json.loads(r2.stdout)
            self.assertTrue(Path(data["written"]).exists())
            r3 = self._run("search", "部署", "--use-index", "--limit", "5")
            # index default path may be empty; search should still return structure
            self.assertIn(r3.returncode, (0, 1))

    def test_report_lab_and_dyad(self):
        with tempfile.TemporaryDirectory() as td:
            for flavor in ("lab", "dyad"):
                r = self._run(
                    "report",
                    "--chat",
                    "演示项目群",
                    "--input",
                    str(FIXTURE),
                    "--flavor",
                    flavor,
                    "--output-dir",
                    td,
                    "--print-body",
                )
                self.assertEqual(r.returncode, 0, r.stderr + r.stdout[:300])
                self.assertIn("发言排行", r.stdout)
                if flavor == "lab":
                    self.assertIn("数据实验室", r.stdout)
                else:
                    self.assertIn("关系", r.stdout)

    def test_history_unknown_chat_no_crash(self):
        r = self._run("--source", "fixture", "--allow-fixture", "history", "--chat", "不存在的群xyz", "--since", "2020-01-01")
        # fixture still returns sample messages (chat_hint only); or empty — must not traceback
        self.assertNotIn("Traceback", r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main()
