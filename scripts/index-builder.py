#!/usr/bin/env python3
"""
index-builder.py — Pre-compute SQLite index for fast session querying.

Creates/updates ~/.claude/.session-digger/index.db with:
  sessions      — session metadata + stats (single row per .jsonl)
  messages_fts  — FTS5 full-text index over all user/assistant messages
  topic_boundaries  — auto-detected topic segment boundaries per session

Usage:
  index-builder.py [--project PATH] [--rebuild] [--agent claude|grok|kimi|cross]

v1.0: Architectural core for session-digger v0.7 speed tier.
     Eliminates per-command file scanning + repeated Python subprocess spawns.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import echolib

DB_DIR = Path.home() / ".claude" / ".session-digger"
DB_PATH = DB_DIR / "index.db"

# FTS stemmer: simple for cross-language (Chinese needs external tokenizer,
# but FTS5's "unicode61" handles CJK as individual characters = usable)
FTS_TOKENIZER = "unicode61"


def _dispatch_session_stats(path):
    """Get stats via adapter registry dispatch."""
    agent = echolib.detect_agent_type(path)
    fn = echolib.ADAPTER_REGISTRY.get(agent, {}).get("session_stats")
    if fn:
        return fn(path)
    return echolib.session_stats(path)


def _dispatch_extract_messages(path, role="both", limit=0):
    """Extract messages via adapter registry dispatch."""
    agent = echolib.detect_agent_type(path)
    fn = echolib.ADAPTER_REGISTRY.get(agent, {}).get("extract_messages")
    if fn:
        return fn(path, role=role, limit=limit, thinking_limit=0)
    return echolib.extract_messages(path, role=role, limit=limit)

def init_db(conn):
    """Create tables if missing."""
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS sessions (
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
        indexed_at REAL
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        session_id UNINDEXED,
        role UNINDEXED,
        timestamp UNINDEXED,
        text,
        tokenize='{FTS_TOKENIZER}'
    );

    CREATE TABLE IF NOT EXISTS topic_boundaries (
        session_id TEXT,
        message_index INTEGER,
        timestamp TEXT,
        topic_label TEXT,
        confidence REAL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    );

    CREATE TABLE IF NOT EXISTS index_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()


def scan_sessions(agent_filter="cross"):
    """Yield (session_id, jsonl_path, agent_type) tuples."""
    entries = []

    if agent_filter in ("claude", "cross"):
        claude_dir = Path.home() / ".claude" / "projects"
        if claude_dir.exists():
            for d in claude_dir.iterdir():
                if not d.is_dir():
                    continue
                for jf in d.glob("*.jsonl"):
                    if "subagents" in str(jf):
                        continue
                    if ".jsonl." in jf.name:
                        continue
                    entries.append((jf.stem, str(jf), "claude"))

    if agent_filter in ("grok", "cross"):
        grok_dir = Path.home() / ".grok" / "sessions"
        if grok_dir.exists():
            for d in grok_dir.iterdir():
                if not d.is_dir():
                    continue
                for session_dir in d.iterdir():
                    if not session_dir.is_dir():
                        continue
                    chat_file = session_dir / "chat_history.jsonl"
                    if chat_file.exists():
                        entries.append((session_dir.name, str(chat_file), "grok"))

    if agent_filter in ("kimi", "cross"):
        kimi_dir = Path.home() / ".kimi-code" / "sessions"
        if kimi_dir.exists():
            for project_dir in kimi_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for session_dir in project_dir.iterdir():
                    if not session_dir.is_dir():
                        continue
                    wire_file = session_dir / "agents" / "main" / "wire.jsonl"
                    if wire_file.exists():
                        sid = session_dir.name.replace("session_", "")
                        entries.append((sid, str(wire_file), "kimi"))

    return entries


def detect_topic_boundaries(messages, min_gap_seconds=300):
    """
    Heuristic topic segmentation based on:
    1. Time gaps > min_gap_seconds
    2. Role shifts (user→assistant→user)
    3. Keyword overlaps between adjacent pairs

    Returns list of (index, timestamp, label, confidence)
    """
    if len(messages) < 3:
        return []

    boundaries = []

    for i in range(1, len(messages)):
        msg = messages[i]
        prev = messages[i - 1]

        # Time gap heuristic
        ts_cur = msg.get("timestamp", "")
        ts_prev = prev.get("timestamp", "")
        gap_score = 0.0
        if ts_cur and ts_prev:
            try:
                from datetime import datetime
                fmt = "%Y-%m-%dT%H:%M:%S"
                t_cur = datetime.strptime(ts_cur[:19], fmt)
                t_prev = datetime.strptime(ts_prev[:19], fmt)
                gap = (t_cur - t_prev).total_seconds()
                if gap > min_gap_seconds:
                    gap_score = min(gap / 3600, 1.0)
            except (ValueError, TypeError):
                pass

        # Content similarity (simple token overlap)
        text_cur = set(msg.get("text", "").lower().split())
        text_prev = set(prev.get("text", "").lower().split())
        if text_cur and text_prev:
            overlap = len(text_cur & text_prev) / max(len(text_cur), 1)
            content_score = 1.0 - overlap
        else:
            content_score = 0.5

        # Combined confidence
        confidence = 0.4 * gap_score + 0.6 * content_score

        if confidence > 0.5:
            boundaries.append((i, ts_cur, f"topic_{len(boundaries)+1}", round(confidence, 2)))

    return boundaries


def build_index(rebuild=False, agent_filter="cross"):
    DB_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)

    if rebuild:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM messages_fts")
        conn.execute("DELETE FROM topic_boundaries")
        conn.commit()

    entries = scan_sessions(agent_filter)
    indexed = 0
    skipped = 0
    errors = 0

    t_start = time.time()

    for session_id, jsonl_path, agent in entries:
        # Skip if mtime unchanged (fast path)
        try:
            mtime = os.path.getmtime(jsonl_path)
        except OSError:
            errors += 1
            continue

        existing = conn.execute(
            "SELECT jsonl_mtime FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()

        if existing and existing[0] == mtime and not rebuild:
            skipped += 1
            continue

        try:
            stats = _dispatch_session_stats(jsonl_path)
        except Exception:
            errors += 1
            continue

        # Upsert session metadata
        conn.execute("""
            INSERT OR REPLACE INTO sessions
            (id, project_path, agent, created, modified, message_count,
             user_messages, assistant_messages, tool_calls, errors,
             compactions, total_tokens, branch, summary, first_prompt,
             jsonl_mtime, indexed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session_id,
            stats.get("branch", ""),
            agent,
            stats.get("started", ""),
            stats.get("ended", ""),
            stats.get("user_messages", 0) + stats.get("assistant_messages", 0),
            stats.get("user_messages", 0),
            stats.get("assistant_messages", 0),
            stats.get("tool_calls", 0),
            stats.get("errors", 0),
            stats.get("compactions", 0),
            stats.get("total_tokens", 0),
            stats.get("branch", ""),
            stats.get("summary", ""),
            "",
            mtime,
            time.time(),
        ))

        # Index messages into FTS
        if existing:
            conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))

        try:
            for msg in _dispatch_extract_messages(jsonl_path, role="both"):
                conn.execute(
                    "INSERT INTO messages_fts (session_id, role, timestamp, text) VALUES (?,?,?,?)",
                    (session_id, msg.get("role", ""), msg.get("timestamp", ""),
                     msg.get("text", "")[:2000])  # Cap per-message
                )
        except Exception:
            pass

        # Topic segmentation
        if existing:
            conn.execute("DELETE FROM topic_boundaries WHERE session_id = ?", (session_id,))

        try:
            all_msgs = list(_dispatch_extract_messages(jsonl_path, role="both"))
            boundaries = detect_topic_boundaries(all_msgs)
            for idx, ts, label, conf in boundaries:
                conn.execute(
                    "INSERT INTO topic_boundaries (session_id, message_index, timestamp, topic_label, confidence) VALUES (?,?,?,?,?)",
                    (session_id, idx, ts, label, conf)
                )
        except Exception:
            pass

        indexed += 1

    # Update meta
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?,?)",
        ("last_build", str(time.time()))
    )
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?,?)",
        ("total_sessions", str(indexed + skipped))
    )
    conn.commit()
    conn.close()

    elapsed = time.time() - t_start
    return {"indexed": indexed, "skipped": skipped, "errors": errors, "elapsed": round(elapsed, 2)}


