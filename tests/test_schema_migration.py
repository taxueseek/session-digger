"""Tests for index_builder/_schema.py migration ledger.

Before: ALTER TABLE ADD COLUMN on every init_db() run (harmless today because
the ADD COLUMN is skipped when the column exists, but no version tracking).

After: the migration version is persisted in index_meta['schema_version'].
When it matches the code-declared version, no ALTER runs at all.
"""
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_SCHEMA = Path(__file__).resolve().parent.parent / "scripts" / "index_builder" / "_schema.py"
_spec = importlib.util.spec_from_file_location("_schema", str(_SCHEMA))
schema = importlib.util.module_from_spec(_spec)
sys.modules["_schema"] = schema
_spec.loader.exec_module(schema)


class _TempDB:
    """Context manager that yields a sqlite3 connection to a temp file."""

    def __init__(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "test.db"
        self.conn = None

    def __enter__(self):
        self.conn = sqlite3.connect(str(self.path))
        return self.conn

    def __exit__(self, *a):
        if self.conn:
            self.conn.close()


class TestSchemaMigration(unittest.TestCase):
    def test_fresh_db_sets_version_and_creates_columns(self):
        with _TempDB() as conn:
            schema.init_db(conn)
            self.assertEqual(schema._schema_version(conn), schema._SCHEMA_VERSION)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            for expected in ("tool_usage_json", "tool_errors_json", "flags_json",
                             "duration_seconds", "project_name", "tags",
                             "outcome", "model", "jsonl_path", "content_hash"):
                self.assertIn(expected, cols, f"missing column: {expected}")

    def test_second_init_is_idempotent(self):
        with _TempDB() as conn:
            schema.init_db(conn)
            before = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            # run again — must NOT raise, must NOT change the table
            schema.init_db(conn)
            after = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            self.assertEqual(before, after)
            self.assertEqual(schema._schema_version(conn), schema._SCHEMA_VERSION)

    def test_old_db_without_version_gets_promoted(self):
        """Pre-migration DB: has base sessions table but no RICH columns and
        no schema_version (the state of DBs that ran old init_db()).
        After init_db(): RICH + version applied in one shot."""
        with _TempDB() as conn:
            conn.execute(schema.SCHEMA_SESSIONS)
            conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                session_id UNINDEXED, role UNINDEXED, timestamp UNINDEXED,
                text, tokenize='{schema.FTS_TOKENIZER}'
            );""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS topic_boundaries (
                session_id TEXT, message_index INTEGER, timestamp TEXT,
                topic_label TEXT, confidence REAL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );""")
            conn.execute("CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()

            self.assertEqual(schema._schema_version(conn), "")

            schema.init_db(conn)

            self.assertEqual(schema._schema_version(conn), schema._SCHEMA_VERSION)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            for expected in ("tool_usage_json", "content_hash", "model"):
                self.assertIn(expected, cols, f"missing: {expected}")


if __name__ == "__main__":
    unittest.main()
