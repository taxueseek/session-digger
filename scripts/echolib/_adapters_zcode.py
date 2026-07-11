"""Adapters for ZCode / DIM / DimCode environments — extracted from _adapters.py.

All ``*_session_stats`` here rely on ``_empty_stats`` from the parent package
(`echolib._adapters`). Import is deferred inside the stat functions to keep the
package-level import order stable.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from echolib._claude import _normalize_timestamp
from echolib._helpers import (
    DIMCODE_DB_PATH,
    DIM_DIR,
    ZCODE_DIR,
    _extract_content_text,
    _iter_jsonl,
    _strip_system_reminder,
)
from echolib._models import SessionMeta


# ── ZCode (transcript.jsonl trace) ────────────────────────────────────
_ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def zcode_list_sessions(cwd=None, limit=50, keyword=""):
    """List ZCode sessions from ~/.zcode/cli/agents/."""
    sessions = []
    if not ZCODE_DIR.exists():
        return sessions
    for sess_dir in sorted(ZCODE_DIR.iterdir(), reverse=True):
        if not sess_dir.is_dir() or not sess_dir.name.startswith("sess_"):
            continue
        for agent_dir in sess_dir.iterdir():
            if not agent_dir.is_dir() or not agent_dir.name.startswith("agent_"):
                continue
            transcript = agent_dir / "transcript.jsonl"
            if not transcript.exists():
                continue
            try:
                sid = agent_dir.name
                started = ""
                model = ""
                for rec in _iter_jsonl(transcript):
                    ts = rec.get("timestamp", "")
                    if ts and not started:
                        started = _normalize_timestamp(ts)
                    rtype = rec.get("type", "")
                    if rtype in ("model_network_status", "model_request"):
                        payload = rec.get("payload", {})
                        if isinstance(payload, dict) and payload.get("model"):
                            model = payload.get("modelRef", payload.get("model", ""))
                            break
                    if started and model:
                        break
                sessions.append({
                    "id": sid, "title": f"ZCode {sess_dir.name[:20]}",
                    "created": started, "modified": "",
                    "message_count": 0, "path": str(transcript),
                    "agent": "ZCode", "model": model,
                })
            except OSError:
                continue
    if keyword:
        keyword_lower = keyword.lower()
        sessions = [s for s in sessions if keyword_lower in s.get("title", "").lower()]
    return sessions[:limit]


def zcode_session_stats(session_path):
    """Stats for ZCode trace-format transcript.jsonl."""
    from echolib._adapters import _empty_stats
    p = Path(session_path)
    stats = _empty_stats("zcode")
    stats["slug"] = p.stem
    if not p.exists() or not p.is_file():
        return stats
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        ts = rec.get("timestamp", "")
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not stats["started"] or nts < stats["started"]:
                    stats["started"] = nts
                if nts > stats["ended"]:
                    stats["ended"] = nts
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if rtype == "turn_started":
            inp = payload.get("input", "")
            if (isinstance(inp, str) and inp.strip()) or (isinstance(inp, list) and any(isinstance(i, dict) and i.get("text") for i in inp)):
                stats["user_messages"] += 1
        elif rtype == "model_complete":
            content = payload.get("content", [])
            if (isinstance(content, list) and content) or (isinstance(content, str) and content.strip()):
                stats["assistant_messages"] += 1
        elif rtype == "tool_call_scheduled":
            stats["tool_calls"] += 1
            tool_name = payload.get("toolName", "")
            if tool_name and not stats["model"]:
                stats["model"] = tool_name
        elif rtype in ("model_network_status", "model_request"):
            if not stats["model"]:
                model = payload.get("model", "")
                if model:
                    stats["model"] = model
    return stats


def zcode_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from ZCode trace format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        ts = rec.get("timestamp", "")
        nt = _normalize_timestamp(ts) if ts else ""
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue

        if role in ("user", "both") and rtype == "turn_started":
            inp = payload.get("input", "")
            if isinstance(inp, str) and inp.strip():
                text = inp[:500]
            elif isinstance(inp, list):
                texts = [str(i.get("text", "")) for i in inp if isinstance(i, dict) and i.get("text")]
                text = "\n".join(texts)[:500] if texts else ""
            else:
                text = ""
            if text:
                cleaned = _strip_system_reminder(text)
                if cleaned:
                    yield {"role": "USER", "timestamp": nt, "text": cleaned}
                    count += 1
                    if limit and count >= limit:
                        return
                    continue

        if role in ("assistant", "both") and rtype == "model_complete":
            text = _extract_content_text(payload.get("content", []), max_len=500)
            if text:
                yield {"role": "ASSISTANT", "timestamp": nt, "text": text}
                count += 1
                if limit and count >= limit:
                    return


def zcode_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from ZCode trace format."""
    p = Path(session_path)
    if not p.exists():
        return

    # Pre-scan for error call IDs when errors_only mode
    error_call_ids = set()
    if errors_only:
        for rec in _iter_jsonl(p):
            if rec.get("type") == "tool_batch_complete":
                pl = rec.get("payload", {})
                if isinstance(pl, dict) and pl.get("errorCount", 0) > 0:
                    for tid in pl.get("toolCallIds", []):
                        error_call_ids.add(tid)

    count = 0
    for rec in _iter_jsonl(p):
        if rec.get("type") != "tool_call_scheduled":
            continue
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        name = payload.get("toolName", "")
        if tool_filter and name != tool_filter:
            continue
        if errors_only and payload.get("toolCallId", "") not in error_call_ids:
            continue
        ts = rec.get("timestamp", "")
        nt = _normalize_timestamp(ts) if ts else ""
        args = payload.get("input", "")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        yield {"timestamp": nt, "name": name or "[tool]", "status": "ok",
               "key_input": str(args)[:150] if args else "", "result_preview": ""}
        count += 1
        if limit and count >= limit:
            return


