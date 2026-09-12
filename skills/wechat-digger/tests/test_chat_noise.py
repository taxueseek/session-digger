#!/usr/bin/env python3
"""Tests for chat noise classification, naming, and read-only connection.

Three defects are locked down here, all found by measuring against the real
vault rather than by reading the code:

1. **Group-name signal never fired.** ``_NameBook`` opened ``contact.db`` with
   ``mode=ro``, which fails on a WAL-mode database with no ``-shm`` sidecar.
   Every chat name silently degraded to its raw ``@chatroom`` id, so the
   strongest single noise indicator (name keywords, 25-40 points) scored zero
   — a group named "示例线报群" got 0 for its name.

2. **Question density inverted the signal.** The "真实对话" penalty applied
   unconditionally, but 线报 groups generate question marks at least as
   densely as real discussion ("还有吗？" "怎么领？"). A 11992-message 京东优惠
   group with 97.8% links and 100% top-share scored ``normal``.

3. **Link density was binary.** 41% links and 98% links scored the same.

The thresholds asserted below are pinned to observed values, not invented:
see the docstrings for the measurements.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from chat_quality import (  # noqa: E402
    BROADCAST_MAX_USERS,
    FOLLOWUP_EXCLUDED_LEVELS,
    LINK_DOMINANT,
    classify_chats,
)
from fts_engine import _connect_ro  # noqa: E402


def _msg(text: str, sender: str = "a", chat: str = "测试群") -> dict:
    return {"text": text, "sender": sender, "chat_name": chat, "ts": 1700000000}


class TestConnectRo(unittest.TestCase):
    """``_connect_ro`` must survive the WAL-without-shm case that silently
    disabled all name resolution."""

    def test_plain_readonly_when_possible(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plain.db"
            con = sqlite3.connect(str(p))
            con.execute("CREATE TABLE t(x)")
            con.commit()
            con.close()
            con = _connect_ro(p)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)
            finally:
                con.close()

    def test_reads_path_containing_spaces(self):
        """The real vault lives under "Application Support"; the URI must
        survive spaces without manual escaping."""
        with tempfile.TemporaryDirectory(prefix="sp ace ") as d:
            p = Path(d) / "with space.db"
            con = sqlite3.connect(str(p))
            con.execute("CREATE TABLE t(x)")
            con.execute("INSERT INTO t VALUES (42)")
            con.commit()
            con.close()
            con = _connect_ro(p)
            try:
                self.assertEqual(con.execute("SELECT x FROM t").fetchone()[0], 42)
            finally:
                con.close()

    def test_falls_back_to_immutable_when_wal_blocks_readonly(self):
        """Reproduce the vault's state: a WAL-mode database whose sidecars are
        absent. Build it, checkpoint, then delete sidecars."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "wal.db"
            con = sqlite3.connect(str(p))
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE t(x)")
            con.execute("INSERT INTO t VALUES (7)")
            con.commit()
            con.close()
            for sidecar in (f"{p}-wal", f"{p}-shm"):
                try:
                    Path(sidecar).unlink()
                except FileNotFoundError:
                    pass
            # The helper must still return a usable connection.
            con = _connect_ro(p)
            try:
                self.assertEqual(con.execute("SELECT x FROM t").fetchone()[0], 7)
            finally:
                con.close()

    def test_raises_for_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(sqlite3.OperationalError):
                _connect_ro(Path(d) / "nope.db")


class TestGroupNameSignal(unittest.TestCase):
    """Name keywords are the strongest single signal and must actually score."""

    def test_linebao_name_scores_high(self):
        msgs = [_msg(f"普通消息 {i}", "s1") for i in range(20)]
        out = classify_chats({"c1": msgs})
        # 聊天群默认名不含噪声词
        self.assertNotIn("群名特征词", " ".join(out["c1"]["reasons"]))

    def test_noise_name_triggers_signal(self):
        msgs = [_msg(f"消息 {i}", "s1", chat="示例线报群") for i in range(20)]
        out = classify_chats({"c1": msgs})
        joined = " ".join(out["c1"]["reasons"])
        self.assertIn("群名特征词", joined, f"reasons={out['c1']['reasons']}")
        self.assertIn("线报", joined)

    def test_noise_name_scores_but_healthy_chat_not_condemned(self):
        """A name alone must not condemn an otherwise healthy community.

        Measured: 3 name keywords give 25+5*2=35, but a chat with 10 active
        senders and no dominator correctly subtracts 10 (the organic-chat
        signal), landing at 25 / normal. That is the intended balance — the
        name is evidence, not a verdict — so this asserts both halves.
        """
        msgs = [_msg(f"消息 {i}", f"s{i%10}", chat="羊毛线报优惠群") for i in range(20)]
        out = classify_chats({"c1": msgs})["c1"]
        self.assertGreaterEqual(out["score"], 25, "群名信号应当计分")
        self.assertIn("群名特征词", " ".join(out["reasons"]))
        self.assertEqual(out["level"], "normal", "活跃讨论群不应因群名被误伤")

    def test_noise_name_with_broadcast_pattern_reaches_noise(self):
        """Name + broadcast pattern (the real 线报 signature) must escalate."""
        msgs = [_msg(f"消息 {i}", "唯一广播员", chat="羊毛线报优惠群") for i in range(30)]
        out = classify_chats({"c1": msgs})["c1"]
        self.assertIn(out["level"], ("low", "noise"), f"score={out['score']}")
        self.assertGreaterEqual(out["score"], 40)


