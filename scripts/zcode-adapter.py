#!/usr/bin/env python3
"""
zcode-adapter.py — ZCode 会话数据适配器。
从 ZCode SQLite 数据库读取会话数据，以 session-digger 兼容格式输出。

用法:
  zcode-adapter.py list-sessions [--limit N] [--keyword K]
  zcode-adapter.py session-stats <session_id>
  zcode-adapter.py extract-tools <session_id> [--limit N]
  zcode-adapter.py extract-messages <session_id> [--role user|assistant|both] [--limit N]

v1.0
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
ZCODE_V2_INDEX = Path.home() / ".zcode" / "v2" / "tasks-index.sqlite"


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def _connect():
    """Connect to ZCode main SQLite DB (read-only)."""
    if not ZCODE_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{ZCODE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_message_data(data_json):
    """Parse the JSON `data` field of a message row."""
    if not data_json:
        return {}
    if isinstance(data_json, str):
        try:
            return json.loads(data_json)
        except (json.JSONDecodeError, ValueError):
            return {}
    return data_json if isinstance(data_json, dict) else {}


# ---------------------------------------------------------------------------
# Session listing
# ---------------------------------------------------------------------------

def list_sessions(limit=200, keyword=""):
    """List ZCode sessions in cross_tool_list_sessions compatible format."""
    conn = _connect()
    if not conn:
        return []

    sessions = []
    try:
        cur = conn.cursor()
        # Query sessions (non-deleted, non-archived, excluding subagent children)
        cur.execute("""
            SELECT s.id, s.slug, s.title, s.task_type, s.time_created, s.time_updated,
                   s.parent_id,
                   COALESCE(tu.input_tokens, 0) as input_tokens,
                   COALESCE(tu.output_tokens, 0) as output_tokens,
                   (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) as msg_count
            FROM session s
            LEFT JOIN (
                SELECT session_id, SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens
                FROM turn_usage GROUP BY session_id
            ) tu ON tu.session_id = s.id
            WHERE s.task_type != 'subagent_child'
               OR s.task_type IS NULL
            ORDER BY s.time_created DESC
            LIMIT ?
        """, (limit * 2,))

        rows = cur.fetchall()
        for row in rows:
            sid = row["id"]
            title = row["title"] or row["slug"] or sid[:12]
            created = _fmt_timestamp(row["time_created"])
            modified = _fmt_timestamp(row["time_updated"])
            msg_count = row["msg_count"] or 0

            # Get summary from first user message
            summary = ""
            first_prompt = ""
            try:
                cur2 = conn.cursor()
                cur2.execute("""
                    SELECT m.data, m.time_created FROM message m
                    WHERE m.session_id = ? ORDER BY m.time_created ASC LIMIT 20
                """, (sid,))
                msgs = cur2.fetchall()
                for m in msgs:
                    md = _parse_message_data(m["data"])
                    role = md.get("role", "")
                    if role == "user" and not first_prompt:
                        # Get text content from parts
                        cur3 = conn.cursor()
                        cur3.execute("""
                            SELECT p.data FROM part p
                            JOIN message m2 ON m2.id = p.message_id
                            WHERE m2.session_id = ? AND json_extract(p.data, '$.type') = 'text'
                            ORDER BY p.id ASC LIMIT 1
                        """, (sid,))
                        txt_row = cur3.fetchone()
                        if txt_row:
                            pd = json.loads(txt_row["data"]) if isinstance(txt_row["data"], str) else txt_row["data"]
                            first_prompt = (pd.get("text", "") or "")[:200]
                            summary = title[:100]
                    if role == "user" and len(first_prompt) < 50:
                        # Try harder to get first prompt
                        cur3 = conn.cursor()
                        cur3.execute("""
                            SELECT p.data FROM part p
                            JOIN message m2 ON m2.id = p.message_id
                            WHERE m2.session_id = ? AND json_extract(p.data, '$.type') = 'text'
                            ORDER BY p.id ASC LIMIT 1
                        """, (sid,))
                        txt_row = cur3.fetchone()
                        if txt_row:
                            pd = json.loads(txt_row["data"]) if isinstance(txt_row["data"], str) else txt_row["data"]
                            first_prompt = (pd.get("text", "") or "")[:200]
                            summary = title[:100]
            except Exception:
                pass

            # Filter by keyword
            if keyword:
                haystack = f"{summary} {first_prompt} {title}".lower()
                if keyword.lower() not in haystack:
                    continue

            sessions.append({
                "agent": "zcode",
                "session_id": sid,
                "created": created,
                "modified": modified,
                "summary": summary or title[:100],
                "first_prompt": first_prompt,
                "msg_count": msg_count,
                "full_path": f"zcode://{sid}",
            })

            if len(sessions) >= limit:
                break

    finally:
        conn.close()

    return sessions


# ---------------------------------------------------------------------------
# Session stats
# ---------------------------------------------------------------------------

def session_stats(session_id):
    """Get stats for a ZCode session, compatible with echolib.session_stats()."""
    conn = _connect()
    if not conn:
        return _empty_stats()

    stats = _empty_stats()
    stats["slug"] = session_id[:12]

    try:
        cur = conn.cursor()

        # Session metadata
        cur.execute("SELECT slug, title, time_created, time_updated FROM session WHERE id = ?", (session_id,))
        row = cur.fetchone()
        if row:
            stats["slug"] = row["slug"] or session_id[:12]
            stats["started"] = _fmt_timestamp(row["time_created"])
            stats["ended"] = _fmt_timestamp(row["time_updated"])

        # Message counts
        cur.execute("""
            SELECT COUNT(*) as total FROM message WHERE session_id = ?
        """, (session_id,))
        row = cur.fetchone()
        total_msgs = row["total"] if row else 0

        # Count user/assistant by parsing message data JSON
        cur.execute("""
            SELECT data FROM message WHERE session_id = ? ORDER BY time_created
        """, (session_id,))
        user_count = 0
        assistant_count = 0
        for mrow in cur.fetchall():
            md = _parse_message_data(mrow["data"])
            role = md.get("role", "")
            if role == "user":
                user_count += 1
            elif role in ("assistant", "model"):
                assistant_count += 1

        stats["user_messages"] = user_count
        stats["assistant_messages"] = assistant_count

        # Token usage from turn_usage aggregation
        cur.execute("""
            SELECT COALESCE(SUM(input_tokens), 0) as inp,
                   COALESCE(SUM(output_tokens), 0) as out,
                   COALESCE(SUM(cache_read_input_tokens), 0) as cache_read,
                   COALESCE(SUM(cache_creation_input_tokens), 0) as cache_create
            FROM turn_usage WHERE session_id = ?
        """, (session_id,))
        row = cur.fetchone()
        if row:
            stats["input_tokens"] = row["inp"]
            stats["output_tokens"] = row["out"]
            stats["cache_read_tokens"] = row["cache_read"]
            stats["cache_create_tokens"] = row["cache_create"]
            stats["total_tokens"] = row["inp"] + row["out"]

        # Tool calls count
        cur.execute("""
            SELECT COUNT(*) as cnt FROM part p
            JOIN message m ON m.id = p.message_id
            WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
        """, (session_id,))
        row = cur.fetchone()
        stats["tool_calls"] = row["cnt"] if row else 0

        # Errors count (tool calls with error status)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM part p
            JOIN message m ON m.id = p.message_id
            WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
              AND json_extract(p.data, '$.state.status') = 'error'
        """, (session_id,))
        row = cur.fetchone()
        stats["errors"] = row["cnt"] if row else 0

        # Model name
        cur.execute("""
            SELECT data FROM message WHERE session_id = ? AND data LIKE '%model%' ORDER BY time_created LIMIT 1
        """, (session_id,))
        row = cur.fetchone()
        if row:
            md = _parse_message_data(row["data"])
            model_id = md.get("model", {})
            if isinstance(model_id, dict):
                stats["model"] = model_id.get("modelId", model_id.get("id", "zcode-unknown"))
            elif isinstance(model_id, str):
                stats["model"] = model_id

    finally:
        conn.close()

    return stats