def zcode_session_path(cwd, session_id=None):
    """Resolve ZCode session path."""
    if session_id:
        for sess_dir in ZCODE_DIR.iterdir():
            if not sess_dir.is_dir():
                continue
            for agent_dir in sess_dir.iterdir():
                if agent_dir.name == session_id:
                    return str(agent_dir / "transcript.jsonl")
    return str(ZCODE_DIR)


# ── ZCode SQLite DB mirror ────────────────────────────────────────────
def _zcode_db_connect():
    """Connect to ZCode SQLite DB (read-only)."""
    if not _ZCODE_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{_ZCODE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _zcode_db_fmt_timestamp(ts):
    """Format timestamp to ISO string. Handles millisecond Unix timestamps."""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OSError, OverflowError):
            return str(ts)
    if isinstance(ts, str):
        return ts
    return str(ts)


def _zcode_db_parse_message_data(data_json):
    """Parse the JSON ``data`` field of a message row."""
    if not data_json:
        return {}
    if isinstance(data_json, str):
        try:
            return json.loads(data_json)
        except (json.JSONDecodeError, ValueError):
            return {}
    return data_json if isinstance(data_json, dict) else {}


def zcode_db_list_sessions(limit=200, keyword=""):
    """List ZCode sessions from SQLite, in echolib-compatible format."""
    conn = _zcode_db_connect()
    if not conn:
        return []

    sessions = []
    try:
        cur = conn.cursor()
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

        for row in cur.fetchall():
            sid = row["id"]
            title = row["title"] or row["slug"] or sid[:12]
            created = _zcode_db_fmt_timestamp(row["time_created"])
            modified = _zcode_db_fmt_timestamp(row["time_updated"])
            msg_count = row["msg_count"] or 0

            summary = title[:100]

            if keyword:
                haystack = f"{summary} {title}".lower()
                if keyword.lower() not in haystack:
                    continue

            sessions.append({
                "id": sid, "title": title,
                "created": created, "modified": modified,
                "message_count": msg_count, "path": f"zcode://{sid}",
                "agent": "zcode", "model": "",
            })

            if len(sessions) >= limit:
                break
    finally:
        conn.close()

    return sessions


