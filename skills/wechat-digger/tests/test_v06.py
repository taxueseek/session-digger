#!/usr/bin/env python3
"""v0.6.0 回归：时区口径统一、snapshot 防串聊、fts 引擎纯函数行为。"""

from __future__ import annotations

import argparse
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from normalize import _to_ts  # noqa: E402


class TestToTsTimezone(unittest.TestCase):
    """naive 字符串按本地时区（曾误当 UTC，与 vault epoch 源混用差 8 小时）。"""

    def test_naive_local_string(self):
        s = "2026-09-06 12:00:00"
        expect = int(datetime(2026, 9, 6, 12, 0, 0).timestamp())
        self.assertEqual(_to_ts(s), expect)

    def test_date_only_local(self):
        expect = int(datetime(2026, 9, 6, 0, 0, 0).timestamp())
        self.assertEqual(_to_ts("2026-09-06"), expect)

    def test_utc_marked_string(self):
        # 带 Z 的显式 UTC 标注不受本修复影响
        self.assertEqual(_to_ts("2026-09-06T12:00:00Z"), _to_ts("2026-09-06T20:00:00+08:00"))

    def test_epoch_passthrough(self):
        self.assertEqual(_to_ts(1700000000), 1700000000)
        self.assertEqual(_to_ts(1720605600000), 1720605600)

    def test_garbage_returns_none(self):
        self.assertIsNone(_to_ts("not-a-date"))
        self.assertIsNone(_to_ts(""))
        self.assertIsNone(_to_ts(None))


class TestSnapshotGuard(unittest.TestCase):
    """--snapshot 必须 --chat：防不同会话快照串写 unknown.json。"""

    def test_snapshot_requires_chat(self):
        from wd import cmd_analyze

        args = argparse.Namespace(
            input=None, chat=None, snapshot=True, mode="summary",
            since=None, until=None, all_time=False, limit=100,
            output="-", save_history=False, self_hint=None, source=None, allow_fixture=True,
        )
        self.assertEqual(cmd_analyze(args), 1)


class TestEmptyFallThrough(unittest.TestCase):
    """空结果≠有覆盖：vault 空窗段（2026-02 前）查询应穿透到 fts 全史层。"""

    def test_history_empty_falls_to_next_engine(self):
        import source_registry as sr
        from source_registry import SOURCE_REGISTRY, SourceRouter

        saved = {k: dict(v) for k, v in SOURCE_REGISTRY.items()}
        SOURCE_REGISTRY["vault"]["history"] = lambda ctx, *a, **k: []
        SOURCE_REGISTRY["wxcli"]["history"] = lambda ctx, *a, **k: []
        SOURCE_REGISTRY["fts"]["history"] = lambda ctx, *a, **k: [{"id": "x", "source": "fts"}]
        r = SourceRouter()
        r.detect_all = lambda: [
            sr.SourceStatus("vault", True, 100),
            sr.SourceStatus("fts", True, 150),
        ]
        try:
            out = r.history("X")
        finally:
            SOURCE_REGISTRY.clear()
            SOURCE_REGISTRY.update(saved)
        self.assertEqual(out.get("source"), "fts", "vault 空结果应穿透到 fts")

    def test_all_engines_empty_is_valid_empty(self):
        import source_registry as sr
        from source_registry import SOURCE_REGISTRY, SourceRouter

        saved = {k: dict(v) for k, v in SOURCE_REGISTRY.items()}
        for name in ("vault", "wxcli", "fts"):
            SOURCE_REGISTRY[name]["history"] = lambda ctx, *a, **k: []
        r = SourceRouter()
        r.detect_all = lambda: [sr.SourceStatus("vault", True, 100)]
        try:
            out = r.history("X")
        finally:
            SOURCE_REGISTRY.clear()
            SOURCE_REGISTRY.update(saved)
        self.assertEqual(out, {"source": "vault", "data": []}, "全部为空时返回合法空结果而非报错")


class TestFtsPureFunctions(unittest.TestCase):
    def test_empty_keyword_is_empty(self):
        from fts_engine import fts_search

        self.assertEqual(fts_search("", limit=3), [])
        out = fts_search("", limit=3, with_meta=True)
        self.assertEqual(out, {"hits": [], "total": 0, "has_more": False, "limit": 3})

    def test_as_epoch(self):
        from fts_engine import _as_epoch

        self.assertEqual(_as_epoch("2026-09-06"), int(datetime(2026, 9, 6).timestamp()))
        self.assertEqual(_as_epoch(123), 123)
        self.assertIsNone(_as_epoch(None))
        self.assertIsNone(_as_epoch("garbage"))


if __name__ == "__main__":
    unittest.main()
