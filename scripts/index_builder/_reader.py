"""Read layer over the prebuilt index (sessions + messages_fts tables).

Everything here reads ONLY the index — no JSONL parsing. Callers fall back
to echolib dispatch for sessions absent from the index.

Per-session evidence is an index-time projection (``_evidence``), not a read of
``messages_fts``: FTS5 keeps ``session_id`` UNINDEXED, so filtering on it scans
the whole content table. ``messages_fts`` is used for MATCH queries only.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

from index_builder._cjk import build_match_query
from index_builder._evidence import evidence_from_row
from index_builder._schema import DB_PATH, _SCHEMA_VERSION


def _connect():
    """Open the index, or None when it does not exist yet."""
    if not DB_PATH.exists():
        return None
    return sqlite3.connect(str(DB_PATH))


def quick_stats_from_index(session_ids):
    """Batch-read precomputed stats from the sessions table.

    Returns {session_id: {msgs, tools, errors, branch, tool_errors_json,
    user_evidence_json, summary, first_prompt, created}} — entries missing from
    the index are absent from the result (caller falls back to file parsing).
    One query for every id, carrying everything a caller needs to render a hit:
    the evidence projection rides along so no per-session read is required.
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
                       tool_errors_json, summary, first_prompt, created,
                       user_evidence_json
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
            "user_evidence_json": r[9] or "",
        }
        for r in rows
    }


def evidence_from_index(session_id, decisions=False, limit_msgs=15):
    """Evidence for one session, read from the sessions table alone.

    Kept for callers that hold only an id; the row is fetched here. Callers
    that already batch-read their rows (sd-recall, deep-analyze) should call
    ``_evidence.evidence_from_row`` directly and skip this query.
    """
    row = summary_row(session_id, "tool_errors_json", "user_evidence_json")
    return evidence_from_row(row, decisions=decisions, limit_msgs=limit_msgs)


def session_stats_by_path(paths):
    """{jsonl_path: {created, msgs, branch, jsonl_mtime}} for indexed paths.

    Exists because the two layers key sessions differently: the index uses
    ``{env}:{adapter_id}`` while ``list_sessions`` returns the adapter's bare id
    (measured: 0/200 id matches, 200/200 path matches for the same 200
    sessions). ``jsonl_path`` is the only key both sides agree on.

    ``sd-recall sessions`` needs created / message count / branch per row and
    got them by fully parsing every transcript — 1.36 s for 200 summaries. All
    three fields are already in the index; the caller compares ``jsonl_mtime``
    against the file's mtime and re-parses only the sessions that grew since
    the build, so the displayed numbers stay live.
    """
    out = {}
    if not paths:
        return out
    conn = _connect()
    if conn is None:
        return out
    try:
        unique = list(dict.fromkeys(paths))
        for start in range(0, len(unique), 900):  # < SQLite's 999 bind limit
            chunk = unique[start:start + 900]
            q = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""SELECT jsonl_path, created, user_messages, assistant_messages,
                           branch, jsonl_mtime
                    FROM sessions WHERE jsonl_path IN ({q})""",
                chunk,
            ).fetchall()
            for path, created, um, am, branch, mtime in rows:
                out[path] = {
                    "created": created or "",
                    "msgs": (um or 0) + (am or 0),
                    "branch": branch or "",
                    "jsonl_mtime": mtime,
                }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return out


def summary_row(session_id, *columns):
    """Fetch selected ``sessions`` columns for one id as a dict ({} when absent)."""
    conn = _connect()
    if conn is None:
        return {}
    try:
        cols = ", ".join(columns) or "id"
        row = conn.execute(
            f"SELECT {cols} FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    if not row:
        return {}
    return dict(zip([c.strip() for c in columns] or ["id"], row))


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
            "first_prompt, model, project_path, tool_errors_json, jsonl_mtime, "
            "user_evidence_json")
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


def schema_status():
    """``(stored, code)`` index schema versions, or ``(None, code)``.

    Read paths use this to say *why* a hit may be missing data instead of
    rendering an emptier answer: a readable index can still be written by an
    older build — an older installed copy of this tool rewrites the ledger on
    every run, so the DB can sit several versions behind the code reading it.
    """
    conn = _connect()
    if conn is None:
        return None, _SCHEMA_VERSION
    try:
        row = conn.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return None, _SCHEMA_VERSION
    finally:
        conn.close()
    return (row[0] if row else None), _SCHEMA_VERSION


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
