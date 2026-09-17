#!/usr/bin/env python3
"""Contracts of the extra-layer list commands.

Two ways a list command can lie about a ``limit``:

1. **Filter after truncating** — `list_voice(chat=...)` filtered in Python
   *after* the SQL `LIMIT`, so asking for a chat whose voice messages are not
   among the newest `limit` rows machine-wide returned ``count: 0`` with no
   error. Measured on a live vault: one chat holds 40 voice messages and
   returned 0 for limit=20, 50 and 200 — the newest voices are dominated by a
   few busy group chats, so this hit any normal one-to-one chat.
2. **Truncate per source** — `list_payments(limit=...)` passed the limit to
   each table's own query, so the merged result was up to 2x the limit
   (measured: --limit 1 returned 2, --limit 3 returned 6).

These tests are hermetic: synthetic vaults are built in temp dirs and the
module's root resolver is patched, so the contracts are pinned without
depending on the operator's vault (the live tests in `test_extra_layers.py`
skip when no vault exists, which is exactly when a regression goes unnoticed).
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import extra_layers  # noqa: E402


def _build_vault(root: Path, chats):
    """chats: list of (rowid, username, [create_time, ...]) newest first."""
    media_dir = root / "message"
    media_dir.mkdir(parents=True, exist_ok=True)
    db = media_dir / "media_0.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)")
    con.execute("""CREATE TABLE VoiceInfo (
        local_id INTEGER PRIMARY KEY, svr_id INTEGER, chat_name_id INTEGER,
        create_time INTEGER, voice_data BLOB)""")
    for rowid, username, times in chats:
        con.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (?,?)",
                    (rowid, username))
        for i, ts in enumerate(times):
            con.execute(
                "INSERT INTO VoiceInfo (local_id, svr_id, chat_name_id, create_time, voice_data)"
                " VALUES (?,?,?,?,?)",
                (rowid * 1000 + i, rowid * 1000 + i, rowid, ts, b"#!SILK" + b"\x00" * 8))
    con.commit()
    con.close()
    return root


class TestVoiceFilterOrder(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "vault"
        # The chat we will ask for is the OLDEST by create_time, so a
        # post-LIMIT filter cannot see it at any small limit.
        base = 1_700_000_000
        busy = [(1, "busy@chatroom", [base + i * 60 for i in range(30)]),
                (2, "other@chatroom", [base + i * 60 for i in range(20)]),
                (3, "quiet_wxid", [base - 100000 + i for i in range(3)])]
        _build_vault(self.root, busy)
        self._orig = extra_layers.decrypted_root
        extra_layers.decrypted_root = lambda: self.root

    def tearDown(self):
        extra_layers.decrypted_root = self._orig
        self._td.cleanup()

    def test_chat_filter_is_not_lost_to_the_limit(self):
        out = extra_layers.list_voice(chat="quiet_wxid", limit=5)
        self.assertEqual(
            out.get("count"), 3,
            "the chat's voice messages must be found regardless of their "
            "recency relative to the global LIMIT")

    def test_limit_still_caps_after_filtering(self):
        out = extra_layers.list_voice(chat="busy@chatroom", limit=7)
        self.assertEqual(out.get("count"), 7)
        times = [v["ts"] for v in out["voice"]]
        self.assertEqual(times, sorted(times, reverse=True), "newest first")

    def test_limit_above_the_chat_total_returns_all_of_them(self):
        out = extra_layers.list_voice(chat="busy@chatroom", limit=500)
        self.assertEqual(out.get("count"), 30)

    def test_no_chat_filter_is_unchanged(self):
        out = extra_layers.list_voice(limit=4)
        self.assertEqual(out.get("count"), 4)
        self.assertTrue(all("chat_id" in v for v in out["voice"]))

    def test_unmatched_chat_says_so_instead_of_a_bare_zero(self):
        out = extra_layers.list_voice(chat="no-such-chat", limit=5)
        self.assertEqual(out.get("count"), 0)
        self.assertIn("note", out, "a 0 that means 'no such chat' must say so")

    def test_substring_match_still_works(self):
        out = extra_layers.list_voice(chat="quiet", limit=5)
        self.assertEqual(out.get("count"), 3)

    def test_matching_is_case_insensitive(self):
        out = extra_layers.list_voice(chat="BUSY@CHATROOM", limit=5)
        self.assertEqual(out.get("count"), 5)


def _build_general_db(root: Path, transfers, redpackets):
    """transfers: [(id, session, ts)]  redpackets: [(id, session)]"""
    gdir = root / "general"
    gdir.mkdir(parents=True, exist_ok=True)
    db = gdir / "general.db"
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE transferTable (
        transfer_id TEXT, session_name TEXT, pay_sub_type INTEGER,
        pay_payer TEXT, pay_receiver TEXT,
        begin_transfer_time INTEGER, last_update_time INTEGER)""")
    con.execute("""CREATE TABLE redEnvelopeTable (
        message_server_id TEXT, session_name TEXT, sender_user_name TEXT,
        hb_status INTEGER, hb_type INTEGER, receive_status INTEGER)""")
    for tid, session, ts in transfers:
        con.execute("INSERT INTO transferTable VALUES (?,?,?,?,?,?,?)",
                    (tid, session, 1, "sender", "receiver", ts, ts))
    for rid, session in redpackets:
        con.execute("INSERT INTO redEnvelopeTable VALUES (?,?,?,?,?,?)",
                    (rid, session, "sender", 2, 1, 1))
    con.commit()
    con.close()
    return root


class TestPaymentsLimitMeansLimit(unittest.TestCase):
    """`limit` caps the merged result, not each source table."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name) / "vault"
        _build_general_db(
            self.root,
            [("t%d" % i, "chat_a", 1_700_000_000 + i * 60) for i in range(6)],
            [("r%d" % i, "chat_a") for i in range(5)],
        )
        self._orig = extra_layers.decrypted_root
        extra_layers.decrypted_root = lambda: self.root

    def tearDown(self):
        extra_layers.decrypted_root = self._orig
        self._td.cleanup()

    def test_count_never_exceeds_the_limit(self):
        for limit in (1, 2, 3, 5):
            out = extra_layers.list_payments(kind="all", limit=limit)
            self.assertLessEqual(out["count"], limit,
                                 "limit=%d returned %d" % (limit, out["count"]))
            self.assertEqual(out["count"], len(out["payments"]))

    def test_single_kind_still_caps(self):
        out = extra_layers.list_payments(kind="transfer", limit=3)
        self.assertEqual(out["count"], 3)
        self.assertTrue(all(p["kind"] == "transfer" for p in out["payments"]))

    def test_timed_rows_come_back_newest_first(self):
        out = extra_layers.list_payments(kind="transfer", limit=4)
        times = [p["ts"] for p in out["payments"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_a_time_filter_that_cannot_apply_is_disclosed(self):
        out = extra_layers.list_payments(kind="all", since="2026-01-01", limit=3)
        self.assertIn("note", out,
                      "red packets have no time column; silently ignoring "
                      "--since must be stated")

    def test_no_note_when_no_time_filter_was_asked_for(self):
        out = extra_layers.list_payments(kind="all", limit=3)
        self.assertNotIn("note", out)


if __name__ == "__main__":
    unittest.main()
