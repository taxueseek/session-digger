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


# ── 时间窗判据：一处定义 ────────────────────────────────────────────────
# 「近 N 天」在本仓库有 5 个副本（这里两处、trend-engine、skill-gap-finder、
# index-builder 各一处）。副本不可怕，漂移才可怕：`recent_sessions` 曾经额外
# `OR created = ''`，把**没有日期**的会话算进任何窗口，于是同一份报告里
# 「近 7 天共 903 个会话」（global_aggregates）与「近 7 天候选 962 个」
# （recent_sessions）互相矛盾——差值恰好是 59 个无日期会话。无日期不等于
# 「在窗口内」，两者都按 `created >= ?` 判，数字才能相加。
WINDOW_PREDICATE = "created >= ?"


def is_virtual_path(path):
    """``zcode://`` / ``dimcode://`` 这类 URI 不是磁盘路径。

    ``os.path.exists`` / ``os.path.getmtime`` 对它们一律返回 False / 抛 OSError，
    所以任何「文件不在就跳过」的过滤都会把整片会话判成「不存在」。判据与
    index_builder._builder 的 ``_prune_stale_sessions`` 同形：非 ``file:`` 的
    scheme 即虚拟。
    """
    p = str(path)
    return "://" in p and not p.startswith("file:")


def is_index_row_fresh(path, jsonl_mtime):
    """索引行是否仍然描述磁盘上的那个文件？

    这条规则只能有一个家。转录文件在构建之后又被写过，该行**所有**派生列就都
    过期了；诚实的回答是「去问文件」，而不是「给你一小时前的数字」。渲染陈旧
    计数的调用方与渲染实时计数的调用方长得一模一样，所以它不能由调用方各自重写。

    虚拟路径（DB 支撑的环境）没有文件可以比对：它的真源就是那个库，构建期的
    指纹由 ``_dimcode_session_fingerprints`` 负责，读侧只能采信索引行。把它们
    一并当成「不新鲜」，会让这些会话在**每一个**读侧永远落回解析——正是这条
    路径把 topic-scan 拖回 7.5 秒。
    """
    if is_virtual_path(path):
        return True
    if not jsonl_mtime:
        return False
    try:
        file_mtime = os.path.getmtime(path)
    except OSError:
        return False
    return abs(file_mtime - jsonl_mtime) < 1


# topic-scan 要的字段：一次批量读齐。读写共用同一份列名，避免「同一个会话在
# 两个视图里显示不同数字」——两份 SELECT 一旦漂移，谁也不会报错。
BUNDLE_COLUMNS = (
    "created", "modified", "user_messages", "assistant_messages",
    "tool_calls", "errors", "total_tokens", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "summary", "first_prompt",
    "model", "branch", "tool_usage_json", "user_evidence_json", "jsonl_mtime",
)


def fresh_session_bundles(paths):
    """{jsonl_path: 行字典} —— 只含「索引里有、且文件没再动过」的会话。

    按路径为键：两层对 id 的口径不同（见 ``session_stats_by_path``），
    ``jsonl_path`` 是双方唯一都认的键。索引没见过的路径、以及构建之后又长过
    的文件，直接**缺席**——调用方原有的「索引里没有 → 解析文件」分支正好覆盖
    这两种情况，不需要第二套判据。
    """
    out = {}
    paths = [p for p in dict.fromkeys(paths or []) if p]
    if not paths:
        return out
    conn = _connect()
    if conn is None:
        return out
    try:
        # 只取这个库**真的有的**列。代码可以比最后一次构建新（v5 的库里多出
        # output_tokens，而线上库还停在 v4），一个陌生列名会让整条 SELECT 直接
        # 失败——于是「快路径」静默停用到下一次重建，谁也不会发现。缺的列留
        # None，由调用方按「这个字段还没算过」处理。
        available = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        cols = [c for c in BUNDLE_COLUMNS if c in available]
        if not cols:
            return out
        for start in range(0, len(paths), 900):  # < SQLite 的 999 绑定上限
            chunk = paths[start:start + 900]
            q = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT jsonl_path, {', '.join(cols)} FROM sessions "
                f"WHERE jsonl_path IN ({q})",
                chunk,
            ).fetchall()
            for row in rows:
                path = row[0]
                bundle = dict.fromkeys(BUNDLE_COLUMNS)
                bundle.update(dict(zip(cols, row[1:])))
                if is_index_row_fresh(path, bundle.get("jsonl_mtime")):
                    out[path] = bundle
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return out


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
                clauses.append(WINDOW_PREDICATE)
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
            clauses.append(WINDOW_PREDICATE)
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