def zcode_db_session_stats(session_id):
    """Get stats for a ZCode session from SQLite."""
    from echolib._adapters import _empty_stats
    conn = _zcode_db_connect()
    if not conn:
        return _empty_stats("zcode")

    stats = _empty_stats("zcode")
    stats["slug"] = session_id[:12]

    try:
        cur = conn.cursor()

        # Session metadata
        cur.execute("SELECT slug, title, time_created, time_updated FROM session WHERE id = ?", (session_id,))
        row = cur.fetchone()
        if row:
            stats["slug"] = row["slug"] or session_id[:12]
            stats["started"] = _zcode_db_fmt_timestamp(row["time_created"])
            stats["ended"] = _zcode_db_fmt_timestamp(row["time_updated"])

        # Message counts by role
        cur.execute("SELECT data FROM message WHERE session_id = ? ORDER BY time_created", (session_id,))
        user_count = 0
        assistant_count = 0
        for mrow in cur.fetchall():
            md = _zcode_db_parse_message_data(mrow["data"])
            role = md.get("role", "")
            if role == "user":
                user_count += 1
            elif role in ("assistant", "model"):
                assistant_count += 1

        stats["user_messages"] = user_count
        stats["assistant_messages"] = assistant_count

        # Token usage
        cur.execute("""
            SELECT COALESCE(SUM(input_tokens), 0) as inp,
                   COALESCE(SUM(output_tokens), 0) as out
            FROM turn_usage WHERE session_id = ?
        """, (session_id,))
        row = cur.fetchone()
        if row:
            stats["input_tokens"] = row["inp"]
            stats["output_tokens"] = row["out"]
            stats["total_tokens"] = row["inp"] + row["out"]

        # Tool calls
        cur.execute("""
            SELECT COUNT(*) as cnt FROM part p
            JOIN message m ON m.id = p.message_id
            WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
        """, (session_id,))
        row = cur.fetchone()
        stats["tool_calls"] = row["cnt"] if row else 0

        # Errors
        cur.execute("""
            SELECT COUNT(*) as cnt FROM part p
            JOIN message m ON m.id = p.message_id
            WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
              AND json_extract(p.data, '$.state.status') = 'error'
        """, (session_id,))
        row = cur.fetchone()
        stats["errors"] = row["cnt"] if row else 0

    finally:
        conn.close()

    return stats


def zcode_db_extract_tools(session_id, limit=30):
    """Extract tool calls from a ZCode session via SQLite."""
    conn = _zcode_db_connect()
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

            output = state.get("output", "") if isinstance(state, dict) else ""
            if isinstance(output, str):
                result_preview = output[:150].replace("\n", " ")
            elif isinstance(output, dict):
                result_preview = json.dumps(output, ensure_ascii=False)[:150]
            else:
                result_preview = ""

            ts = _zcode_db_fmt_timestamp(row["time_created"])
            tools.append({
                "timestamp": ts, "name": tool_name,
                "status": status, "key_input": inp_str,
                "result_preview": result_preview,
            })
    finally:
        conn.close()

    return tools


