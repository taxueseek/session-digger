"""SQL schema and database initialisation for the session-digger index."""
import os
import sqlite3
import json
from pathlib import Path

# 单一真源：所有读写 index.db 的工具必须从这里导入 DB_DIR / DB_PATH。
# 用户可通过 SESSION_DIGGER_DATA_DIR 覆盖（dotfiles 同步 / 多机 / 换盘）。
DB_DIR = Path(os.environ.get("SESSION_DIGGER_DATA_DIR",
                             str(Path.home() / ".claude" / ".session-digger")))
DB_PATH = DB_DIR / "index.db"
FTS_TOKENIZER = "unicode61"


__all__ = ["DB_DIR", "DB_PATH", "FTS_TOKENIZER", "SCHEMA_SESSIONS",
           "RICH_COLUMNS", "QUERY_COLUMNS", "ensure_schema", "connect"]

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
    # Cache hit rate: cache_read / (input + cache_read) — computed at index time
    "cache_hit_rate": "REAL",
}

QUERY_COLUMNS = [
    "id", "agent", "created", "user_messages", "assistant_messages",
    "tool_calls", "errors", "project_name", "summary",
    "tool_usage_json", "flags_json", "duration_seconds",
]

# ── Schema migration ledger ─────────────────────────────────────────────
# Each entry is applied in order, idempotent via the schema_version row in
# index_meta. Adding a new column = append a new entry (never edit in-place).
_SCHEMA_VERSION = "3"

_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    # version → [(column_name, column_type_sql), ...]
    "1": [*RICH_COLUMNS.items(), ("jsonl_path", "TEXT"), ("content_hash", "TEXT")],
    "2": [("cache_hit_rate", "REAL")],
    # main conversation vs subagent/child agent (for split cache rankings)
    "3": [("session_role", "TEXT DEFAULT 'unknown'")],
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
    stored = _schema_version(conn)
    if stored != _SCHEMA_VERSION:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        # 增量升级：从 stored 版本之后的每个版本顺序应用，每版本是 delta。
        # 这样未来 v1→v2→v3 的多跳升级不会重复 ADD 已存在的列，也不会遗漏中间版本。
        pending_versions = sorted(v for v in _MIGRATIONS if v > stored)
        for ver in pending_versions:
            for col_name, col_type in _MIGRATIONS[ver]:
                if col_name not in cols:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_type}")
                cols.add(col_name)
        _apply_schema_version(conn, _SCHEMA_VERSION)

    conn.commit()
