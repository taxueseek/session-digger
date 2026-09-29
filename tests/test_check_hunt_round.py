#!/usr/bin/env python3
"""One check+hunt round: the fixes, and the pins that keep them fixed.

Everything below was measured on the live install (6948 sessions, 16
environments, 215 MB index) before it was changed, and re-measured after. The
theme of the round is that the *same* defect kept reappearing in a new place,
so most of these tests assert a rule at the place the rule is defined rather
than at the call site that happened to break:

  * ``limit = 0`` means "no cap" to Python and "return nothing" to SQL. Three
    adapters bound the Python value straight into ``LIMIT ?`` and answered
    "give me everything" with an empty result set.
  * A predicate applied after a fixed window starves whatever it selects. The
    round-robin spent slots on out-of-scope sessions and then threw them away.
  * A rule implemented in one function and re-derived at N call sites drifts;
    N grew to five for the sidecar predicate and seven for ``cap``.

The tests that matter are the ones that fail when the fix is reverted, and each
class below says which mutation it catches.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402
from echolib._helpers import cap, is_sidecar_jsonl, sql_limit  # noqa: E402


def _load(module_name, filename):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SD_RECALL = _load("sd_recall_under_test", "sd-recall.py")


# ---------------------------------------------------------------------------
# The limit=0 convention, in both of its spellings
# ---------------------------------------------------------------------------

class LimitZeroConventionTest(unittest.TestCase):
    """``0`` (or None) means "no cap" — to Python and to SQL alike.

    Mutation caught: reverting ``sql_limit`` to a bare ``limit``, or restoring
    ``while len(result) < limit`` in the rotation, turns these red.
    """

    def test_sql_limit_translates_the_two_spellings(self):
        self.assertEqual(sql_limit(0), -1)
        self.assertEqual(sql_limit(None), -1)
        self.assertEqual(sql_limit(20), 20)
        self.assertEqual(sql_limit(-5), -5)

    def test_sql_limit_negative_means_unbounded_in_sqlite(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
        self.assertEqual(len(conn.execute("SELECT x FROM t LIMIT ?",
                                          (sql_limit(0),)).fetchall()), 50)
        self.assertEqual(len(conn.execute("SELECT x FROM t LIMIT ?",
                                          (sql_limit(5),)).fetchall()), 5)
        conn.close()

    def test_round_robin_does_not_exit_immediately_on_zero(self):
        # The rotation's own guard: `len(result) < limit` is False at 0.
        src = (SCRIPTS / "echolib" / "_adapters.py").read_text(encoding="utf-8")
        self.assertIn("limit <= 0 or len(result) < limit", src)
        # …and the per-adapter request must not floor it back to 20.
        self.assertIn("limit * 2 if limit > 0 else 0", src)

    def test_cap_is_the_one_home_for_the_rule(self):
        self.assertEqual(cap([1, 2, 3], 0), [1, 2, 3])
        self.assertEqual(cap([1, 2, 3], None), [1, 2, 3])
        self.assertEqual(cap([1, 2, 3], 2), [1, 2])

    def test_no_call_site_reinlines_cap(self):
        """Seven sites re-derived the rule; the guard regex could not see them.

        The old pattern was anchored to end-of-line, so
        ``return x[:limit] if limit else x`` never matched. This asserts the
        rule is *called*, not reimplemented — which is what makes the next
        adapter inherit it.
        """
        offenders = []
        for path in sorted((SCRIPTS / "echolib").glob("*.py")):
            if path.name == "_helpers.py":
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"return\s+\w+\[:limit\]", line):
                    offenders.append(f"{path.name}:{n}")
        self.assertEqual(offenders, [],
                         "these re-derive cap() instead of calling it")

    def test_loop_guards_do_not_break_on_row_one(self):
        """``if len(x) >= limit`` is true at 0, so the loop stops after 1 row.

        """
        # dimcode and zcode_db both had this shape behind a SQL ``LIMIT -1``, so
        # fixing only the SQL turned "0 rows" into "1 row". The evidence
        # projector is exempt: its ``limit`` is a display count that is never 0.
        for path in (SCRIPTS / "echolib" / "_adapters_zcode.py", SCRIPTS / "sd-recall.py"):
            text = path.read_text(encoding="utf-8")
            bare = [f"{path.name}:{n}" for n, line in enumerate(text.splitlines(), 1)
                    if re.search(r"if len\(\w+\) >= limit", line)]
            self.assertEqual(bare, [], f"{path.name} has an unguarded length check")


class SqlBackedAdapterLimitTest(unittest.TestCase):
    """The three SQL-backed adapters must honour ``limit=0`` like the rest.

    Mutation caught: removing ``sql_limit(...)`` from any of the three query
    sites, or restoring ``(limit * 2,)`` / ``(limit,)`` verbatim.
    """

    def test_every_adapter_treats_zero_as_unlimited(self):
        broken = []
        for name in echolib.ADAPTER_REGISTRY:
            fn = echolib.ADAPTER_REGISTRY[name].get("list_sessions")
            if not fn:
                continue
            try:
                with echolib.discovery_only():
                    everything = fn(limit=0, keyword="")
                    capped = fn(limit=200, keyword="")
                n_all = len(everything) if everything is not None else -1
                n_cap = len(capped) if capped is not None else -1
            except Exception as exc:  # pragma: no cover - surfaced by the assert
                broken.append(f"{name} raised {type(exc).__name__}")
                continue
            if n_all == 0 and n_cap > 0:
                broken.append(f"{name}: limit=0 -> 0 rows but limit=200 -> {n_cap}")
        self.assertEqual(broken, [], "these adapters answer 'everything' with nothing")

    def test_no_raw_python_limit_reaches_sql(self):
        """``LIMIT ?`` must be bound through ``sql_limit``.

        Greps the three files that build SQL against the adapter registry. A new
        ``LIMIT ?`` added without the translation fails here rather than in
        production, where it would look like "this environment has no sessions".
        """
        offenders = []
        # Scoped to echolib/: the CLI commands take ``type=int`` limits from
        # argparse and can never be 0, so they are not where this convention
        # applies. The adapters are the ones whose limit crosses the boundary.
        for path in sorted((SCRIPTS / "echolib").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for n, line in enumerate(text.splitlines(), 1):
                # A SQL terminator, not the phrase ``LIMIT ?`` inside a
                # docstring explaining the rule.
                if not re.search(r"LIMIT \?[\"']?\s*$", line):
                    continue
                window = "\n".join(text.splitlines()[max(0, n - 6):n + 2])
                if not re.search(r"sql_limit\(", window):
                    offenders.append(f"{path.name}:{n}")
        self.assertEqual(offenders, [], "these bind a raw limit into SQL")


# ---------------------------------------------------------------------------
# A filter belongs to the selection, not to a sweep over its output
# ---------------------------------------------------------------------------

class FilterInsideSelectionTest(unittest.TestCase):
    """``--scope current`` must not lose sessions to a fixed window.

    Measured 2026-09-24 from this repository's root: the registry path asked for
    ``max(limit*5, 40)`` rows, the round-robin spread them over 15 environments,
    and the cwd predicate then kept 26 of 194 in-scope sessions — 87% of the
    project's history was invisible, including every kimix and 67 of 75 grok
    sessions. With the predicate inside the rotation: 194 of 194.
    """

    def test_registry_window_opens_when_a_predicate_is_present(self):
        src = (SCRIPTS / "sd-recall.py").read_text(encoding="utf-8")
        self.assertIn("fetch_n = max(limit * 5, 40) if keep is None else 0", src)

    def test_cross_tool_accepts_and_applies_the_predicate(self):
        src = (SCRIPTS / "echolib" / "_adapters.py").read_text(encoding="utf-8")
        # Assert the *parameter* exists rather than pinning the whole signature:
        # the previous exact-match form failed the moment an unrelated keyword
        # (``previews``) was added, which is a false alarm about a gate that was
        # really protecting two things — the ``keep`` argument and its use
        # inside the rotation. A gate that breaks on any signature growth stops
        # being read as a signal.
        sig = re.search(r"def cross_tool_list_sessions\((.*?)\):", src, re.S)
        self.assertIsNotNone(sig, "cross_tool_list_sessions signature not found")
        self.assertIn("keep=None", sig.group(1))
        self.assertIn("keep(s, name)", src)

    def test_session_in_cwd_memoises_its_invariants(self):
        """``cwd`` and ``$HOME`` were re-resolved for every candidate row.

        The predicate now runs over every session of every environment, so two
        ``realpath`` calls per row measured as the entire cost of a
        ``--scope current`` listing (0.20 s → 0.27 s over ~7,000 rows).
        """
        src = (SCRIPTS / "echolib" / "_helpers.py").read_text(encoding="utf-8")
        self.assertIn("@functools.lru_cache(maxsize=None)", src)
        self.assertIn("def _resolve_cwd(cwd):", src)
        self.assertIn("def _home_resolved():", src)

    def test_memoisation_does_not_change_the_answer(self):
        from echolib._helpers import session_in_cwd
        cwd = os.getcwd()
        self.assertTrue(session_in_cwd(str(Path(cwd) / "x.jsonl"), cwd))
        self.assertFalse(session_in_cwd("/somewhere/else/x.jsonl", cwd))
        # $HOME is never a project
        self.assertFalse(session_in_cwd(str(Path.home() / "x.jsonl"), str(Path.home())))


# ---------------------------------------------------------------------------
# The recall-lite contract: a search term has to reach the search
# ---------------------------------------------------------------------------

class RecallLiteKeywordTest(unittest.TestCase):
    """``recall-lite.sh <term>`` listed recent sessions and called them matches.

    The shell front-end took ``$1`` as a keyword, but ``sd-recall.py sessions``
    had no keyword argument to receive it and the shell never passed one — so a
    term occurring nowhere and one occurring everywhere printed byte-identical
    output. The keyword is now a real argument on both sides.
    """

    def test_sessions_subcommand_accepts_a_keyword(self):
        src = (SCRIPTS / "sd-recall.py").read_text(encoding="utf-8")
        self.assertIn('p_list.add_argument("keyword", nargs="?", default=None', src)

    def test_cmd_sessions_forwards_the_keyword(self):
        src = (SCRIPTS / "sd-recall.py").read_text(encoding="utf-8")
        self.assertIn('keyword=getattr(args, "keyword", None)', src)

    def test_shell_passes_the_query_to_both_listing_calls(self):
        script = (SCRIPTS / "recall-lite.sh").read_text(encoding="utf-8")
        self.assertEqual(script.count('sd-recall.py" sessions "$QUERY"'), 2)

    def test_keyword_reaches_both_lookup_paths(self):
        """The term must reach the FTS query *and* the registry fallback.

        Asserted at the seam rather than end-to-end on purpose: an end-to-end
        assertion needs a session inside a registered environment root, which
        would mean writing into the user's ``~/.claude``. What broke was that
        the term reached neither.
        """
        seen = {}
        real_fts, real_reg = SD_RECALL._fts_search, SD_RECALL._list_from_registry

        def spy_fts(keyword, **kw):
            seen["fts"] = keyword
            return real_fts(keyword, **kw)

        def spy_reg(**kw):
            seen["registry"] = kw.get("keyword")
            return real_reg(**kw)

        SD_RECALL._fts_search = spy_fts
        SD_RECALL._list_from_registry = spy_reg
        try:
            SD_RECALL.find_sessions(scope="all", limit=5, keyword="数据库")
        finally:
            SD_RECALL._fts_search = real_fts
            SD_RECALL._list_from_registry = real_reg
        self.assertEqual(seen.get("fts"), "数据库")
        self.assertEqual(seen.get("registry"), "数据库")

    def test_shell_passes_the_query_not_a_blank(self):
        """``sessions`` with no keyword lists recents; with one it must filter."""
        script = (SCRIPTS / "recall-lite.sh").read_text(encoding="utf-8")
        # The old call had no positional argument at all.
        self.assertNotIn('sd-recall.py" sessions --scope', script)


# ---------------------------------------------------------------------------
# Virtual paths are not files, and must not be re-parsed as if they were
# ---------------------------------------------------------------------------

class VirtualPathListingTest(unittest.TestCase):
    """``os.path.getmtime`` raises for ``dimcode://`` — that is not staleness.

    Treating the OSError as "the file changed" re-parsed the whole session on
    every listing. Measured: 16 of 16 rows fell back to
    ``dispatch_session_stats`` for 170 ms of a 248 ms ``sessions --limit 20``.
    The builder's per-session fingerprint is the change detector for these, so a
    row in the index *is* current.
    """

    def test_virtual_path_is_trusted_from_the_index(self):
        src = (SCRIPTS / "sd-recall.py").read_text(encoding="utf-8")
        self.assertIn("virtual = _is_virtual_path(path)", src)
        self.assertIn("(virtual or (file_mtime is not None", src)

    def test_virtual_paths_are_not_os_paths(self):
        self.assertTrue(SD_RECALL._is_virtual_path("dimcode://sess_x"))
        self.assertTrue(SD_RECALL._is_virtual_path("zcode://sess_x"))
        self.assertFalse(SD_RECALL._is_virtual_path("/tmp/x.jsonl"))


# ---------------------------------------------------------------------------
# Crashes and silent zeros
# ---------------------------------------------------------------------------

class CoercionAndCrashTest(unittest.TestCase):
    """Third-party JSONL is untrusted input; every counter must survive it."""

    def test_null_usage_does_not_raise(self):
        from echolib._claude import session_stats
        recs = [
            {"type": "user", "timestamp": "2026-01-01T10:00:00Z",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"model": "m",
                         "usage": {"input_tokens": None, "output_tokens": None,
                                   "cache_read_input_tokens": None,
                                   "cache_creation_input_tokens": None},
                         "content": [{"type": "text", "text": "ok"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T10:00:09Z",
             "message": {"model": "m",
                         "usage": {"input_tokens": "500", "output_tokens": 100,
                                   "cache_read_input_tokens": 400,
                                   "cache_creation_input_tokens": 0},
                         "content": [{"type": "text", "text": "ok2"}]}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False, encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
            path = f.name
        try:
            st = session_stats(path)
        finally:
            os.unlink(path)
        self.assertEqual(st["input_tokens"], 500)
        self.assertEqual(st["cache_read_tokens"], 400)
        self.assertIsNotNone(st["cache_hit_rate"])

    def test_ts19_tolerates_none(self):
        self.assertEqual(SD_RECALL._ts19(None), "?")
        self.assertEqual(SD_RECALL._ts19(""), "?")
        self.assertEqual(SD_RECALL._ts19("2026-01-01T10:00:00Z"), "2026-01-01T10:00:00")
        self.assertEqual(SD_RECALL._ts19(12345), "?")

    def test_lite_decisions_path_routes_through_ts19(self):
        src = (SCRIPTS / "sd-recall.py").read_text(encoding="utf-8")
        self.assertNotIn('rec.get("timestamp", "")[:19]', src)

    def test_iso_timestamps_do_not_collapse_to_zero(self):
        from echolib._claude_index import _iso_to_epoch
        self.assertAlmostEqual(_iso_to_epoch("2026-01-01T10:00:00Z"), 1767261600.0, delta=2)
        self.assertAlmostEqual(_iso_to_epoch("2026-01-01T10:00:00"), 1767261600.0, delta=2)
        self.assertEqual(_iso_to_epoch(""), 0.0)
        self.assertEqual(_iso_to_epoch("not-a-time"), 0.0)
        self.assertEqual(_iso_to_epoch(1767261600), 1767261600.0)

    def test_mixed_timezone_durations_are_computed(self):
        from index_builder._session_analysis import _build_rich_stats
        # Both directions, because the normalisation is symmetric and a test
        # that only covers one of them passes with half the fix removed:
        # normalising the naive *end* is enough to make an aware *start*
        # subtractable, so the "…Z start, bare end" pair alone cannot detect a
        # missing start-side normalisation.
        cases = [
            # (started, ended, expected)
            ("2026-01-01T10:00:00Z", "2026-01-01T10:05:00", 300.0),
            ("2026-01-01T10:00:00", "2026-01-01T10:05:00Z", 300.0),
            ("2026-01-01T10:00:00Z", "2026-01-01T10:05:00Z", 300.0),
            ("2026-01-01T10:00:00", "2026-01-01T10:05:00", 300.0),
        ]
        for started, ended, expected in cases:
            stats = {"user_messages": 1, "assistant_messages": 1,
                     "started": started, "ended": ended}
            rich = _build_rich_stats({}, {}, stats, "x")
            self.assertIsNotNone(rich.get("duration_seconds"),
                                 f"{started!r} … {ended!r} produced no duration")
            self.assertAlmostEqual(rich["duration_seconds"], expected, delta=1,
                                   msg=f"{started!r} … {ended!r}")

    def test_grok_as_int_keeps_zero_distinct_from_absent(self):
        from echolib._adapters_grok import _grok_as_int
        self.assertEqual(_grok_as_int(0, None), 0)
        self.assertEqual(_grok_as_int("0", None), 0)
        self.assertEqual(_grok_as_int(None, None), None)
        self.assertEqual(_grok_as_int("abc", None), None)
        self.assertEqual(_grok_as_int("1e3"), 1000)
        self.assertEqual(_grok_as_int(True), 1)
        # The hazard _as_int exists for: int(float('inf')) raises OverflowError,
        # which the old `except (TypeError, ValueError)` did not catch.
        self.assertEqual(_grok_as_int(float("inf")), 0)
        self.assertEqual(_grok_as_int(float("nan")), 0)

    def test_file_hash_distinguishes_unreadable_from_a_real_hash(self):
        from retrieval_utils import _file_hash, collapse_near_dups
        self.assertIsNone(_file_hash("/nonexistent/path/xyz"))
        # Two unreadable candidates must NOT fold into one.
        items = [("a", "/nonexistent/a"), ("b", "/nonexistent/b")]
        kept, collapsed = collapse_near_dups(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(collapsed, 0)


# ---------------------------------------------------------------------------
# Sentinels and defaults that misattribute
# ---------------------------------------------------------------------------

class SentinelTest(unittest.TestCase):
    """A default that names a concrete environment absorbs every unknown one."""

    def test_unknown_agent_does_not_become_claude(self):
        src = (SCRIPTS / "sd-recall.py").read_text(encoding="utf-8")
        self.assertIn('entry = (sid, path, agent or "unknown")', src)
        self.assertNotIn('entry = (sid, path, agent or "claude")', src)

    def test_evidence_from_index_asks_before_rendering(self):
        """The third entry point asked no one, so a fixed bug was one caller away.

        ``projection_available`` exists because ``''`` ("never computed") and
        ``'[]'`` ("computed, no user turns") are not interchangeable — on the
        live index 183 rows were in that state and ``--decisions`` returned the
        same output as a plain search.
        """
        src = (SCRIPTS / "index_builder" / "_reader.py").read_text(encoding="utf-8")
        i = src.find("def evidence_from_index")
        self.assertNotEqual(i, -1)
        body = src[i:i + 1600]
        self.assertIn("projection_available(row)", body)

    def test_empty_result_report_names_both_axes(self):
        src = (SCRIPTS / "sd-recall.py").read_text(encoding="utf-8")
        self.assertIn('print("  Widen: rerun with --agent cross")', src)
        self.assertIn("report_empty_search(args.keyword, args.scope, args.agent)", src)

    def test_sidecar_predicate_has_one_home(self):
        """Five call sites re-derived ``".jsonl." in name``.

        Mutation caught: reintroducing the literal at any call site — the
        predicate is what keeps ``<id>.jsonl.summary.jsonl`` out of listings.
        """
        offenders = []
        for path in sorted(SCRIPTS.rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            if path.name == "_helpers.py":
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if '".jsonl." in' in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}")
        self.assertEqual(offenders, [], "these re-derive is_sidecar_jsonl()")
        self.assertTrue(is_sidecar_jsonl("abc.jsonl.summary.jsonl"))
        self.assertFalse(is_sidecar_jsonl("abc.jsonl"))


# ---------------------------------------------------------------------------
# The policy table is the source of truth, not a literal at the call site
# ---------------------------------------------------------------------------

class TokenPolicyTest(unittest.TestCase):
    """``input_includes_cache`` was hardcoded where the policy says otherwise.

    ``PROVIDER_POLICY`` declares ``zcode_v2: input_includes_cache=True``; both
    the adapter path and the single-pass path passed ``False``. For an
    environment whose ``input_tokens`` already contains the cached leg, that
    computes ``cache_read / (input + cache_read)`` and understates every hit
    rate by roughly half.
    """

    def test_single_pass_resolves_the_flag_from_the_policy(self):
        src = (SCRIPTS / "index_builder" / "_session_analysis.py").read_text(
            encoding="utf-8")
        self.assertIn("echolib.attach_cache_hit_rates(stats, agent=agent)", src)
        self.assertNotIn("attach_cache_hit_rates(stats, input_includes_cache=False)", src)

    def test_policy_is_actually_consulted_per_agent(self):
        from index_builder._session_analysis import _single_pass_analyze
        recs = [
            {"type": "user", "timestamp": "2026-01-01T10:00:00Z",
             "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "timestamp": "2026-01-01T10:00:05Z",
             "message": {"model": "m",
                         "usage": {"input_tokens": 100, "output_tokens": 50,
                                   "cache_read_input_tokens": 900,
                                   "cache_creation_input_tokens": 0},
                         "content": [{"type": "text", "text": "ok"}]}},
        ]
        rates = {}
        for agent in ("claude", "zcode_v2"):
            with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                             encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
                path = f.name
            try:
                st, _t, _m, _i = _single_pass_analyze(path, agent)
            finally:
                os.unlink(path)
            rates[agent] = st["cache_hit_rate"]
        # claude: non-cached leg -> 900/(100+900)
        self.assertAlmostEqual(rates["claude"], 0.9, places=3)
        # zcode_v2: total input -> 900/100, clamped to 1.0
        self.assertAlmostEqual(rates["zcode_v2"], 1.0, places=3)
        self.assertNotEqual(rates["claude"], rates["zcode_v2"])


# ---------------------------------------------------------------------------
# Index shape
# ---------------------------------------------------------------------------

class IndexShapeTest(unittest.TestCase):
    """``sessions`` had no secondary index; three everyday queries scanned it."""

    def test_the_two_indexes_are_declared(self):
        src = (SCRIPTS / "index_builder" / "_schema.py").read_text(encoding="utf-8")
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_sessions_agent", src)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_sessions_jsonl_path", src)

    def test_indexes_are_used_for_the_queries_that_need_them(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "index.db"
            conn = sqlite3.connect(db)
            from index_builder._schema import init_db
            init_db(conn)
            plans = {
                "agent": conn.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM sessions WHERE agent = 'x'"
                ).fetchall(),
                "path": conn.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM sessions"
                    " WHERE jsonl_path IN ('a','b')"
                ).fetchall(),
            }
            conn.close()
        for label, plan in plans.items():
            text = " ".join(str(r[-1]) for r in plan)
            self.assertNotIn("SCAN sessions", text,
                             f"{label} still full-scans sessions")

    def test_backfill_early_returns_when_nothing_needs_a_role(self):
        """Seven full scans became one probe.

        The docstring promised a pure no-op after the first backfill, but
        SQLite cannot skip a zero-row UPDATE, so every build paid for all seven
        statements to discover there was nothing to do.
        """
        src = (SCRIPTS / "index_builder" / "_builder.py").read_text(encoding="utf-8")
        self.assertIn("SELECT 1 FROM sessions WHERE {needs_role} LIMIT 1", src)


# ---------------------------------------------------------------------------
# Version declarations
# ---------------------------------------------------------------------------

class VersionDeclarationTest(unittest.TestCase):
    """Six declarations, two of which nothing compared.

    ``herdr-plugin.toml``'s *description* said v0.9.6 while its ``version =``
    line said 0.9.29 — the one field a human reads was the one that drifted, for
    21 releases. The README footer sat at 0.9.19 for eight. Both are published
    text, so both are declarations.
    """

    def test_gate_covers_the_description_and_the_footer(self):
        src = (REPO_ROOT / "tests" / "test_manifest_versions.py").read_text(
            encoding="utf-8")
        self.assertIn("README.md footer", src)
        self.assertIn("herdr-plugin.toml description", src)
        self.assertIn("_subskill_version_pairs", src)

    def test_declarations_are_actually_current(self):
        canonical = re.search(r"^version:\s*(\S+)\s*$",
                              (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8"),
                              flags=re.MULTILINE).group(1)
        herdr = (REPO_ROOT / "herdr-plugin.toml").read_text(encoding="utf-8")
        self.assertIn(f"v{canonical}", herdr)
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"session-digger v{canonical}", readme)


# ---------------------------------------------------------------------------
# The test suite itself
# ---------------------------------------------------------------------------

class SuiteHygieneTest(unittest.TestCase):
    """A test file pytest never collects is not a test.

    ``test-improvements.py`` used a hyphen, so the default ``python_files``
    pattern never matched it, and its functions ``return``ed instead of
    asserting — so even if collected, ``return False`` would have passed with a
    warning. Six checks had been silently dead for the whole release line.
    """

    def test_improvements_file_is_collectable(self):
        self.assertTrue((REPO_ROOT / "tests" / "test_improvements.py").is_file())
        self.assertFalse((REPO_ROOT / "tests" / "test-improvements.py").exists())

    def test_improvements_uses_assertions_not_returns(self):
        src = (REPO_ROOT / "tests" / "test_improvements.py").read_text(encoding="utf-8")
        self.assertNotIn("return False", src)
        self.assertNotIn("return True", src)

    def test_local_state_checks_skip_rather_than_fail(self):
        """``~/.codex/AGENTS.md`` is machine state, not a plugin property."""
        src = (REPO_ROOT / "tests" / "test_improvements.py").read_text(encoding="utf-8")
        self.assertIn("SD_CHECK_CODEX_AGENTS_MD", src)
        self.assertIn("unittest.SkipTest", src)


if __name__ == "__main__":
    unittest.main()
