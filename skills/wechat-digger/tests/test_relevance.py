#!/usr/bin/env python3
"""Tests for the relevance profile (P1: user-aligned opportunity filtering).

Design contract under test, in order of importance:

1. **An empty/absent profile is a no-op.** The module was added to an
   existing pipeline, so the single most important property is that
   behaviour is byte-identical when nobody has configured anything.
2. **A malformed profile degrades to no policy**, never to an exception —
   scanning must not break because a config file is corrupt.
3. **Manual confirmation outranks policy.** A signal a human explicitly
   confirmed is never dropped by keyword filtering.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from followups import sync_signals  # noqa: E402
from relevance import (  # noqa: E402
    PRIORITY_MAX,
    PRIORITY_MIN,
    RelevanceProfile,
)


def _signal(text: str, hints: int = 2) -> dict:
    return {
        "kind": "deal",
        "text": text,
        "sender": "someone",
        "ts": 1700000000,
        "hints": ["h"] * hints,
    }


def _state() -> dict:
    return {"items": [], "feedback": [], "nextId": 1}


class TestEmptyProfileIsNoOp(unittest.TestCase):
    """The safety property: introducing this module changes nothing by default."""

    def test_empty_profile_reports_empty(self):
        self.assertTrue(RelevanceProfile().is_empty)
        self.assertTrue(RelevanceProfile(include=(), exclude=(), chat_trust={}).is_empty)

    def test_adjust_returns_input_unchanged(self):
        p = RelevanceProfile()
        for base in (0, 1, 3, 5):
            with self.subTest(base=base):
                self.assertEqual(p.adjust(base, "任意文本", "any"), (base, []))

    def test_nothing_is_excluded(self):
        p = RelevanceProfile()
        self.assertFalse(p.is_excluded_text("信用卡 话费 优惠券"))

    def test_sync_signals_identical_without_profile(self):
        """Same input, with and without an empty profile, must yield the same
        items — this is the compatibility guard."""
        signals = [_signal("京东优惠券 拍1 链接"), _signal("合作项目 brief 预算")]

        s1 = _state()
        r1 = sync_signals(s1, "群", "c1", signals)

        s2 = _state()
        r2 = sync_signals(s2, "群", "c1", signals, profile=RelevanceProfile())

        self.assertEqual(r1["created"], r2["created"])
        self.assertEqual(len(s1["items"]), len(s2["items"]))
        self.assertEqual(
            [i["title"] for i in s1["items"]], [i["title"] for i in s2["items"]]
        )


class TestMatching(unittest.TestCase):
    def test_exclude_match_is_case_insensitive(self):
        p = RelevanceProfile(exclude=["信用卡", "Whitelist"])
        self.assertEqual(p.match_excluded("办信用卡吗"), "信用卡")
        self.assertEqual(p.match_excluded("whitelist invite"), "Whitelist")
        self.assertIsNone(p.match_excluded("无关内容"))

    def test_include_match(self):
        p = RelevanceProfile(include=["AI", "投资"])
        self.assertEqual(p.match_included("聊聊 AI 项目"), "AI")
        self.assertIsNone(p.match_included("今天天气"))

    def test_empty_terms_ignored_in_construction(self):
        p = RelevanceProfile.from_dict({"include": ["AI", "", "  ", None, 42]})
        self.assertEqual(p.include, ("AI",))


class TestAdjust(unittest.TestCase):
    def test_exclude_pins_to_minimum(self):
        p = RelevanceProfile(exclude=["信用卡"])
        value, notes = p.adjust(5, "浦发信用卡开卡", "c1")
        self.assertEqual(value, PRIORITY_MIN)
        self.assertTrue(any(n.startswith("excluded:") for n in notes))

    def test_include_boosts(self):
        p = RelevanceProfile(include=["合作"], boost=2)
        value, notes = p.adjust(2, "有个合作想聊", "c1")
        self.assertEqual(value, 4)
        self.assertIn("topic:合作", notes)

    def test_boost_clamps_to_max(self):
        p = RelevanceProfile(include=["合作"], boost=3)
        value, _ = p.adjust(4, "合作 brief", "c1")
        self.assertEqual(value, PRIORITY_MAX)

    def test_chat_trust_penalty(self):
        p = RelevanceProfile(chat_trust={"noisy@chatroom": -3})
        value, notes = p.adjust(5, "普通内容", "noisy@chatroom")
        self.assertEqual(value, 2)
        self.assertIn("trust:-3", notes)

    def test_trust_clamps_at_minimum(self):
        p = RelevanceProfile(chat_trust={"x": -99})
        value, _ = p.adjust(2, "any", "x")
        self.assertEqual(value, PRIORITY_MIN)

    def test_untrusted_chat_unaffected(self):
        p = RelevanceProfile(chat_trust={"other": -3})
        self.assertEqual(p.adjust(4, "any", "mine"), (4, []))


class TestLoading(unittest.TestCase):
    def test_missing_file_yields_empty_profile(self):
        p = RelevanceProfile.load(Path("/nonexistent/relevance.json"))
        self.assertTrue(p.is_empty)

    def test_valid_file_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rel.json"
            path.write_text(json.dumps({
                "include": ["AI"],
                "exclude": ["话费"],
                "chat_trust": {"c1": -2},
                "boost": 1,
            }), encoding="utf-8")
            p = RelevanceProfile.load(path)
            self.assertEqual(p.include, ("AI",))
            self.assertEqual(p.exclude, ("话费",))
            self.assertEqual(p.chat_trust, {"c1": -2})
            self.assertEqual(p.boost, 1)
            self.assertEqual(p.source_path, path)

    def test_corrupt_json_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            p = RelevanceProfile.load(path)
            self.assertTrue(p.is_empty, "损坏配置必须降级为空策略而非抛错")

    def test_wrong_shape_degrades_to_empty(self):
        for payload in ("[]", "42", "null", '"str"'):
            with self.subTest(payload=payload):
                p = RelevanceProfile.from_dict(json.loads(payload))
                self.assertTrue(p.is_empty)

    def test_bad_field_types_are_tolerated(self):
        p = RelevanceProfile.from_dict({
            "include": "AI",
            "exclude": 123,
            "chat_trust": ["not", "a", "dict"],
            "boost": "big",
        })
        self.assertEqual(p.include, ())
        self.assertEqual(p.exclude, ())
        self.assertEqual(p.chat_trust, {})
        self.assertEqual(p.boost, 2)

    def test_describe_is_json_serializable(self):
        p = RelevanceProfile(include=["AI"], exclude=["话费"])
        json.dumps(p.describe(), ensure_ascii=False)


class TestSyncSignalsIntegration(unittest.TestCase):
    def test_excluded_topic_signals_are_filtered(self):
        profile = RelevanceProfile(exclude=["信用卡", "话费"])
        state = _state()
        signals = [
            _signal("浦发信用卡开卡活动"),
            _signal("充值话费立减"),
            _signal("有个合作想谈"),
        ]
        stats = sync_signals(state, "群", "c1", signals, profile=profile)
        self.assertEqual(stats["filtered"], 2)
        self.assertEqual(stats["created"], 1)
        self.assertEqual(len(state["items"]), 1)
        self.assertIn("合作", state["items"][0]["title"])

    def test_confirmed_verdict_bypasses_filter(self):
        """A human-confirmed signal must survive keyword filtering."""
        from followups import _signal_key

        profile = RelevanceProfile(exclude=["信用卡"])
        text = "浦发信用卡开卡活动"
        state = _state()
        state["feedback"].append({
            # Real key shape: f"{chat_id}|{kind}|{md5(text)[:8]}"
            "target": "key:" + _signal_key("c1", "deal", text),
            "verdict": "confirmed",
        })
        stats = sync_signals(state, "群", "c1", [_signal(text)], profile=profile)
        self.assertEqual(stats["filtered"], 0)
        self.assertEqual(len(state["items"]), 1)

    def test_stats_shape_is_backward_compatible(self):
        """Callers iterate over the stats keys to accumulate; the original
        three keys must remain present."""
        stats = sync_signals(_state(), "群", "c1", [_signal("内容")])
        for key in ("created", "updated", "skipped"):
            self.assertIn(key, stats)

    def test_include_boost_raises_priority(self):
        profile = RelevanceProfile(include=["合作"], boost=2)
        state = _state()
        sync_signals(state, "群", "c1", [_signal("合作项目 brief", hints=1)], profile=profile)
        # base = min(5, 2 + max(0, 1-1)) = 2, boosted by 2 → 4
        self.assertEqual(state["items"][0]["priority"], 4)

    def test_no_profile_argument_still_works(self):
        """Backward compatibility: existing callers pass no profile."""
        state = _state()
        stats = sync_signals(state, "群", "c1", [_signal("普通内容")])
        self.assertEqual(stats["created"], 1)


if __name__ == "__main__":
    unittest.main()
