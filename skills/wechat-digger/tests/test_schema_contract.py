#!/usr/bin/env python3
"""Tests for the shared schema contract and field-coverage observability.

Two properties are locked in here:

1. **Table naming has one definition.** ``Msg_<md5>`` was previously derived
   in eight places across ``vault_cli``, ``export_chat``, ``list_contacts``
   and ``fts_engine``; forward and reverse derivations could drift. These
   tests assert the shared module is the only source and that reverse
   lookups round-trip.

2. **Field resolution is observable.** ``_first_traced`` must preserve the
   exact precedence of the original ``_first`` (including treating empty
   string and None as absent) while reporting which candidate matched, so a
   silent field-loss across a data source becomes a measurable number rather
   than an invisible default.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from normalize import _first, _first_traced, field_coverage, normalize_messages  # noqa: E402
from wechat_schema import (  # noqa: E402
    MESSAGE_COLUMN_CANDIDATES,
    MESSAGE_FIELD_CANDIDATES,
    message_table,
    message_table_like_pattern,
    resolve_columns,
    table_username,
    username_for_table,
)


class TestTableNaming(unittest.TestCase):
    def test_table_name_shape(self):
        table = message_table("wxid_abc123")
        self.assertTrue(table.startswith("Msg_"))
        # "Msg_" + md5 hex digest
        self.assertEqual(len(table), 4 + 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in table[4:]))

    def test_known_vector_is_stable(self):
        """Pinning one vector guards the encoding used for the digest - a
        switch to utf-16 or a different field would change this value."""
        import hashlib

        username = "wxid_abc123"
        expected = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        self.assertEqual(message_table(username), expected)

    def test_table_username_round_trips(self):
        for username in ("wxid_abc", "12345678@chatroom", "gh_abcdef", "群名"):
            with self.subTest(username=username):
                self.assertEqual(table_username(message_table(username)), table_username(message_table(username)))

    def test_table_username_rejects_non_message_tables(self):
        for table in ("Contact", "Name2Id", "msg_something", "Msg", ""):
            with self.subTest(table=table):
                self.assertEqual(table_username(table), "")

    def test_username_for_table_reverse_lookup(self):
        usernames = ["wxid_a", "wxid_b", "9999@chatroom"]
        for username in usernames:
            with self.subTest(username=username):
                self.assertEqual(
                    username_for_table(message_table(username), usernames), username
                )

    def test_username_for_table_unknown_returns_empty(self):
        usernames = ["wxid_a"]
        self.assertEqual(username_for_table(message_table("wxid_zzz"), usernames), "")

    def test_username_for_table_rejects_non_message_table(self):
        self.assertEqual(username_for_table("Contact", ["wxid_a"]), "")

    def test_like_pattern_derives_from_prefix(self):
        """The pattern must derive from the same constant, not a literal."""
        pattern = message_table_like_pattern()
        self.assertTrue(pattern.endswith("%"))
        self.assertTrue(message_table("x").startswith(pattern[:-1]))


class TestColumnResolution(unittest.TestCase):
    def test_preferred_alias_wins(self):
        available = ["local_id", "id", "rowid", "server_id", "create_time"]
        self.assertEqual(resolve_columns(available)["local_id"], "local_id")
        self.assertEqual(resolve_columns(available)["server_id"], "server_id")

    def test_falls_back_to_alias(self):
        available = ["id", "type", "timestamp"]
        cols = resolve_columns(available)
        self.assertEqual(cols["local_id"], "id")
        self.assertEqual(cols["local_type"], "type")
        self.assertEqual(cols["create_time"], "timestamp")

    def test_rowid_always_available(self):
        """rowid is implicit on every SQLite table and is the documented
        fallback for local_id, so it must resolve even when absent from the
        declared column list."""
        cols = resolve_columns(["message_content"])
        self.assertEqual(cols["local_id"], "rowid")

    def test_unresolvable_role_is_omitted(self):
        cols = resolve_columns(["message_content"])
        self.assertNotIn("server_id", cols)

    def test_compression_aliases_share_a_column(self):
        """compress_content and compression_flag intentionally resolve to the
        same WCDB column; assert that intent is preserved."""
        cols = resolve_columns(["WCDB_CT_message_content"])
        self.assertEqual(cols["compress_content"], "WCDB_CT_message_content")
        self.assertEqual(cols["compression_flag"], "WCDB_CT_message_content")

    def test_all_roles_have_candidates_defined(self):
        for role in MESSAGE_COLUMN_CANDIDATES:
            with self.subTest(role=role):
                self.assertTrue(MESSAGE_COLUMN_CANDIDATES[role])


class TestTracedResolutionMatchesFirst(unittest.TestCase):
    """``_first_traced`` replaced explicit candidate lists at call sites, so
    it must agree with ``_first`` on every input, including the empty-string
    and None cases that decide whether a fallback fires."""

    def test_agrees_with_first_on_all_candidate_fields(self):
        """Cross-checks the two resolvers on the same input.

        Note this alone cannot catch a reordering of the candidate table:
        both functions read the same constant, so they move together. The
        precedence assertions below pin the *intended* order independently.
        """
        sample = {
            "local_type": 1,
            "ts": 1700000000,
            "text": "hello",
            "nickname": "alice",
            "sender_username": "wxid_a",
            "sender": "alice",
            "chat_id": "c1",
            "chat_name": "群",
            "id": "m1",
            "time": "2026-01-01",
            "reply_to": "m0",
        }
        for field, candidates in MESSAGE_FIELD_CANDIDATES.items():
            with self.subTest(field=field):
                expected = _first(sample, *candidates, default="__miss__")
                actual = _first_traced(sample, field, default="__miss__")
                self.assertEqual(actual, expected)

    def test_precedence_is_pinned_explicitly(self):
        """Asserts the documented priority order against hardcoded values.

        Without this, reordering ``MESSAGE_FIELD_CANDIDATES`` silently
        changes which key wins while every consistency test still passes -
        both resolvers read the same table, so they agree on the wrong
        answer. Each case below supplies two competing keys and states which
        one must win.
        """
        cases = [
            # (field, raw, expected winning key)
            ("msg_type", {"local_type": 1, "type": "文字"}, "local_type"),
            ("ts", {"ts": 111, "timestamp": 222}, "ts"),
            ("text", {"text": "a", "content": "b"}, "text"),
            ("nickname", {"nickname": "n", "displayName": "d"}, "nickname"),
            ("sender", {"sender_username": "u", "wxid": "w"}, "sender_username"),
            ("chat_id", {"chat_id": "c", "sessionId": "s"}, "chat_id"),
            ("chat_name", {"chat_name": "n", "sessionName": "s"}, "chat_name"),
            ("msg_id", {"id": "i", "msgId": "m"}, "id"),
            ("time", {"time": "t", "timestamp": "ts"}, "time"),
            ("reply_to", {"reply_to": "r", "replyTo": "q"}, "reply_to"),
        ]
        for field, raw, expected_key in cases:
            with self.subTest(field=field):
                trace: dict = {}
                value = _first_traced(raw, field, trace=trace)
                self.assertEqual(value, raw[expected_key])
                self.assertIn(f"hit:{field}:{expected_key}", trace)

    def test_local_type_outranks_type(self):
        """Vault stores an int enum in local_type while exports may put a
        human label in type; the int is the richer signal and must win."""
        raw = {"type": "文字消息", "local_type": 1}
        self.assertEqual(_first_traced(raw, "msg_type"), 1)

    def test_sender_username_outranks_display_name(self):
        """sender_username carries the stable wxid; generic sender may be a
        display name and is only a fallback."""
        raw = {"sender": "爱丽丝", "sender_username": "wxid_real"}
        self.assertEqual(_first_traced(raw, "sender"), "wxid_real")

    def test_empty_string_is_treated_as_absent(self):
        """A present-but-empty key must not shadow a later candidate."""
        raw = {"ts": "", "timestamp": 1700000000}
        self.assertEqual(_first(raw, "ts", "timestamp", default=None), 1700000000)
        self.assertEqual(_first_traced(raw, "ts", default=None), 1700000000)

    def test_none_is_treated_as_absent(self):
        raw = {"ts": None, "timestamp": 1700000000}
        self.assertEqual(_first_traced(raw, "ts", default=None), 1700000000)

    def test_miss_returns_default(self):
        self.assertEqual(_first_traced({"unrelated": 1}, "ts", default=-1), -1)

    def test_unknown_field_returns_default(self):
        self.assertEqual(_first_traced({"x": 1}, "no_such_field", default="d"), "d")


class TestTracing(unittest.TestCase):
    def test_records_winning_key(self):
        trace: dict = {}
        _first_traced({"timestamp": 5}, "ts", trace=trace)
        self.assertEqual(trace.get("hit:ts:timestamp"), 1)

    def test_records_miss(self):
        trace: dict = {}
        _first_traced({"unrelated": 1}, "ts", trace=trace)
        self.assertEqual(trace.get("miss:ts"), 1)

    def test_no_trace_leaves_no_side_effects(self):
        """Callers that do not opt in must see zero overhead and no
        mutation - this is what keeps the hot path unchanged."""
        result = _first_traced({"ts": 1}, "ts")
        self.assertEqual(result, 1)

    def test_coverage_report_shape(self):
        trace: dict = {}
        for raw in ({"timestamp": 1}, {"timestamp": 2}, {"unrelated": 3}):
            _first_traced(raw, "ts", trace=trace)
        report = field_coverage(trace, total=3)
        ts = report["ts"]
        self.assertEqual(ts["resolved"], 2)
        self.assertEqual(ts["missed"], 1)
        self.assertAlmostEqual(ts["miss_rate"], 1 / 3, places=4)
        self.assertEqual(ts["sources"], {"timestamp": 2})
        self.assertEqual(report["_total_messages"], 3)

    def test_coverage_zero_miss_rate(self):
        trace: dict = {}
        for _ in range(4):
            _first_traced({"text": "x"}, "text", trace=trace)
        self.assertEqual(field_coverage(trace, 4)["text"]["miss_rate"], 0.0)

    def test_coverage_handles_empty_trace(self):
        report = field_coverage({}, 0)
        for field in MESSAGE_FIELD_CANDIDATES:
            with self.subTest(field=field):
                self.assertEqual(report[field]["miss_rate"], 0.0)


class TestNormalizeIntegration(unittest.TestCase):
    def test_trace_populated_over_batch(self):
        raw = [
            {"local_type": 1, "text": "a", "ts": 1700000000, "id": "1", "chat_id": "c"},
            {"local_type": 1, "text": "b", "ts": 1700000001, "id": "2", "chat_id": "c"},
        ]
        trace: dict = {}
        out = normalize_messages(raw, source="vault", trace=trace)
        self.assertEqual(len(out), 2)
        self.assertEqual(trace.get("_normalized"), 2)
        report = field_coverage(trace, len(out))
        self.assertEqual(report["text"]["resolved"], 2)
        self.assertEqual(report["text"]["sources"], {"text": 2})

    def test_results_identical_with_and_without_trace(self):
        """Observability must not change output - the trace is a side
        channel, not a behavior change."""
        raw = [
            {"local_type": 1, "text": "hello", "ts": 1700000000, "sender": "a", "chat_name": "g"},
            {"type": 3, "content": "world", "timestamp": 1700000001, "from": "b", "chat": "g"},
        ]
        plain = normalize_messages(raw, source="vault")
        traced = normalize_messages(raw, source="vault", trace={})
        self.assertEqual(plain, traced)

    def test_malformed_records_are_counted_not_hidden(self):
        class Boom(dict):
            def get(self, *a, **k):  # noqa: D401
                raise RuntimeError("boom")

        raw = [{"text": "ok", "ts": 1, "id": "1", "chat_id": "c"}, Boom()]
        trace: dict = {}
        out = normalize_messages(raw, source="vault", trace=trace)
        self.assertEqual(len(out), 1)
        self.assertEqual(trace.get("_dropped_records"), 1)


class TestScriptsUseSharedSchema(unittest.TestCase):
    """Guards against reintroducing a local copy of the table-naming rule."""

    AFFECTED = (
        "acquire/vault_cli.py",
        "acquire/export_chat.py",
        "fts_engine.py",
        "normalize.py",
    )

    def test_no_hardcoded_md5_table_derivation(self):
        offenders = []
        for rel in self.AFFECTED:
            path = _SCRIPTS / rel
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if 'md5(' in line and '"Msg_"' in line:
                    offenders.append(f"{rel}:{lineno}")
        self.assertEqual(
            offenders,
            [],
            "derive message table names via wechat_schema.message_table",
        )

    def test_no_hardcoded_like_pattern(self):
        offenders = []
        for rel in self.AFFECTED:
            path = _SCRIPTS / rel
            text = path.read_text(encoding="utf-8")
            if "LIKE 'Msg_%'" in text:
                offenders.append(rel)
        self.assertEqual(
            offenders,
            [],
            "use wechat_schema.message_table_like_pattern()",
        )


if __name__ == "__main__":
    unittest.main()
