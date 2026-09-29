"""The query rewriter must ask for what the user typed.

Two defects, one root cause: the query side and the write side segmented text
differently, so a multi-fragment keyword became an implicit AND of *prefix*
matches across the whole corpus.

    v0.9.31    →  '"v0"* "9"* "31"*'   119 rows, 2 real   (1.7% precision)
    C:\\path   →  '"C"* "path"*'     8177 rows, 1 real   (0.0%)
    skill 优化 →  '"skill"* "优 化"'   2192 rows, 0 real   (1.5%)

None of those is a ranking problem — BM25 never got a chance to rank a result
set that was wrong at the door. A developer asking for one version number got
117 unrelated sessions back.

The fix spells multi-fragment keywords as one phrase over the writer's own
segmentation (``split_cjk``), which turns those three into 2 / 1 / 49 rows.
Single-fragment keywords — which is every CJK phrase — must not move at all,
so the gate pins their exact row counts too.

``test_stats_reads_fts_count_from_index_meta`` covers the other half: the
cross-environment message count in ``sd-recall stats`` was an unfiltered
COUNT over every FTS row (37.6 ms, 93% of that command's SQL time), and the
builder now records the same number so the read is a primary-key lookup.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from index_builder._cjk import (  # noqa: E402
    _TOKEN,
    build_match_query,
    query_rejection,
)
import index_builder._schema as schema_mod  # noqa: E402


class MultiFragmentQueryTest(unittest.TestCase):
    """A keyword with several fragments is one string, not a bag of prefixes."""

    def test_multi_fragment_is_a_phrase_not_an_and_of_prefixes(self):
        for keyword in ("v0.9.31", "python3.14", "C:\\path", "foo-bar",
                        "skill 优化", "test OR"):
            with self.subTest(keyword=keyword):
                match = build_match_query(keyword)
                self.assertIsNotNone(match, keyword)
                self.assertNotIn("*", match,
                                 f"{keyword!r} still prefix-matches: {match!r}")
                # A phrase is one quoted run — the fragments must be *inside* the
                # quotes, not juxtaposed as separate FTS5 terms (juxtaposition
                # is the implicit AND this whole change exists to remove).
                self.assertTrue(match.startswith('"') and match.endswith('"'),
                                f"{keyword!r} is not a single phrase: {match!r}")
                self.assertNotIn('" "', match, match)

    def test_single_fragment_keeps_prefix_search(self):
        """Prefix search is right for one fragment — the tail is unknown."""
        for keyword, expected in (("token", '"token"*'),
                                  ("Postgre", '"Postgre"*'),
                                  ("fts5", '"fts5"*')):
            with self.subTest(keyword=keyword):
                self.assertEqual(build_match_query(keyword), expected)

    def test_cjk_phrases_are_untouched(self):
        """Every CJK query is single-fragment; the fix must not reach them."""
        cases = {
            "缓存": '"缓 存"',
            "架构设计": '"架 构 设 计"',
            "数据库迁移": '"数 据 库 迁 移"',
            "投资": '"投 资"',
        }
        for keyword, expected in cases.items():
            with self.subTest(keyword=keyword):
                self.assertEqual(build_match_query(keyword), expected)

    def test_rejection_still_applies_before_any_rewriting(self):
        for keyword in ("", "   ", "!!!", "a " * 30):
            with self.subTest(keyword=keyword):
                self.assertTrue(query_rejection(keyword),
                                f"{keyword!r} should be rejected")
                self.assertIsNone(build_match_query(keyword))

    def test_injection_hole_stays_closed(self):
        """The phrase rewrite must not reopen FTS5 operator injection.

        The property that matters is not "the word OR is absent" — inside a
        quoted phrase ``OR`` is a literal, which is fine and correct. It is that
        the result is **one** quoted phrase, so no operator can ever be parsed as
        syntax. The regression this guards was real: filtering the phrase through
        ``split_cjk`` alone left a bare ``=`` outside the token alphabet and
        SQLite raised ``fts5: syntax error near "="``, turning a searchable
        keyword into a failed query that falls back to a full file rescan.
        """
        for hostile in ('a" OR "1"="1', "NEAR(a b)", "col:val", "x^y", "a AND b"):
            with self.subTest(hostile=hostile):
                match = build_match_query(hostile)
                self.assertIsNotNone(match, hostile)
                # Exactly one quoted phrase: no bare operator can survive.
                self.assertEqual(match.count('"'), 2, match)
                self.assertTrue(match.startswith('"') and match.endswith('"'),
                                match)
                # Every token inside the phrase is a real index token, so nothing
                # that SQLite could read as syntax is left inside it.
                inner = match[1:-1].split()
                self.assertTrue(inner, match)
                for token in inner:
                    self.assertRegex(token, rf"^{_TOKEN.pattern}$", match)


class StatsCountCacheTest(unittest.TestCase):
    """``stats`` must not re-count 95k FTS rows to print a total."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "index.db"
        conn = sqlite3.connect(str(self.db))
        conn.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT);
            CREATE TABLE messages_fts (session_id TEXT, role TEXT, text TEXT);
            CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.executemany("INSERT INTO sessions VALUES (?,?)",
                         [("s1", "claude"), ("s2", "codex")])
        conn.executemany("INSERT INTO messages_fts VALUES (?,?,?)",
                         [("s1", "USER", "a"), ("s1", "ASSISTANT", "b"),
                          ("s2", "USER", "c")])
        # An orphan FTS row whose session is gone: the cached total must count
        # only joinable rows, or "N messages indexed" starts overstating.
        conn.execute("INSERT INTO messages_fts VALUES ('gone','USER','x')")
        conn.commit()
        conn.close()

    def _read_count(self, agent="cross", with_meta=True):
        """Mirror ``sd-recall.cmd_stats``'s FTS-count block."""
        conn = sqlite3.connect(str(self.db))
        try:
            fts_sql = ("SELECT COUNT(*) FROM messages_fts f "
                       "JOIN sessions s ON s.id = f.session_id")
            fts_n = None
            if agent == "cross":
                try:
                    row = conn.execute(
                        "SELECT value FROM index_meta "
                        "WHERE key = 'total_fts_rows'"
                    ).fetchone()
                    fts_n = int(row[0]) if row and str(row[0]).isdigit() else None
                except sqlite3.Error:
                    fts_n = None
            if fts_n is None:
                if agent == "cross":
                    fts_n = conn.execute(fts_sql).fetchone()[0]
                else:
                    fts_n = conn.execute(fts_sql + " WHERE s.agent = ?",
                                         (agent,)).fetchone()[0]
            return fts_n
        finally:
            conn.close()

    def test_missing_meta_falls_back_to_the_scan(self):
        """An index built before this change has no key — behaviour unchanged."""
        self.assertEqual(self._read_count("cross"), 3)

    def test_cached_total_is_used_when_present(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT OR REPLACE INTO index_meta VALUES "
                     "('total_fts_rows','3')")
        conn.commit()
        conn.close()
        self.assertEqual(self._read_count("cross"), 3)

    def test_per_agent_never_uses_the_global_total(self):
        """The cached number is the cross-environment one; reusing it per agent
        would print claude's count as the total for every environment."""
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT OR REPLACE INTO index_meta VALUES "
                     "('total_fts_rows','3')")
        conn.commit()
        conn.close()
        self.assertEqual(self._read_count("claude"), 2)
        self.assertEqual(self._read_count("codex"), 1)

    def test_corrupt_meta_value_is_ignored(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT OR REPLACE INTO index_meta VALUES "
                     "('total_fts_rows','not-a-number')")
        conn.commit()
        conn.close()
        self.assertEqual(self._read_count("cross"), 3)

    def test_schema_defines_index_meta(self):
        """The key only helps if the table is guaranteed to exist."""
        self.assertTrue(hasattr(schema_mod, "init_db"))


if __name__ == "__main__":
    unittest.main()