class TestQuestionDensityInversion(unittest.TestCase):
    """Question marks must not suppress the signal in link-dominated chats."""

    def _link_dominated_chat(self) -> list[dict]:
        # 98% links, 100% top-share, 4 senders, and heavy question usage —
        # mirrors the measured 示例优惠群.
        msgs = []
        for i in range(50):
            if i % 2 == 0:
                msgs.append(_msg(f"https://u.jd.com/abc{i} 这件还有吗？", "s1"))
            else:
                msgs.append(_msg(f"https://u.jd.com/xyz{i} 怎么领？", "s1"))
        return msgs

    def test_link_dominated_with_questions_is_not_rescued(self):
        out = classify_chats({"c1": self._link_dominated_chat()})
        info = out["c1"]
        self.assertGreaterEqual(
            info["score"], 60,
            f"链接主导群不应因问号被救回 normal: score={info['score']}",
        )
        self.assertEqual(info["level"], "noise", f"reasons={info['reasons']}")

    def test_reason_explains_why_questions_ignored(self):
        out = classify_chats({"c1": self._link_dominated_chat()})
        joined = " ".join(out["c1"]["reasons"])
        self.assertIn("不计真实对话", joined)

    def test_genuine_qa_chat_still_benefits(self):
        """A real discussion group (low links, many questions, many senders)
        must still receive the 真实对话 treatment."""
        msgs = [_msg(f"这个方案你怎么看？第{i}个问题", f"user{i%12}") for i in range(40)]
        out = classify_chats({"c1": msgs})
        joined = " ".join(out["c1"]["reasons"])
        self.assertIn("真实对话特征", joined)
        self.assertEqual(out["c1"]["level"], "normal")


class TestLinkGrading(unittest.TestCase):
    def test_dominant_links_score_higher_than_moderate(self):
        moderate = [_msg(f"看看 https://x.com/{i}", f"s{i%10}") for i in range(50)]
        for i in range(25):
            moderate[i] = _msg(f"纯讨论内容第 {i} 条", f"s{i%10}")
        dominant = [_msg(f"https://x.com/{i}", f"s{i%10}") for i in range(50)]

        a = classify_chats({"c1": moderate})["c1"]
        b = classify_chats({"c2": dominant})["c2"]
        self.assertGreater(
            b["score"], a["score"],
            "近乎纯转发应比中等链接密度得分更高",
        )
        self.assertIn("近乎纯转发", " ".join(b["reasons"]))


class TestFollowupExclusionScope(unittest.TestCase):
    def test_low_is_excluded_for_followups(self):
        self.assertIn("low", FOLLOWUP_EXCLUDED_LEVELS)
        self.assertIn("noise", FOLLOWUP_EXCLUDED_LEVELS)

    def test_constants_are_sane(self):
        self.assertTrue(0 < LINK_DOMINANT <= 1)
        self.assertGreater(BROADCAST_MAX_USERS, 0)


class TestBroadcastForwardingRule(unittest.TestCase):
    """The broadcast+forwarding rule is what pushes link-dominated chats over
    the noise threshold. Removing it left every other test green, so it is
    pinned here directly."""

    def _broadcast_chat(self) -> list[dict]:
        # Few senders, near-pure forwarding, no name keywords — the rule is
        # the only thing that can escalate this.
        return [_msg(f"京东福利 https://u.jd.com/{i}", "广播员", chat="普通群名")
                for i in range(60)]

    def test_rule_fires_and_is_reported(self):
        out = classify_chats({"c1": self._broadcast_chat()})["c1"]
        joined = " ".join(out["reasons"])
        self.assertIn("广播群+高转发", joined, f"reasons={out['reasons']}")

    def test_rule_alone_reaches_noise(self):
        out = classify_chats({"c1": self._broadcast_chat()})["c1"]
        self.assertEqual(out["level"], "noise", f"score={out['score']}")

    def test_many_senders_do_not_trigger_rule(self):
        """The rule must not fire once a real community is talking."""
        msgs = [_msg(f"https://u.jd.com/{i}", f"user{i}") for i in range(60)]
        out = classify_chats({"c1": msgs})["c1"]
        self.assertNotIn("广播群+高转发", " ".join(out["reasons"]))


