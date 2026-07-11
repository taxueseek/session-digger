"""SQL schema and database initialisation for the session-digger index."""
import sqlite3
import json
from pathlib import Path

DB_DIR = Path.home() / ".claude" / ".session-digger"
DB_PATH = DB_DIR / "index.db"
FTS_TOKENIZER = "unicode61"

SCHEMA_SESSIONS = """CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_path TEXT,
    agent TEXT,
    created TEXT,
    modified TEXT,
    message_count INTEGER,
    user_messages INTEGER,
    assistant_messages INTEGER,
    tool_calls INTEGER,
    errors INTEGER,
    compactions INTEGER,
    total_tokens INTEGER,
    branch TEXT,
    summary TEXT,
    first_prompt TEXT,
    jsonl_mtime REAL,
    indexed_at REAL,
    jsonl_path TEXT
);"""

RICH_COLUMNS = {
    "tool_usage_json": "TEXT DEFAULT '{}'",
    "tool_errors_json": "TEXT DEFAULT '{}'",
    "flags_json": "TEXT DEFAULT '[]'",
    "duration_seconds": "REAL",
    "project_name": "TEXT",
    "tags": "TEXT DEFAULT '[]'",
    "outcome": "TEXT",
    # Preferred model name for the session (best-effort across agents)
    "model": "TEXT DEFAULT ''",
}

QUERY_COLUMNS = [
    "id", "agent", "created", "user_messages", "assistant_messages",
    "tool_calls", "errors", "project_name", "summary",
    "tool_usage_json", "flags_json", "duration_seconds",
]

# ── Schema migration ledger ─────────────────────────────────────────────
# Each entry is applied in order, idempotent via the schema_version row in
# index_meta. Adding a new column = append a new entry (never edit in-place).
_SCHEMA_VERSION = "1"

_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    # version → [(column_name, column_type_sql), ...]
    "1": [*RICH_COLUMNS.items(), ("jsonl_path", "TEXT"), ("content_hash", "TEXT")],
}


def _schema_version(conn) -> str:
    """Read schema_version from index_meta (returns "" if not yet set)."""
    row = conn.execute(
        "SELECT value FROM index_meta WHERE key = 'schema_version'"
    ).fetchone()
    return row[0] if row else ""


def _apply_schema_version(conn, version: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('schema_version', ?)",
        (version,),
    )


def init_db(conn):
    """Create tables if missing, and migrate older schemas."""
    conn.execute(SCHEMA_SESSIONS)

    conn.execute(f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        session_id UNINDEXED,
        role UNINDEXED,
        timestamp UNINDEXED,
        text,
        tokenize='{FTS_TOKENIZER}'
    );""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS topic_boundaries (
        session_id TEXT,
        message_index INTEGER,
        timestamp TEXT,
        topic_label TEXT,
        confidence REAL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    );""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS index_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );""")

    # Apply pending schema migrations (idempotent via tracking table).
    # Runs only when stored version differs from the code-declared version.
    # This also promotes any pre-migration DB (version unset but some columns
    # present) to the canonical version after applying any missing columns.
    if _schema_version(conn) != _SCHEMA_VERSION:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        for col_name, col_type in _MIGRATIONS[_SCHEMA_VERSION]:
            if col_name not in cols:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_type}")
        _apply_schema_version(conn, _SCHEMA_VERSION)

    conn.commit()