def zcode_db_extract_messages(session_id, role="both", limit=5):
    """Extract messages from a ZCode session via SQLite."""
    conn = _zcode_db_connect()
    if not conn:
        return []

    messages = []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, data, time_created FROM message
            WHERE session_id = ?
            ORDER BY time_created ASC
        """, (session_id,))

        for mrow in cur.fetchall():
            md = _zcode_db_parse_message_data(mrow["data"])
            msg_role = md.get("role", "unknown")
            ts = _zcode_db_fmt_timestamp(mrow["time_created"])
            mapped_role = "USER" if msg_role == "user" else "ASSISTANT"

            if role == "user" and mapped_role != "USER":
                continue
            if role == "assistant" and mapped_role != "ASSISTANT":
                continue

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
                "role": mapped_role, "timestamp": ts,
                "text": text[:2000],
            })

            if limit and len(messages) >= limit:
                break
    finally:
        conn.close()

    return messages


# ── DIM (memory-summary) ─────────────────────────────────────────────
def dim_list_sessions(cwd=None, limit=50, keyword=""):
    """List DIM memory sessions from ~/.dim/memory/."""
    sessions = []
    if not DIM_DIR.exists():
        return sessions
    for mem_dir in sorted(DIM_DIR.iterdir(), reverse=True):
        if not mem_dir.is_dir():
            continue
        for date_dir in sorted(mem_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for jf in date_dir.glob("*.jsonl"):
                try:
                    # backfill.jsonl: each record is a separate session
                    if jf.name == "backfill.jsonl":
                        for idx, rec in enumerate(_iter_jsonl(jf)):
                            st_val = rec.get("session_time", rec.get("timestamp", ""))
                            started = _normalize_timestamp(st_val) if st_val else ""
                            intent = str(rec.get("intent", ""))[:80] if rec.get("intent") else f"backfill #{idx+1}"
                            sessions.append({
                                "id": f"{jf.stem}_{idx:03d}",
                                "title": intent,
                                "created": started, "modified": "",
                                "message_count": 0, "path": str(jf),
                                "agent": "DIM", "model": "",
                            })
                    else:
                        started = ""
                        intent = ""
                        for rec in _iter_jsonl(jf):
                            st_val = rec.get("session_time", rec.get("timestamp", ""))
                            if st_val and not started:
                                started = _normalize_timestamp(st_val)
                            if rec.get("intent") and not intent:
                                intent = str(rec["intent"])[:80]
                            if started and intent:
                                break
                        sessions.append({
                            "id": jf.stem,
                            "title": intent or f"DIM {jf.stem[:20]}",
                            "created": started, "modified": "",
                            "message_count": 0, "path": str(jf),
                            "agent": "DIM", "model": "",
                        })
                except OSError:
                    continue
    if keyword:
        keyword_lower = keyword.lower()
        sessions = [s for s in sessions if keyword_lower in s.get("title", "").lower()]
    return sessions[:limit]


def dim_session_stats(session_path):
    """Stats for DIM memory-summary format."""
    from echolib._adapters import _empty_stats
    p = Path(session_path)
    stats = _empty_stats("dim")
    stats["slug"] = p.stem
    if not p.exists() or not p.is_file():
        return stats
    for rec in _iter_jsonl(p):
        ts = rec.get("session_time", rec.get("timestamp", ""))
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not stats["started"] or nts < stats["started"]:
                    stats["started"] = nts
                if nts > stats["ended"]:
                    stats["ended"] = nts
        if rec.get("intent"):
            stats["user_messages"] += 1
        if rec.get("learned") or rec.get("outcome"):
            stats["assistant_messages"] += 1
        actions = rec.get("actions", [])
        if isinstance(actions, list):
            stats["tool_calls"] += len(actions)
        if rec.get("model_perf"):
            mp = rec["model_perf"]
            if isinstance(mp, dict) and mp.get("model"):
                stats["model"] = str(mp["model"])
        if rec.get("intent") and not stats["summary"]:
            stats["summary"] = str(rec["intent"])[:200]
    return stats


def dim_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from DIM memory-summary format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        ts = rec.get("session_time", rec.get("timestamp", ""))
        nt = _normalize_timestamp(ts) if ts else ""
        if role in ("user", "both") and rec.get("intent"):
            text = str(rec["intent"])
            actions = rec.get("actions", [])
            if isinstance(actions, list) and actions:
                text += "\n[Actions: " + ", ".join(str(a.get("name", a) if isinstance(a, dict) else a)[:30] for a in actions[:5]) + "]"
            yield {"role": "USER", "timestamp": nt, "text": text[:500]}
            count += 1
            if limit and count >= limit:
                return
            continue
        if role in ("assistant", "both") and (rec.get("learned") or rec.get("outcome")):
            parts = []
            if rec.get("learned"):
                parts.append("[Learned] " + str(rec["learned"])[:200])
            if rec.get("outcome"):
                parts.append("[Outcome] " + str(rec["outcome"])[:200])
            yield {"role": "ASSISTANT", "timestamp": nt, "text": "\n".join(parts)[:500]}
            count += 1
            if limit and count >= limit:
                return


def dim_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Extract tool-like actions from DIM memory format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        actions = rec.get("actions", [])
        if not isinstance(actions, list):
            continue
        ts = rec.get("session_time", rec.get("timestamp", ""))
        nt = _normalize_timestamp(ts) if ts else ""
        for action in actions:
            if not isinstance(action, dict):
                continue
            name = action.get("name", action.get("type", "action"))
            if tool_filter and name != tool_filter:
                continue
            if errors_only and not action.get("error"):
                continue
            status = "error" if action.get("error") else "ok"
            yield {"timestamp": nt, "name": str(name), "status": status,
                   "key_input": str(action.get("input", action.get("args", "")))[:150],
                   "result_preview": str(action.get("result", ""))[:80]}
            count += 1
            if limit and count >= limit:
                return


def dim_session_path(cwd, session_id=None):
    """Resolve DIM session path."""
    if session_id:
        # Check if session_id is already a full path
        p = Path(session_id)
        if p.is_file():
            return str(p)
        # Search for a matching file
        for mem_dir in DIM_DIR.iterdir():
            if not mem_dir.is_dir():
                continue
            for date_dir in mem_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                for jf in date_dir.glob(f"*{session_id}*.jsonl"):
                    return str(jf)
    return str(DIM_DIR)


# ── DimCode (SQLite) ────────────────────────────────────────────────
def _dimcode_db_connect():
    """Connect to DimCode SQLite DB (read-only)."""
    if not DIMCODE_DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{DIMCODE_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def dimcode_list_sessions(cwd=None, limit=50, keyword=""):
    """List DimCode sessions from dimcode.sqlite."""
    conn = _dimcode_db_connect()
    if not conn:
        return []
    sessions = []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.sessionId, s.title, s.cwd, s.createdAt, s.status,
                   (SELECT COUNT(*) FROM messages m WHERE m.sessionId = s.sessionId) as msg_count
            FROM sessions s
            ORDER BY s.createdAt DESC
            LIMIT ?
        """, (limit * 2,))
        for row in cur.fetchall():
            sid = row["sessionId"]
            title = row["title"] or sid[:20]
            created = row["createdAt"] or ""
            msg_count = row["msg_count"] or 0
            summary = title[:100]
            if keyword:
                haystack = f"{summary} {title}".lower()
                if keyword.lower() not in haystack:
                    continue
            sessions.append({
                "id": sid, "title": title,
                "created": created, "modified": "",
                "message_count": msg_count, "path": f"dimcode://{sid}",
                "agent": "DimCode", "model": "",
            })
            if len(sessions) >= limit:
                break
    finally:
        conn.close()
    return sessions


