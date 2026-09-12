#!/usr/bin/env python3
"""
SQLite FTS5 index for chat messages.

对齐 session-digger speed tier：
1. 先 /index 建索引
2. 后续搜索走 FTS，避免每次全量扫库
3. mtime/content hash 增量：未变化 chat 跳过
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

from paths import default_index_path


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
  chat_id TEXT PRIMARY KEY,
  chat_name TEXT,
  source TEXT,
  message_count INTEGER DEFAULT 0,
  last_ts INTEGER,
  content_hash TEXT,
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  chat_id TEXT,
  chat_name TEXT,
  sender TEXT,
  nickname TEXT,
  ts INTEGER,
  msg_type TEXT,
  text TEXT,
  source TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text,
  nickname,
  chat_name,
  content='messages',
  content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text, nickname, chat_name)
  VALUES (new.rowid, new.text, new.nickname, new.chat_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, nickname, chat_name)
  VALUES ('delete', old.rowid, old.text, old.nickname, old.chat_name);
END;
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path or default_index_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def content_hash(messages: Iterable[dict]) -> str:
    h = hashlib.sha256()
    for m in messages:
        h.update(f"{m.get('id')}|{m.get('ts')}|{m.get('text')}\n".encode("utf-8", errors="ignore"))
    return h.hexdigest()[:32]


def upsert_messages(conn: sqlite3.Connection, messages: list[dict], source: str = "unknown") -> dict:
    if not messages:
        return {"upserted": 0, "chats": 0}

    by_chat: dict[str, list[dict]] = {}
    for m in messages:
        cid = m.get("chat_id") or m.get("chat_name") or "unknown"
        by_chat.setdefault(cid, []).append(m)

    upserted = 0
    for chat_id, msgs in by_chat.items():
        ch = content_hash(msgs)
        row = conn.execute("SELECT content_hash FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if row and row["content_hash"] == ch:
            continue  # incremental skip
        # replace chat messages
        conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        chat_name = msgs[0].get("chat_name") or chat_id
        for m in msgs:
            conn.execute(
                """INSERT OR REPLACE INTO messages
                   (id, chat_id, chat_name, sender, nickname, ts, msg_type, text, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    m.get("id") or f"{chat_id}:{m.get('ts')}:{hash(m.get('text') or '') & 0xFFFFFFFF:x}",
                    chat_id,
                    chat_name,
                    m.get("sender"),
                    m.get("nickname"),
                    m.get("ts"),
                    m.get("msg_type"),
                    m.get("text"),
                    m.get("source") or source,
                ),
            )
            upserted += 1
        last_ts = max((m.get("ts") or 0) for m in msgs)
        conn.execute(
            """INSERT INTO chats(chat_id, chat_name, source, message_count, last_ts, content_hash, updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(chat_id) DO UPDATE SET
                 chat_name=excluded.chat_name,
                 source=excluded.source,
                 message_count=excluded.message_count,
                 last_ts=excluded.last_ts,
                 content_hash=excluded.content_hash,
                 updated_at=datetime('now')""",
            (chat_id, chat_name, source, len(msgs), last_ts, ch),
        )
    conn.commit()
    return {"upserted": upserted, "chats": len(by_chat)}


def search(conn: sqlite3.Connection, query: str, chat: Optional[str] = None, limit: int = 50) -> list[dict]:
    q = query.strip()
    if not q:
        return []
    # FTS: quote multi-token loosely
    fts_q = " ".join(f'"{t}"' if " " in t else t for t in q.split())
    sql = """
      SELECT m.id, m.chat_id, m.chat_name, m.nickname, m.ts, m.text, m.source
      FROM messages_fts f
      JOIN messages m ON m.rowid = f.rowid
      WHERE messages_fts MATCH ?
    """
    params: list[Any] = [fts_q]
    if chat:
        sql += " AND (m.chat_name LIKE ? OR m.chat_id LIKE ?)"
        params.extend([f"%{chat}%", f"%{chat}%"])
    sql += " ORDER BY m.ts DESC LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # fallback LIKE
        like = f"%{q}%"
        if chat:
            rows = conn.execute(
                """SELECT id, chat_id, chat_name, nickname, ts, text, source FROM messages
                   WHERE text LIKE ? AND (chat_name LIKE ? OR chat_id LIKE ?)
                   ORDER BY ts DESC LIMIT ?""",
                (like, f"%{chat}%", f"%{chat}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, chat_id, chat_name, nickname, ts, text, source FROM messages
                   WHERE text LIKE ? ORDER BY ts DESC LIMIT ?""",
                (like, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    chats = conn.execute("SELECT COUNT(*) AS c FROM chats").fetchone()["c"]
    msgs = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
    return {"chats": chats, "messages": msgs, "db": str(default_index_path())}


def main():
    p = argparse.ArgumentParser(description="wechat-digger index")
    sub = p.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="build/update index from messages JSON")
    b.add_argument("--input", required=True)
    b.add_argument("--db", default=None)
    b.add_argument("--source", default="unknown")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--chat", default=None)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--db", default=None)
    st = sub.add_parser("stats")
    st.add_argument("--db", default=None)
    args = p.parse_args()

    if args.cmd == "build":
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        if isinstance(data, dict) and "messages" in data:
            data = data["messages"]
        conn = connect(Path(args.db) if args.db else None)
        result = upsert_messages(conn, data, source=args.source)
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "search":
        conn = connect(Path(args.db) if args.db else None)
        print(json.dumps(search(conn, args.query, chat=args.chat, limit=args.limit), ensure_ascii=False, indent=2))
    elif args.cmd == "stats":
        conn = connect(Path(args.db) if args.db else None)
        print(json.dumps(stats(conn), ensure_ascii=False, indent=2))
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