class TestNoiseGroupEndToEnd(unittest.TestCase):
    """The measured worst case, asserted as a whole."""

    def test_broadcast_forwarding_group_is_noise(self):
        msgs = []
        for i in range(100):
            msgs.append(_msg(f"京东福利 https://u.jd.com/{i}", "广播员"))
        out = classify_chats({"c1": msgs})["c1"]
        self.assertEqual(out["level"], "noise", f"score={out['score']} reasons={out['reasons']}")

    def test_healthy_community_stays_normal(self):
        msgs = [_msg(f"我最近在研究第 {i} 个问题，大家怎么看？", f"user{i%25}") for i in range(80)]
        out = classify_chats({"c1": msgs})["c1"]
        self.assertEqual(out["level"], "normal", f"score={out['score']} reasons={out['reasons']}")


class TestPurgeHistoricalResidue(unittest.TestCase):
    """Excluding a chat only skips it during scanning — already-stored items
    persist forever. Measured: 363 of 607 items (59.8%) belonged to chats
    later classified noise/low. The purge must be safe by default."""

    def setUp(self):
        import wd

        self.wd = wd
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "followups.json"
        self.msgs = [
            {"chat_id": "noisy@chatroom", "text": "https://u.jd.com/1", "sender": "b",
             "chat_name": "京东福利群", "ts": 1700000000},
            {"chat_id": "good@chatroom", "text": "合作 brief 预算", "sender": "a",
             "chat_name": "投研群", "ts": 1700000001},
        ]
        self.state = {
            "items": [
                {"id": 1, "key": "k1", "chat": "京东福利群", "chat_id": "noisy@chatroom",
                 "kind": "deal", "title": "京东优惠", "evidence": [], "status": "new",
                 "confidence": "medium", "priority": 2, "reinforcement": 1,
                 "firstSeen": "2026-01-01 00:00:00", "lastSignal": 1700000000,
                 "expires": "2099-01-01 00:00:00", "notes": []},
                {"id": 2, "key": "k2", "chat": "投研群", "chat_id": "good@chatroom",
                 "kind": "deal", "title": "合作 brief", "evidence": [], "status": "new",
                 "confidence": "medium", "priority": 3, "reinforcement": 1,
                 "firstSeen": "2026-01-01 00:00:00", "lastSignal": 1700000001,
                 "expires": "2099-01-01 00:00:00", "notes": []},
            ],
            "feedback": [], "nextId": 3,
        }
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _args(self, **kw):
        from types import SimpleNamespace

        base = dict(list_only=False, triage=None, feedback=None, purge_excluded=True,
                    yes=False, window=30, status="all", limit=20, relevance=None,
                    chat=None, include_noise=False, self_hint=None, note=None, verdict=None,
                    decision=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def _run(self, args):
        import contextlib
        import io
        from unittest import mock

        import chat_quality
        import fts_engine
        import paths

        buf = io.StringIO()
        patch_cls = mock.patch.object(
            chat_quality, "classify_chats",
            return_value={
                "noisy@chatroom": {"level": "noise", "score": 90, "name": "京东福利群"},
                "good@chatroom": {"level": "normal", "score": 0, "name": "投研群"},
            },
        )
        # cmd_followups resolves the state path via `from paths import
        # default_data_root` inside the function body, so the patch must
        # target the paths module, not wd's namespace.
        with mock.patch.object(fts_engine, "fts_recent", return_value=self.msgs), patch_cls, \
                mock.patch.object(paths, "default_data_root", return_value=self.state_path.parent), \
                contextlib.redirect_stdout(buf):
            self.wd.cmd_followups(args)
        return buf.getvalue()

    def test_state_path_is_isolated(self):
        """Guard the test harness itself: if the temp state is not the file
        being read/written, every other assertion here is meaningless."""
        self.assertNotIn(
            str(self.state_path),
            str(Path.home() / ".local" / "share" / "wechat-digger"),
        )
        self._run(self._args(yes=True))
        self.assertTrue(self.state_path.with_suffix(".json.bak").exists())

    def test_dry_run_does_not_modify_state(self):
        before = self.state_path.read_text(encoding="utf-8")
        out = self._run(self._args(yes=False))
        self.assertIn("dryRun", out)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before,
                         "dry-run 绝不能改动状态文件")

    def test_purge_removes_only_excluded_chat_items(self):
        self._run(self._args(yes=True))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["items"]), 1)
        self.assertEqual(saved["items"][0]["chat_id"], "good@chatroom")

    def test_purge_writes_backup_before_deleting(self):
        self._run(self._args(yes=True))
        backup = self.state_path.with_suffix(self.state_path.suffix + ".bak")
        self.assertTrue(backup.exists(), "删除前必须留下备份")
        restored = json.loads(backup.read_text(encoding="utf-8"))
        self.assertEqual(len(restored["items"]), 2, "备份应保留删除前的完整内容")


if __name__ == "__main__":
    unittest.main()