def _empty_stats():
    return {
        "slug": "", "model": "zcode", "branch": "",
        "started": "", "ended": "",
        "user_messages": 0, "assistant_messages": 0,
        "tool_calls": 0, "files_edited": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "compactions": 0, "summary": "",
        "total_tokens": 0,
    }


# ---------------------------------------------------------------------------
# Tool extraction
# ---------------------------------------------------------------------------

def extract_tools(session_id, limit=30):
    """Extract tool calls from a ZCode session."""
    conn = _connect()
    if not conn:
        return []

    tools = []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.data, m.time_created
            FROM part p
            JOIN message m ON m.id = p.message_id
            WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
            ORDER BY p.id ASC
            LIMIT ?
        """, (session_id, limit))

        for row in cur.fetchall():
            pd = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
            if not isinstance(pd, dict):
                continue
            tool_name = pd.get("tool", pd.get("name", ""))
            state = pd.get("state", pd.get("status", {}))
            if isinstance(state, dict):
                status = "error" if state.get("status") in ("error", "failure") else "ok"
            else:
                status = "ok"

            inp = state.get("input", "") if isinstance(state, dict) else ""
            if isinstance(inp, dict):
                inp_str = json.dumps(inp, ensure_ascii=False)[:150]
            elif isinstance(inp, str):
                inp_str = inp[:150]
            else:
                inp_str = str(inp)[:150]

            key_input = inp_str
            result_preview = ""
            output = state.get("output", "") if isinstance(state, dict) else ""
            if isinstance(output, str):
                result_preview = output[:150].replace("\n", " ")
            elif isinstance(output, dict):
                result_preview = json.dumps(output, ensure_ascii=False)[:150]

            ts = _fmt_timestamp(row["time_created"])

            tools.append({
                "timestamp": ts,
                "name": tool_name,
                "status": status,
                "key_input": key_input,
                "result_preview": result_preview,
            })
    finally:
        conn.close()

    return tools


# ---------------------------------------------------------------------------
# Message extraction
# ---------------------------------------------------------------------------

def extract_messages(session_id, role="both", limit=5):
    """Extract messages from a ZCode session."""
    conn = _connect()
    if not conn:
        return []

    messages = []
    try:
        cur = conn.cursor()
        # First get all messages
        cur.execute("""
            SELECT id, data, time_created FROM message
            WHERE session_id = ?
            ORDER BY time_created ASC
        """, (session_id,))
        msg_rows = cur.fetchall()

        for mrow in msg_rows:
            md = _parse_message_data(mrow["data"])
            msg_role = md.get("role", "unknown")
            ts = _fmt_timestamp(mrow["time_created"])

            # Map role
            mapped_role = "USER" if msg_role == "user" else "ASSISTANT"

            # Filter by role
            if role == "user" and mapped_role != "USER":
                continue
            if role == "assistant" and mapped_role != "ASSISTANT":
                continue

            # Get text content for this message
            cur2 = conn.cursor()
            cur2.execute("""
                SELECT p.data FROM part p
                WHERE p.message_id = ? AND json_extract(p.data, '$.type') = 'text'
                ORDER BY p.id ASC LIMIT 5
            """, (mrow["id"],))
            text_parts = []
            for part_row in cur2.fetchall():
                pd = json.loads(part_row["data"]) if isinstance(part_row["data"], str) else part_row["data"]
                if isinstance(pd, dict):
                    text_parts.append(pd.get("text", "") or "")
                elif isinstance(pd, str):
                    text_parts.append(pd)

            text = "\n".join(text_parts)
            if not text.strip():
                continue

            messages.append({
                "role": mapped_role,
                "timestamp": ts,
                "text": text[:2000],  # truncate like echolib
            })

            if limit and len(messages) >= limit:
                break
    finally:
        conn.close()

    return messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_timestamp(ts):
    """Format timestamp to ISO string. Handles millisecond Unix timestamps."""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        from datetime import datetime, timezone
        # Detect millisecond timestamps (>1e12) and convert to seconds
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OSError, OverflowError):
            return str(ts)
    if isinstance(ts, str):
        return ts
    return str(ts)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ZCode session data adapter")
    sub = parser.add_subparsers(dest="command")

    # list-sessions
    p_list = sub.add_parser("list-sessions")
    p_list.add_argument("--limit", type=int, default=200)
    p_list.add_argument("--keyword", default="")

    # session-stats
    p_stats = sub.add_parser("session-stats")
    p_stats.add_argument("session_id")

    # extract-tools
    p_tools = sub.add_parser("extract-tools")
    p_tools.add_argument("session_id")
    p_tools.add_argument("--limit", type=int, default=30)

    # extract-messages
    p_msgs = sub.add_parser("extract-messages")
    p_msgs.add_argument("session_id")
    p_msgs.add_argument("--role", default="both", choices=["user", "assistant", "both"])
    p_msgs.add_argument("--limit", type=int, default=5)

    args = parser.parse_args()

    if args.command == "list-sessions":
        result = list_sessions(limit=args.limit, keyword=args.keyword)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "session-stats":
        result = session_stats(args.session_id)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "extract-tools":
        result = extract_tools(args.session_id, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "extract-messages":
        result = extract_messages(args.session_id, role=args.role, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
