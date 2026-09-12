#!/usr/bin/env python3
"""v0.7.0 回归：followups 状态机（hub 精华 lite）+ trends 环比/2σ 异常（session-digger 精华）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from analyze import compare_periods  # noqa: E402
from followups import (  # noqa: E402
    add_feedback,
    expire_stale,
    load_state,
    reactivations,
    save_state,
    scan_messages,
    sync_signals,
    today_actions,
    triage,
)


def _msg(text: str, sender: str, ts: int, chat: str = "测试群") -> dict:
    return {
        "id": f"m{ts}", "chat_id": "c1", "chat_name": chat, "chat_type": "group",
        "sender": sender, "nickname": sender, "ts": ts, "text": text,
        "msg_type": "text", "source": "fts",
    }


class TestScanHeuristics(unittest.TestCase):
    """信号提取：商机/承诺进状态机，待回复/等待对方现算。"""

    def test_deal_and_commit_signals(self):
        msgs = [
            _msg("最近有个投放合作，预算大概两万", "品牌方小李", 1000),
            _msg("好的，我明天给你整理资料", "我本人", 2000),
        ]
        out = scan_messages(msgs, self_hint="我本人")
        kinds = {s["kind"] for s in out["signals"]}
        self.assertEqual(kinds, {"deal", "commit"})
        commit = next(s for s in out["signals"] if s["kind"] == "commit")
        self.assertEqual(commit["side"], "self")

    def test_reply_needed_on_question(self):
        msgs = [_msg("这个方案报价多少？", "客户王总", 3000)]
        out = scan_messages(msgs, self_hint="我本人")
        self.assertIsNotNone(out["toReply"])
        self.assertIn("问句", out["toReply"]["note"])

    def test_waiting_when_self_asks_last(self):
        msgs = [_msg("预算批下来了吗？", "我本人", 4000)]
        out = scan_messages(msgs, self_hint="我本人")
        self.assertIsNone(out["toReply"])
        self.assertIsNotNone(out["waiting"])

    def test_ack_closes_reply_turns_waiting(self):
        # 我方交付后对方只回「好的」→ 待回复关闭，转等待对方（hub 任务状态语义）
        msgs = [
            _msg("初稿发你了", "我本人", 5000),
            _msg("好的", "客户王总", 5100),
        ]
        out = scan_messages(msgs, self_hint="我本人")
        self.assertIsNone(out["toReply"])
        self.assertIsNotNone(out["waiting"])
        self.assertIn("已应答", out["waiting"]["note"])


class TestStateMachine(unittest.TestCase):
    """candidate→强化→过期→复活 / feedback 学习 / triage 分流。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "followups.json"
        self.state = load_state(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _deal(self, ts=1000, text="投放合作预算两万，截止下周"):
        return {"kind": "deal", "text": text, "sender": "品牌方", "ts": ts, "hints": ["预算", "截止"]}

    def test_create_reinforce_expire_revive(self):
        st = sync_signals(self.state, "测试群", "c1", [self._deal()])
        self.assertEqual(st["created"], 1)
        item = self.state["items"][0]
        self.assertEqual(item["status"], "new")
        self.assertEqual(item["confidence"], "high")  # 2 个 hint

        st2 = sync_signals(self.state, "测试群", "c1", [self._deal(ts=2000)])
        self.assertEqual(st2["updated"], 1)
        self.assertEqual(item["reinforcement"], 2)

        item["expires"] = "2000-01-01 00:00:00"
        self.assertEqual(expire_stale(self.state, at="2000-02-01 00:00:00"), 1)
        self.assertEqual(item["status"], "stale")

        st3 = sync_signals(self.state, "测试群", "c1", [self._deal(ts=3000)])
        self.assertEqual(st3["updated"], 1)
        self.assertEqual(item["status"], "new", "过期候选有新信号应复活")

    def test_feedback_ignore_suppresses(self):
        sync_signals(self.state, "测试群", "c1", [self._deal()])
        add_feedback(self.state, "chat:c1", "ignore")
        st = sync_signals(self.state, "测试群", "c1", [self._deal(ts=9000)])
        self.assertEqual(st["skipped"], 1)
        self.assertEqual(len(self.state["items"]), 1)

    def test_feedback_confirmed_upgrades(self):
        sig = {"kind": "deal", "text": "合作", "sender": "x", "ts": 1000, "hints": ["合作"]}
        sync_signals(self.state, "测试群", "c1", [sig])
        item = self.state["items"][0]
        self.assertEqual(item["priority"], 2)
        add_feedback(self.state, "chat:c1", "confirmed")
        sync_signals(self.state, "测试群", "c1", [{**sig, "ts": 2000}])
        self.assertEqual(item["priority"], 5)
        self.assertEqual(item["confidence"], "confirmed")

    def test_triage_flow(self):
        sig = {"kind": "commit", "text": "明天发你报价", "sender": "我", "ts": 1000, "hints": ["明天发"]}
        sync_signals(self.state, "测试群", "c1", [sig])
        item = self.state["items"][0]
        triage(self.state, item["id"], "pursue", note="跟进")
        self.assertEqual(item["status"], "open")
        triage(self.state, item["id"], "won")
        self.assertEqual(item["status"], "won")
        # 关闭后同 key 新信号 → 新条目（不复活旧记录）
        st = sync_signals(self.state, "测试群", "c1", [{**sig, "ts": 9000}])
        self.assertEqual(st["created"], 1)
        self.assertEqual(len(self.state["items"]), 2)

    def test_today_actions_ordering(self):
        sig = {"kind": "deal", "text": "合作A", "sender": "x", "ts": 1000, "hints": ["合作"]}
        sync_signals(self.state, "A群", "ca", [sig])
        sig2 = {"kind": "deal", "text": "合作B 预算 报价", "sender": "y", "ts": 2000, "hints": ["预算", "报价", "合作"]}
        sync_signals(self.state, "B群", "cb", [sig2])
        actions = today_actions(self.state)
        self.assertEqual(actions[0]["chat"], "B群", "高优先级在前")

    def test_state_persistence_roundtrip(self):
        sync_signals(self.state, "测试群", "c1", [self._deal()])
        save_state(self.path, self.state)
        loaded = load_state(self.path)
        self.assertEqual(len(loaded["items"]), 1)
        self.assertEqual(loaded["items"][0]["key"], self.state["items"][0]["key"])

    def test_corrupt_state_resets(self):
        self.path.write_text("not-json{", encoding="utf-8")
        loaded = load_state(self.path)
        self.assertEqual(loaded["items"], [])


class TestReactivations(unittest.TestCase):
    def test_idle_private_chat_flagged(self):
        import time

        now = int(time.time())
        state = {"version": 1, "items": [], "feedback": [], "nextId": 1}
        old = now - 20 * 86400
        by_chat = {
            "wxid_friend": [
                _msg("上次说的资料发我一份", "朋友甲", old, chat="朋友甲"),
                _msg("好的我整理下", "我本人", old + 100, chat="朋友甲"),
            ]
        }
        out = reactivations(state, by_chat, self_hint="我本人", idle_days=14)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["chat"], "朋友甲")
        self.assertGreaterEqual(out[0]["idleDays"], 14)

    def test_recent_chat_not_flagged(self):
        import time

        now = int(time.time())
        state = {"version": 1, "items": [], "feedback": [], "nextId": 1}
        by_chat = {"wxid_friend": [_msg("在吗", "朋友甲", now - 3600, chat="朋友甲")]}
        self.assertEqual(reactivations(state, by_chat, "我本人", idle_days=14), [])


