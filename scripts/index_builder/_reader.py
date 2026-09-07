"""Read layer over the prebuilt index (sessions + messages_fts tables).

Everything here reads ONLY the index — no JSONL parsing. Callers fall back
to echolib dispatch for sessions absent from the index. Write-side contract
(split-CJK storage) lives in index_builder._cjk; every text read back here
goes through uncjk() before leaving this module.
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

from index_builder._cjk import build_match_query, uncjk
from index_builder._schema import DB_PATH

# Bilingual decision-signal patterns (single source; sd-recall re-exports).
DECISION_PATTERNS = [
    r"(?i)\bdecided to\b", r"(?i)\bchose to\b", r"(?i)\bgoing to (use|switch|try|migrate)\b",
    r"(?i)\bwill (use|switch|try|migrate|go with)\b", r"(?i)\binstead of\b",
    r"(?i)\bswitch(ed|ing)? to\b", r"(?i)\buse \w+ over\b", r"(?i)\bmoving to\b",
    r"决定", r"选择", r"改用", r"还是", r"换成", r"放弃", r"尝试",
]


def _connect():
    """Open the index, or None when it does not exist yet."""
    if not DB_PATH.exists():
        return None
    return sqlite3.connect(str(DB_PATH))


def quick_stats_from_index(session_ids):
    """Batch-read precomputed stats from the sessions table.

    Returns {session_id: {msgs, tools, errors, branch, tool_errors_json,
    summary, first_prompt, created}} — entries missing from the index are
    absent from the result (caller falls back to file parsing for those).
    """
    if not session_ids:
        return {}
    conn = _connect()
    if conn is None:
        return {}
    try:
        q = ",".join("?" * len(session_ids))
        rows = conn.execute(
            f"""SELECT id, user_messages, tool_calls, errors, branch,
                       tool_errors_json, summary, first_prompt, created
                FROM sessions WHERE id IN ({q})""",
            list(session_ids),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {
        r[0]: {
            "msgs": r[1], "tools": r[2], "errors": r[3], "branch": r[4] or "",
            "tool_errors_json": r[5], "summary": r[6] or "",
            "first_prompt": r[7] or "", "created": r[8] or "",
        }
        for r in rows
    }


def evidence_from_index(session_id, decisions=False, limit_msgs=15):
    """Evidence from the prebuilt index instead of re-parsing the JSONL file.

    messages_fts stores text in split-CJK form — uncjk() before display or
    decision matching. Tool errors come from the sessions table aggregate
    (name → count), not per-call rows. Same shape as extract_evidence minus
    full_excerpt (deep mode still goes to the file).
    """
    result = {
        "user_messages": [],
        "tool_errors": [],
        "decisions": [] if decisions else None,
    }
    conn = _connect()
    if conn is None:
        return result
    try:
        rows = conn.execute(
            """SELECT role, timestamp, text FROM messages_fts
               WHERE session_id = ? AND role = 'USER' LIMIT ?""",
            (session_id, limit_msgs),
        ).fetchall()
    except sqlite3.Error:
        return result
    finally:
        conn.close()

    decision_res = [re.compile(p) for p in DECISION_PATTERNS] if decisions else []
    for _role, ts, text in rows:
        display = uncjk(text or "")
        entry = {"role": "USER", "timestamp": ts, "text": display[:300]}
        result["user_messages"].append(entry)
        if decisions:
            for pat in decision_res:
                if pat.search(display):
                    result["decisions"].append(entry)
                    break
    return result


def recent_sessions(days=7, agent=None, keyword=None, limit=12, min_messages=4,
                    sort="quality"):
    """Pick candidate sessions for analysis, index-only.

    sort: "quality" (message_count DESC) or "recent" (created/modified DESC).
    keyword routes through FTS first (build_match_query), then details come
    from the sessions table. Returns a list of plain dicts.
    """
    conn = _connect()
    if conn is None:
        return []
    cols = ("id, agent, jsonl_path, created, modified, message_count, "
            "user_messages, assistant_messages, tool_calls, errors, summary, "
            "first_prompt, model, project_path, tool_errors_json, jsonl_mtime")
    since = None
    if days:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        if keyword:
            match_q = build_match_query(keyword)
            if not match_q:
                return []
            rows = conn.execute(
                f"""SELECT {cols} FROM sessions
                    WHERE id IN (SELECT DISTINCT session_id FROM messages_fts
                                 WHERE messages_fts MATCH ?)
                      AND message_count >= ?
                    ORDER BY message_count DESC LIMIT ?""",
                (match_q, min_messages, limit),
            ).fetchall()
        else:
            clauses = ["message_count >= ?"]
            params = [min_messages]
            if since:
                clauses.append("(created >= ? OR created = '' OR created IS NULL)")
                params.append(since)
            if agent:
                clauses.append("agent = ?")
                params.append(agent)
            order = ("message_count DESC" if sort == "quality"
                     else "created DESC, modified DESC")
            params.append(limit)
            rows = conn.execute(
                f"""SELECT {cols} FROM sessions
                    WHERE {' AND '.join(clauses)}
                    ORDER BY {order} LIMIT ?""",
                params,
            ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    names = [c.strip() for c in cols.split(",")]
    return [dict(zip(names, r)) for r in rows]


def global_aggregates(days=7, agent=None):
    """Totals + per-env + per-day distribution, one connection, index-only.

    days=None (or 0) scopes everything to all time; a positive number scopes
    totals/envs to the window and adds a per-day breakdown.
    """
    conn = _connect()
    if conn is None:
        return None
    try:
        since = None
        if days:
            since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        clauses, params = [], []
        if since:
            clauses.append("created >= ?")
            params.append(since)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        where = " AND ".join(clauses) if clauses else "1=1"

        row = conn.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(user_messages+assistant_messages),0),
                       COALESCE(SUM(tool_calls),0), COALESCE(SUM(errors),0),
                       COALESCE(SUM(total_tokens),0) FROM sessions WHERE {where}""",
            params,
        ).fetchone()
        totals = {"sessions": row[0], "messages": row[1], "tool_calls": row[2],
                  "tool_errors": row[3], "total_tokens": row[4]}

        env_rows = conn.execute(
            f"""SELECT agent, COUNT(*), SUM(tool_calls), SUM(errors)
                FROM sessions WHERE {where} GROUP BY agent ORDER BY COUNT(*) DESC LIMIT 10""",
            params,
        ).fetchall()

        daily_rows = []
        if since:
            daily_rows = conn.execute(
                f"""SELECT substr(created, 1, 10) d, COUNT(*)
                    FROM sessions WHERE {' AND '.join(clauses + ["created != ''"])}
                    GROUP BY d ORDER BY d DESC LIMIT 14""",
                params,
            ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {
        "totals": totals,
        "envs": [{"agent": r[0], "sessions": r[1], "tool_calls": r[2] or 0,
                  "tool_errors": r[3] or 0} for r in env_rows],
        "daily": [{"day": r[0], "sessions": r[1]} for r in daily_rows],
    }


def summary_cache_info(session_ids):
    """{session_id: cache info} from .summary.jsonl files next to each session.

    File-backed sessions only (virtual schemes have no cache location).
    latest_* fields describe the most recent record; source_mtime lets the
    caller compare against the session's jsonl_mtime to detect
    "session grew since it was last analyzed".
    """
    out = {}
    conn = _connect()
    if conn is None:
        return out
    try:
        rows = conn.execute(
            "SELECT id, jsonl_path FROM sessions WHERE id IN (%s)"
            % ",".join("?" * len(session_ids)),
            list(session_ids),
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    for sid, path in rows:
        if not path or "://" in path:
            continue
        sp = path + ".summary.jsonl"
        if not os.path.exists(sp):
            continue
        records = []
        try:
            with open(sp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
        if not records:
            continue
        records.sort(key=lambda r: str(r.get("analyzed_at") or ""))
        latest = records[-1]
        out[sid] = {
            "count": len(records),
            "latest_at": str(latest.get("analyzed_at") or ""),
            "latest_intent": str(latest.get("query_intent") or ""),
            "latest_source_mtime": latest.get("source_mtime"),
            "latest_analysis": str(latest.get("analysis") or "")[:120],
        }
    return out


def last_build_age_hours():
    """Hours since the index was last built, or None when never built."""
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM index_meta WHERE key = 'last_build'"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    try:
        return (datetime.now().timestamp() - float(row[0])) / 3600.0
    except (ValueError, TypeError):
        return None