def search_fts(keyword, limit=10):
    """Fast full-text search using the pre-built FTS index."""
    if not DB_PATH.exists():
        return None  # Index not built yet

    conn = sqlite3.connect(str(DB_PATH))
    try:
        # FTS5 query: prefix match + NEAR for proximity
        # Sanitize: FTS5 special chars
        safe_kw = keyword.replace('"', '""').replace(":", " ")
        rows = conn.execute("""
            SELECT session_id, role, timestamp, text,
                   bm25(messages_fts) as score
            FROM messages_fts
            WHERE messages_fts MATCH ?
            ORDER BY score
            LIMIT ?
        """, (safe_kw, limit)).fetchall()

        return [
            {"session_id": r[0], "role": r[1], "timestamp": r[2],
             "text": r[3], "score": round(r[4], 2)}
            for r in rows
        ]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()


def session_detail(session_id):
    """Return full detail for a session."""
    if not DB_PATH.exists():
        return None

    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        conn.close()
        return None

    cols = [d[0] for d in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    result = dict(zip(cols, row))

    topics = conn.execute(
        "SELECT message_index, timestamp, topic_label, confidence FROM topic_boundaries WHERE session_id = ?",
        (session_id,)
    ).fetchall()
    result["topics"] = [
        {"index": t[0], "timestamp": t[1], "label": t[2], "confidence": t[3]}
        for t in topics
    ]

    conn.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="session-digger index builder")
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="Build/update the index")
    p_build.add_argument("--project", default=None)
    p_build.add_argument("--rebuild", action="store_true")
    p_build.add_argument("--agent", default="cross", choices=["claude", "grok", "kimi", "cross"])

    p_search = sub.add_parser("search", help="Full-text search across all sessions")
    p_search.add_argument("keyword")
    p_search.add_argument("--limit", type=int, default=10)

    p_detail = sub.add_parser("detail", help="Get session detail")
    p_detail.add_argument("session_id")

    p_stats = sub.add_parser("stats", help="Index statistics")

    args = parser.parse_args()

    if args.command == "build":
        result = build_index(rebuild=args.rebuild, agent_filter=args.agent)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "search":
        results = search_fts(args.keyword, limit=args.limit)
        if results is None:
            print("Index not built. Run: index-builder.py build")
        else:
            print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.command == "detail":
        detail = session_detail(args.session_id)
        if detail is None:
            print("Session not found in index.")
        else:
            print(json.dumps(detail, ensure_ascii=False, indent=2))

    elif args.command == "stats":
        conn = sqlite3.connect(str(DB_PATH))
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        topics = conn.execute("SELECT COUNT(*) FROM topic_boundaries").fetchone()[0]
        last_build = conn.execute("SELECT value FROM index_meta WHERE key='last_build'").fetchone()
        conn.close()
        print(f"Sessions: {total} | Messages indexed: {fts_count} | Topic boundaries: {topics}")
        if last_build:
            from datetime import datetime
            ts = datetime.fromtimestamp(float(last_build[0]))
            print(f"Last built: {ts.strftime('%Y-%m-%d %H:%M:%S')}")

    else:
        parser.print_help()
