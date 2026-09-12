#!/usr/bin/env python3
"""v0.5.x 新增能力与审查修复的单元测试（离线安全）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from analyze import (  # noqa: E402
    _is_self,
    extract_signals,
    score_text,
    segment_messages,
    trend_series,
)
from fts_engine import _as_epoch, _escape_like, coverage_data  # noqa: E402
from wx_bridge import parse_json_output  # noqa: E402


def _msg(text: str, sender: str = "张三", nickname: str = "", ts: int = 1700000000) -> dict:
    return {"id": f"m{ts}{sender}", "sender": sender, "nickname": nickname or sender, "ts": ts, "text": text, "msg_type": "text"}


class TestWxBridge(unittest.TestCase):
    def test_parse_json_strips_warning_prefix(self):
        raw = '[wx] 警告：磁盘上发现 daemon 不认识的分片\n{\n  "meta": {"a": 1},\n  "data": []\n}'
        out = parse_json_output(raw)
        self.assertIsInstance(out, dict)
        self.assertIn("meta", out)

    def test_parse_json_plain(self):
        self.assertEqual(parse_json_output('{"ok": 1}'), {"ok": 1})


class TestEscape(unittest.TestCase):
    def test_escape_like(self):
        self.assertEqual(_escape_like("100%折扣_全"), "100\\%折扣\\_全")
        self.assertEqual(_escape_like("a\\b"), "a\\\\b")


class TestEpoch(unittest.TestCase):
    def test_date_string(self):
        self.assertEqual(_as_epoch("2025-01-01"), 1735660800)

    def test_int_passthrough(self):
        self.assertEqual(_as_epoch(123), 123)

    def test_none(self):
        self.assertIsNone(_as_epoch(None))


class TestSelfDetection(unittest.TestCase):
    def test_chat_name_must_not_mark_self(self):
        # 回归：self_hint 撞聊天名时不能把全员判成本人
        m = _msg("在吗？", sender="路人", nickname="路人")
        self.assertFalse(_is_self(m, "聪明投资者"))

    def test_sender_match(self):
        self.assertTrue(_is_self(_msg("hi", sender="成启"), "成启"))
        self.assertFalse(_is_self(_msg("hi", sender="路人"), "成启"))


class TestSignals(unittest.TestCase):
    def setUp(self):
        self.msgs = [
            _msg("最近有个投放合作，预算大概两万，截止下周", sender="品牌方小李", ts=1700000100),
            _msg("好的，我明天给你整理资料", sender="我本人", nickname="我本人", ts=1700000200),
            _msg("这个方案报价多少？能便宜点吗？", sender="客户王总", ts=1700000300),
        ]
        self.segs = segment_messages(self.msgs)

    def test_deals_detected(self):
        out = extract_signals(self.msgs, self.segs, self_hint="我本人")
        self.assertGreaterEqual(len(out["deals"]), 1)
        self.assertTrue(any("预算" in d["hints"] or "投放" in d["hints"] for d in out["deals"]))

    def test_commitment_side(self):
        out = extract_signals(self.msgs, self.segs, self_hint="我本人")
        sides = {c["side"] for c in out["commitments"]}
        self.assertIn("self", sides)

    def test_reply_needed(self):
        out = extract_signals(self.msgs, self.segs, self_hint="我本人")
        self.assertIsNotNone(out["replyNeeded"])
        self.assertEqual(out["replyNeeded"]["sender"], "客户王总")

    def test_no_reply_when_self_last(self):
        msgs = self.msgs + [_msg("好的没问题", sender="我本人", nickname="我本人", ts=1700000400)]
        out = extract_signals(msgs, segment_messages(msgs), self_hint="我本人")
        self.assertIsNone(out["replyNeeded"])


class TestTrends(unittest.TestCase):
    def test_monthly_aggregation(self):
        msgs = [
            _msg("很好很棒", ts=1700000000),  # 2023-11
            _msg("太差了", ts=1702600000),    # 2023-12
            _msg("还行", ts=1704067200),      # 2024-01
        ]
        out = trend_series(msgs)
        series = out["series"]
        self.assertEqual(out["months"], 3)
        self.assertEqual([s["month"] for s in series], ["2023-11", "2023-12", "2024-01"])
        self.assertEqual(series[0]["sentimentNet"] > 0, True)
        self.assertEqual(series[1]["sentimentNet"] < 0, True)

    def test_empty(self):
        out = trend_series([])
        self.assertEqual(out["series"], [])
        self.assertEqual(out["months"], 0)
        self.assertEqual(out["periods"]["months"], 0, "空序列无环比")


class TestScoreText(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(score_text("非常棒")[0], "positive")
        self.assertEqual(score_text("太差了")[0], "negative")
        self.assertEqual(score_text("今天天气多云")[0], "neutral")


class TestCoverageData(unittest.TestCase):
    def test_shape(self):
        data = coverage_data()
        # 解密副本存在时应有 vault 与 fts 两层
        if "vault" in data and "messages" in data["vault"]:
            self.assertIn("span", data["vault"])
            self.assertIn("monthly_holes", data["vault"])
        if "fts" in data and "messages" in data["fts"]:
            self.assertIn("span", data["fts"])


if __name__ == "__main__":
    unittest.main()