def _dimcode_normalize_session_id(session_id):
    """Accept dimcode://URI, dimcode:id, or bare sessionId."""
    s = str(session_id or "")
    if s.startswith("dimcode://"):
        return s[len("dimcode://"):]
    if s.startswith("dimcode:"):
        return s.split(":", 1)[1]
    # index ids look like dimcode:sess_xxx
    if s.startswith("dimcode:") is False and "/" not in s and s.startswith("sess_"):
        return s
    if ":" in s and s.split(":", 1)[0] in ("dimcode", "dim"):
        return s.split(":", 1)[1]
    return s


def dimcode_session_stats(session_id):
    """Get stats for a DimCode session from SQLite."""
    session_id = _dimcode_normalize_session_id(session_id)
    conn = _dimcode_db_connect()
    if not conn:
        return {}
    stats = {"slug": "", "model": "dimcode", "started": "", "ended": "",
             "user_messages": 0, "assistant_messages": 0, "tool_calls": 0,
             "errors": 0, "input_tokens": 0, "output_tokens": 0,
             "total_tokens": 0, "summary": ""}
    try:
        cur = conn.cursor()
        cur.execute("SELECT title, createdAt, updatedAt FROM sessions WHERE sessionId = ?", (session_id,))
        row = cur.fetchone()
        if row:
            stats["slug"] = session_id[:20]
            stats["summary"] = (row["title"] or "")[:200]
            stats["started"] = row["createdAt"] or ""
            # updatedAt if column exists
            try:
                stats["ended"] = row["updatedAt"] or ""
            except (IndexError, KeyError, TypeError):
                stats["ended"] = ""
        # Count messages by role + last message time as ended
        cur.execute("SELECT role, COUNT(*) as cnt FROM messages WHERE sessionId = ? GROUP BY role", (session_id,))
        for r in cur.fetchall():
            if r["role"] == "user":
                stats["user_messages"] = r["cnt"]
            elif r["role"] == "assistant":
                stats["assistant_messages"] = r["cnt"]
            elif r["role"] in ("tool", "function"):
                stats["tool_calls"] += r["cnt"]
        cur.execute("SELECT MAX(createdAt) as t1 FROM messages WHERE sessionId = ?", (session_id,))
        r2 = cur.fetchone()
        if r2 and r2["t1"] and not stats.get("ended"):
            stats["ended"] = r2["t1"]
        # Token usage
        cur.execute("""
            SELECT COALESCE(SUM(inputTokens), 0) as inp,
                   COALESCE(SUM(outputTokens), 0) as out
            FROM usage_run_stats WHERE sessionId = ?
        """, (session_id,))
        row = cur.fetchone()
        if row:
            stats["input_tokens"] = row["inp"]
            stats["output_tokens"] = row["out"]
            stats["total_tokens"] = row["inp"] + row["out"]
    except Exception:
        pass
    finally:
        conn.close()
    return stats