class TestComparePeriods(unittest.TestCase):
    """环比 + 2σ 异常（session-digger 精华）。"""

    def test_basic_delta(self):
        series = [
            {"month": "2026-01", "messages": 100, "activeUsers": 10, "sentimentNet": 5},
            {"month": "2026-02", "messages": 150, "activeUsers": 12, "sentimentNet": -3},
        ]
        out = compare_periods(series)
        self.assertEqual(out["compare"], ["2026-01", "2026-02"])
        self.assertEqual(out["messages"]["pct"], 50.0)
        self.assertEqual(out["sentimentNet"]["delta"], -8)

    def test_too_short(self):
        self.assertIn("note", compare_periods([{"month": "2026-01", "messages": 1}]))

    def test_anomaly_detection(self):
        # 平稳 6 个月后一个 10 倍尖峰 → 必须被 2σ 门抓到
        series = [{"month": f"2025-{m:02d}", "messages": 100, "activeUsers": 10, "sentimentNet": 0} for m in range(1, 8)]
        series.append({"month": "2025-08", "messages": 1000, "activeUsers": 10, "sentimentNet": 0})
        out = compare_periods(series)
        self.assertTrue(out["anomalies"])
        self.assertEqual(out["anomalies"][-1]["month"], "2025-08")
        self.assertGreater(out["anomalies"][-1]["sigma"], 2)

    def test_no_anomaly_in_stable_series(self):
        series = [{"month": f"2025-{m:02d}", "messages": 100 + m, "activeUsers": 10, "sentimentNet": 0} for m in range(1, 10)]
        self.assertEqual(compare_periods(series)["anomalies"], [])


if __name__ == "__main__":
    unittest.main()
