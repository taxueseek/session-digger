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
from cjk import split_cjk, build_match_query


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
  text_cjk TEXT,
  source TEXT
);

-- 倒排喂 text_cjk（CJK 逐字切分版），显示读 messages.text（原文）。
-- unicode61 下连续中文是一个 token，不切分则子串查询全漏（"永安" 查不到 "永安城管"）。
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text_cjk,
  nickname,
  chat_name,
  content='messages',
  content_rowid='rowid'
);

"""

# 触发器独立于 SCHEMA：rebuild 全量灌数时先不装，'rebuild' 命令重建倒排后再挂，
# 避免 177 万行逐行触发写倒排再被整体作废（写放大 2 倍）。
TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text_cjk, nickname, chat_name)
  VALUES (new.rowid, new.text_cjk, new.nickname, new.chat_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text_cjk, nickname, chat_name)
  VALUES ('delete', old.rowid, old.text_cjk, old.nickname, old.chat_name);
END;
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path or default_index_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(TRIGGERS_SQL)
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
                   (id, chat_id, chat_name, sender, nickname, ts, msg_type, text, text_cjk, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    m.get("id") or f"{chat_id}:{m.get('ts')}:{hash(m.get('text') or '') & 0xFFFFFFFF:x}",
                    chat_id,
                    chat_name,
                    m.get("sender"),
                    m.get("nickname"),
                    m.get("ts"),
                    m.get("msg_type"),
                    m.get("text"),
                    split_cjk(m.get("text") or ""),
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
    # CJK 感知 MATCH：中文逐字 phrase（子串语义），ASCII 前缀星；
    # FTS5 操作符/标点被丢弃，顺带关闭注入洞。None → 直接走 LIKE。
    fts_q = build_match_query(q)
    rows: Optional[list] = None
    if fts_q is not None:
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
            rows = None  # 旧库无 text_cjk 列或 fts 结构旧 → LIKE 自愈
    if rows is None:
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


# ── 全史索引：从微信自带 message_fts 库一次性重建 + rowid 上界增量同步 ──

def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def fts_rowid_bounds() -> Optional[dict[str, int]]:
    """message_fts 各分片表当前 max(rowid)。rowid B 树右端读，单表 0.3ms。

    微信 fts content 表按插入序写 rowid，新消息 rowid 严格更大——这是
    新鲜度判定与增量同步的根依据；发现非单调再回来换方案。
    """
    try:
        from fts_engine import _FTS_TABLES, _connect_ro, fts_db_path
    except Exception:
        return None
    path = fts_db_path()
    if not path:
        return None
    bounds: dict[str, int] = {}
    try:
        con = _connect_ro(path)
        try:
            for t in _FTS_TABLES:
                mx = con.execute(f"SELECT max(rowid) FROM {t}_content").fetchone()[0]
                # 空分片表合法（新装/新建库），max=NULL 记 0 而不是整个判定不可达
                bounds[t] = int(mx) if mx is not None else 0
        finally:
            con.close()
    except Exception:
        return None
    return bounds


def _fts_rows_above(lower_bounds: dict[str, int], batch: int = 20000):
    """逐表流式产出 rowid 超过 lower_bounds 的 (table, rowid, text, sid, sender_id, ts)。"""
    from fts_engine import _FTS_TABLES, _connect_ro, fts_db_path
    path = fts_db_path()
    if not path:
        return
    con = _connect_ro(path)
    try:
        for t in _FTS_TABLES:
            last = lower_bounds.get(t, 0)
            while True:
                rows = con.execute(
                    f"SELECT rowid, c0, c4, c5, c6 FROM {t}_content WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (last, batch),
                ).fetchall()
                if not rows:
                    break
                for rowid, c0, c4, c5, c6 in rows:
                    yield t, int(rowid), c0 or "", int(c4), c5, int(c6 or 0)
                    last = int(rowid)
                if len(rows) < batch:
                    break
    finally:
        con.close()


def _fts_namebook():
    from fts_engine import fts_db_path, _NameBook
    path = fts_db_path()
    return _NameBook(path.parent.parent) if path else None


def _sync_incremental(conn: sqlite3.Connection, rec: dict[str, int], cur: dict[str, int]) -> dict:
    """把 rowid 超过已记录上界的新消息增量灌入索引，更新 meta 上界。"""
    book = _fts_namebook()
    synced = 0
    chats_touched = 0
    batch: list[tuple] = []
    chats: dict[str, dict] = {}
    # 流式分批：rec 丢失（崩溃残留）时 rec={} 会重放全史，无上限会 OOM
    def _flush():
        nonlocal synced, chats_touched
        if not batch:
            return
        conn.executemany(
            """INSERT OR REPLACE INTO messages
               (id, chat_id, chat_name, sender, nickname, ts, msg_type, text, text_cjk, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""", batch)
        for cid, c in chats.items():
            conn.execute(
                """INSERT INTO chats(chat_id, chat_name, source, message_count, last_ts, content_hash, updated_at)
                   VALUES (?,?,?,?,?,NULL,datetime('now'))
                   ON CONFLICT(chat_id) DO UPDATE SET
                     message_count=message_count+excluded.message_count,
                     last_ts=max(coalesce(chats.last_ts,0),excluded.last_ts),
                     updated_at=datetime('now')""",
                (cid, c["name"], "fts", c["n"], c["last"]))
        conn.commit()
        synced += len(batch)
        chats_touched += len(chats)
        batch.clear()
        chats.clear()
    for t, rowid, text, sid, sender_id, ts in _fts_rows_above(rec):
        uname = display = sender = None
        if book is not None:
            uname, display = book.chat(sid)
            sender = book.sender(sender_id)
        cid = uname or f"session:{sid}"
        batch.append((f"fts:{t}:{rowid}", cid, display or "", sender or "", "", ts,
                      "text", text, split_cjk(text), "fts"))
        c = chats.setdefault(cid, {"name": display or cid, "n": 0, "last": 0})
        c["n"] += 1
        c["last"] = max(c["last"], ts)
        if len(batch) >= 20000:
            _flush()
    _flush()
    _meta_set(conn, "fts_rowid_bounds", json.dumps(cur))
    conn.commit()
    return {"synced": synced, "chats_touched": chats_touched}


def rebuild_from_fts(conn: sqlite3.Connection, batch: int = 20000, progress=None) -> dict:
    """清空并从微信自带 message_fts 库重建全史 FTS 索引（2022-02→今）。

    触发器先卸后挂、倒排用 fts5 'rebuild' 命令走 C 路径，避免逐行触发器
    开销；text_cjk 是 split_cjk 后的切分版（倒排用），text 保留原文（显示用）。
    完成后在 meta 记录各分片表 rowid 上界，作为后续增量同步与新鲜度判定基准。
    """
    import time as _time
    t0 = _time.perf_counter()
    cur = fts_rowid_bounds()
    if cur is None:
        return {"ok": False, "error": "message_fts 库不可达（fts_db_path 为空或库读不了）"}

    book = _fts_namebook()
    # 重建窗口闸门：先把 full_history 标记清零并落盘。DROP 到灌数完成的
    # ~2 分钟里并发 search 会看到空索引——没有这道闸，空结果会被当作
    # 真实「零命中」返回（实测），且崩溃后标记残留会让增量往空索引里灌。
    conn.execute("DELETE FROM meta WHERE key='full_history'")
    conn.commit()
    # 旧结构（无 text_cjk 列的 messages / 旧列名 fts 表）直接换新，这是唯一 schema 迁移点
    conn.executescript("""
      DROP TRIGGER IF EXISTS messages_ai;
      DROP TRIGGER IF EXISTS messages_ad;
      DROP TABLE IF EXISTS messages_fts;
      DROP TABLE IF EXISTS messages;
      DROP TABLE IF EXISTS chats;
    """)
    conn.executescript(SCHEMA)  # 无触发器版（TRIGGERS_SQL 在 'rebuild' 后挂）

    total = 0
    chats: dict[str, dict] = {}
    batch_rows: list[tuple] = []
    for t, rowid, text, sid, sender_id, ts in _fts_rows_above({t: 0 for t in cur}):
        uname = display = sender = None
        if book is not None:
            uname, display = book.chat(sid)
            sender = book.sender(sender_id)
        cid = uname or f"session:{sid}"
        batch_rows.append((f"fts:{t}:{rowid}", cid, display or "", sender or "", "", ts,
                           "text", text, split_cjk(text), "fts"))
        c = chats.setdefault(cid, {"name": display or cid, "n": 0, "last": 0})
        c["n"] += 1
        c["last"] = max(c["last"], ts)
        if len(batch_rows) >= batch:
            conn.executemany(
                """INSERT INTO messages
                   (id, chat_id, chat_name, sender, nickname, ts, msg_type, text, text_cjk, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", batch_rows)
            total += len(batch_rows)
            batch_rows = []
            if progress:
                progress(total)
    if batch_rows:
        conn.executemany(
            """INSERT INTO messages
               (id, chat_id, chat_name, sender, nickname, ts, msg_type, text, text_cjk, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)""", batch_rows)
        total += len(batch_rows)

    conn.executemany(
        """INSERT OR REPLACE INTO chats(chat_id, chat_name, source, message_count, last_ts, content_hash, updated_at)
           VALUES (?,?,?,?,?,NULL,datetime('now'))""",
        [(cid, c["name"], "fts", c["n"], c["last"]) for cid, c in chats.items()])
    # 倒排整体重建走 C 路径（比逐行触发器快一个量级）
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    conn.executescript(TRIGGERS_SQL)
    _meta_set(conn, "full_history", "1")
    _meta_set(conn, "fts_rowid_bounds", json.dumps(cur))
    _meta_set(conn, "built_at", str(int(_time.time())))
    conn.commit()
    return {"ok": True, "indexed": total, "chats": len(chats),
            "elapsed_s": round(_time.perf_counter() - t0, 1)}


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