def dimcode_extract_messages(session_id, role="both", limit=0, thinking_limit=0):
    """Extract messages from a DimCode session via SQLite."""
    session_id = _dimcode_normalize_session_id(session_id)
    conn = _dimcode_db_connect()
    if not conn:
        return
    count = 0
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT messageId, role, parts, createdAt
            FROM messages
            WHERE sessionId = ?
            ORDER BY orderKey ASC
        """, (session_id,))
        for row in cur.fetchall():
            msg_role = row["role"].upper() if row["role"] else "USER"
            if role not in ("both", msg_role.lower(), msg_role):
                continue
            ts = row["createdAt"] or ""
            parts = row["parts"]
            text = ""
            if parts:
                try:
                    parsed = json.loads(parts)
                    if isinstance(parsed, list):
                        texts = []
                        for p in parsed:
                            if isinstance(p, dict) and p.get("type") == "text":
                                texts.append(p.get("text", ""))
                            elif isinstance(p, dict) and p.get("type") == "tool_call":
                                fn = p.get("function", {}).get("name", "?")
                                texts.append(f"[TOOL: {fn}]")
                        text = "\n".join(texts)
                    elif isinstance(parsed, dict) and parsed.get("type") == "text":
                        text = parsed.get("text", "")
                except (json.JSONDecodeError, ValueError):
                    text = str(parts)[:200]
            if not text:
                continue
            yield {"role": msg_role, "timestamp": ts, "text": text[:500]}
            count += 1
            if limit and count >= limit:
                return
    finally:
        conn.close()


def dimcode_extract_tools(session_id, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from DimCode session via SQLite."""
    session_id = _dimcode_normalize_session_id(session_id)
    conn = _dimcode_db_connect()
    if not conn:
        return
    count = 0
    try:
        cur = conn.cursor()
        # Look for assistant messages with tool_call parts
        cur.execute("""
            SELECT messageId, parts, createdAt
            FROM messages
            WHERE sessionId = ? AND role = 'assistant'
            ORDER BY orderKey ASC
        """, (session_id,))
        for row in cur.fetchall():
            parts = row["parts"]
            if not parts:
                continue
            try:
                parsed = json.loads(parts)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(parsed, list):
                continue
            for p in parsed:
                if not isinstance(p, dict) or p.get("type") != "tool_call":
                    continue
                name = p.get("function", {}).get("name", "")
                if tool_filter and name != tool_filter:
                    continue
                ts = row["createdAt"] or ""
                args = p.get("function", {}).get("arguments", "")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                if errors_only:
                    # DimCode doesn't expose error status in parts-based format
                    continue
                yield {"timestamp": ts, "name": name or "[tool]", "status": "ok",
                       "key_input": str(args)[:150] if args else "", "result_preview": ""}
                count += 1
                if limit and count >= limit:
                    return
    finally:
        conn.close()


def dimcode_session_path(cwd, session_id=None):
    """Resolve DimCode session identifier."""
    if session_id and session_id.startswith("dimcode://"):
        return session_id
    return f"dimcode://{session_id}" if session_id else str(DIMCODE_DB_PATH)
