"""User-evidence projection: index-time write, read-time render.

Guards the P1 search-path fix. Evidence used to be read per hit with
``SELECT ... FROM messages_fts WHERE session_id = ?``, which FTS5 answers by
scanning the whole content table (session_id is UNINDEXED). On the live index
that was 127 ms per hit, 1240 ms per miss, 85% of a 20-result search.

These tests pin the two properties the fix relies on: the projection is a
faithful, size-bounded rendering of the session's user turns, and the renderer
needs no database access at all.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "index_builder"))

from index_builder._evidence import (  # noqa: E402
    DEFAULT_MESSAGES,
    EVIDENCE_TEXT_CAP,
    evidence_from_row,
    projection_available,
    project_user_evidence,
)


class TestProjection(unittest.TestCase):
    def test_keeps_only_user_turns_in_order(self):
        blob = project_user_evidence([
            {"role": "USER", "timestamp": "t1", "text": "first"},
            {"role": "ASSISTANT", "timestamp": "t2", "text": "reply"},
            {"role": "USER", "timestamp": "t3", "text": "second"},
        ])
        items = json.loads(blob)
        self.assertEqual([i["text"] for i in items], ["first", "second"])
        self.assertEqual([i["ts"] for i in items], ["t1", "t3"])

    def test_lowercase_role_still_counts(self):
        blob = project_user_evidence([{"role": "user", "timestamp": "t", "text": "x"}])
        self.assertEqual(len(json.loads(blob)), 1)

    def test_empty_and_blank_turns_are_dropped(self):
        blob = project_user_evidence([
            {"role": "USER", "timestamp": "t", "text": ""},
            {"role": "USER", "timestamp": "t"},
            None,
            {"role": "USER", "timestamp": "t", "text": "kept"},
        ])
        self.assertEqual([i["text"] for i in json.loads(blob)], ["kept"])

    def test_text_is_capped_but_decision_sees_full_text(self):
        """Display is capped; the decision flag is evaluated before the cap.

        The cap exists to bound index growth, and the flag is computed here
        precisely so a decision phrase past the cap is still recorded.
        """
        long_text = "x" * (EVIDENCE_TEXT_CAP + 50) + "决定改用方案 B"
        blob = project_user_evidence([{"role": "USER", "timestamp": "t", "text": long_text}])
        item = json.loads(blob)[0]
        self.assertEqual(len(item["text"]), EVIDENCE_TEXT_CAP)
        self.assertNotIn("决定", item["text"], "phrase sits past the display cap")
        self.assertTrue(item["decision"], "decision must be seen in the full text")

    def test_message_count_is_bounded(self):
        msgs = [{"role": "USER", "timestamp": str(i), "text": f"m{i}"}
                for i in range(DEFAULT_MESSAGES + 20)]
        self.assertEqual(len(json.loads(project_user_evidence(msgs))), DEFAULT_MESSAGES)

    def test_empty_input_yields_parsable_empty_list(self):
        self.assertEqual(json.loads(project_user_evidence([])), [])
        self.assertEqual(json.loads(project_user_evidence(None)), [])


class TestRender(unittest.TestCase):
    def _row(self, **kw):
        return {"user_evidence_json": kw.pop("evidence", "[]"),
                "tool_errors_json": kw.pop("tool_errors", None), **kw}

    def test_round_trip(self):
        blob = project_user_evidence([
            {"role": "USER", "timestamp": "t1", "text": "先做减法"},
            {"role": "USER", "timestamp": "t2", "text": "决定改用索引"},
        ])
        ev = evidence_from_row(self._row(evidence=blob))
        self.assertEqual([m["text"] for m in ev["user_messages"]],
                         ["先做减法", "决定改用索引"])
        self.assertIsNone(ev["decisions"], "decisions not requested → None, as before")

    def test_decisions_only_when_requested(self):
        blob = project_user_evidence([
            {"role": "USER", "timestamp": "t1", "text": "先做减法"},
            {"role": "USER", "timestamp": "t2", "text": "决定改用索引"},
        ])
        row = self._row(evidence=blob)
        self.assertEqual(evidence_from_row(row, decisions=False)["decisions"], None)
        hits = evidence_from_row(row, decisions=True)["decisions"]
        self.assertEqual([h["text"] for h in hits], ["决定改用索引"])

    def test_limit_is_applied_at_render(self):
        blob = project_user_evidence([
            {"role": "USER", "timestamp": str(i), "text": f"m{i}"} for i in range(10)
        ])
        ev = evidence_from_row(self._row(evidence=blob), limit_msgs=3)
        self.assertEqual(len(ev["user_messages"]), 3)

    def test_tool_errors_come_from_the_aggregate_column(self):
        ev = evidence_from_row(self._row(tool_errors='{"Bash": 5, "Edit": 2}'))
        self.assertEqual([t["name"] for t in ev["tool_errors"]], ["Bash", "Edit"])
        self.assertIn("5 failed", ev["tool_errors"][0]["result_preview"])

    def test_absent_projection_yields_empty_not_error(self):
        """Rows before schema v4 carry '' — the *renderer* yields no evidence.

        That is correct but incomplete on its own: callers must ask
        ``projection_available`` first, because "renders as empty" is
        indistinguishable from "this session has no user turns".
        """
        for raw in ("", None, "not json", "{}", "[1,2]"):
            ev = evidence_from_row(self._row(evidence=raw))
            self.assertEqual(ev["user_messages"], [], f"raw={raw!r}")
        ev = evidence_from_row(self._row(evidence="not json"))
        self.assertEqual(ev["tool_errors"], [])

    def test_row_may_be_none(self):
        ev = evidence_from_row(None)
        self.assertEqual(ev, {"user_messages": [], "tool_errors": [],
                              "decisions": None})


class TestProjectionAvailable(unittest.TestCase):
    """``''`` (never computed) must be distinguishable from ``'[]'`` (empty).

    The renderer is deliberately dumb about this — it returns an empty dict
    either way — so the *caller* decides. When ``cmd_search`` gated only on
    "the index has a row", every un-rebuilt row rendered zero user messages and
    ``--decisions`` produced output identical to a plain search. On the live
    index that was 183 rows (2.9%) the scan can no longer revisit, plus every
    row of any install whose build predates schema v4.
    """

    def test_empty_string_is_not_a_projection(self):
        self.assertFalse(projection_available({"user_evidence_json": ""}))

    def test_computed_empty_list_is_a_projection(self):
        """'[]' means "computed, this session has no user turns"."""
        self.assertTrue(projection_available({"user_evidence_json": "[]"}))

    def test_missing_key_and_none_row_are_not_projections(self):
        self.assertFalse(projection_available({}))
        self.assertFalse(projection_available(None))

    def test_real_blob_is_a_projection(self):
        blob = project_user_evidence([{"role": "USER", "timestamp": "t", "text": "x"}])
        self.assertTrue(projection_available({"user_evidence_json": blob}))

    def test_caller_can_tell_the_two_apart_where_the_renderer_cannot(self):
        never = {"user_evidence_json": ""}
        empty = {"user_evidence_json": "[]"}
        self.assertEqual(evidence_from_row(never), evidence_from_row(empty))
        self.assertNotEqual(projection_available(never), projection_available(empty))


if __name__ == "__main__":
    unittest.main()
