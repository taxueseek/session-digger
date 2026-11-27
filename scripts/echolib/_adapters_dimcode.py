"""Adapters for DIM (memory-summary) and DimCode (SQLite) — split from _adapters_zcode.py."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from echolib._claude import _normalize_timestamp
from echolib._helpers import (
    DIMCODE_DB_PATH,
    DIMCODE_DB_PATH as _DEFAULT_DIMCODE_DB_PATH,
    DIM_DIR,
    _as_int,
    _iter_jsonl,
    attach_cache_hit_rates,
    cap,
    discovery_only_active,
    get_ro_sqlite,
    sql_limit,
)
from echolib._models import normalize_model_name

_log = logging.getLogger(__name__)
_EDIT_TOOLS = frozenset({"edit_file", "write_file", "Edit", "Write", "MultiEdit"})


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
                    if jf.name == "backfill.jsonl":
                        for idx, rec in enumerate(_iter_jsonl(jf)):
                            st_val = rec.get("session_time", rec.get("timestamp", ""))
                            started = _normalize_timestamp(st_val) if st_val else ""
                            intent = str(rec.get("intent", ""))[:80] if rec.get("intent") else f"backfill #{idx+1}"
                            sid = f"{jf.stem}_{idx:03d}"
                            path = str(jf)
                            sessions.append({
                                "id": sid,
                                "session_id": sid,
                                "title": intent,
                                "created": started, "modified": "",
                                "message_count": 0, "path": path,
                                "full_path": path,
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
                        path = str(jf)
                        sessions.append({
                            "id": jf.stem,
                            "session_id": jf.stem,
                            "title": intent or f"DIM {jf.stem[:20]}",
                            "created": started, "modified": "",
                            "message_count": 0, "path": path,
                            "full_path": path,
                            "agent": "DIM", "model": "",
                        })
                except OSError:
                    continue
    if keyword:
        keyword_lower = keyword.lower()
        sessions = [s for s in sessions if keyword_lower in s.get("title", "").lower()]
    return cap(sessions, limit)


def dim_session_stats(session_path):
    """Stats for DIM memory-summary format."""
    from echolib._helpers import _empty_stats
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
            if isinstance(mp, list):
                models = [
                    str(e.get("model")) for e in mp
                    if isinstance(e, dict) and e.get("model")
                ]
                if models and stats["model"] in ("", "dim"):
                    stats["model"] = models[0]
            elif isinstance(mp, dict) and mp.get("model") and stats["model"] in ("", "dim"):
                stats["model"] = str(mp["model"])
        if rec.get("intent") and not stats["summary"]:
            stats["summary"] = str(rec["intent"])[:200]
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    attach_cache_hit_rates(stats, agent="dim")
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
            yield {"role": "USER", "timestamp": nt, "text": text}
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
            yield {"role": "ASSISTANT", "timestamp": nt, "text": "\n".join(parts)}
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
            if isinstance(action, str):
                name = action.strip()[:120] or "action"
                if tool_filter and name != tool_filter:
                    continue
                if errors_only:
                    continue
                yield {
                    "timestamp": nt,
                    "name": name[:80],
                    "status": "ok",
                    "key_input": name[:150],
                    "result_preview": "",
                }
                count += 1
                if limit and count >= limit:
                    return
                continue
            if not isinstance(action, dict):
                continue
            name = action.get("name", action.get("type", "action"))
            if tool_filter and name != tool_filter:
                continue
            if errors_only and not action.get("error"):
                continue
            status = "error" if action.get("error") else "ok"
            yield {
                "timestamp": nt, "name": str(name), "status": status,
                "key_input": str(action.get("input", action.get("args", "")))[:150],
                "result_preview": str(action.get("result", ""))[:80],
            }
            count += 1
            if limit and count >= limit:
                return


def dim_session_path(cwd, session_id=None):
    """Resolve DIM session path."""
    if session_id:
        p = Path(session_id)
        if p.is_file():
            return str(p)
        if DIM_DIR.exists():
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
def _resolve_dimcode_db_path() -> Path:
    """Respect monkeypatches on either _adapters_dimcode or _adapters_zcode."""
    if DIMCODE_DB_PATH != _DEFAULT_DIMCODE_DB_PATH:
        return Path(DIMCODE_DB_PATH)
    zmod = sys.modules.get("echolib._adapters_zcode")
    if zmod is not None:
        z_path = getattr(zmod, "DIMCODE_DB_PATH", None)
        if z_path is not None and z_path != _DEFAULT_DIMCODE_DB_PATH:
            return Path(z_path)
    return Path(DIMCODE_DB_PATH)


def _dimcode_db_connect():
    """Connect to DimCode SQLite DB via cached thread-local read-only pool."""
    return get_ro_sqlite(_resolve_dimcode_db_path())


def dimcode_list_sessions(cwd=None, limit=50, keyword=""):
    """List DimCode sessions from dimcode.sqlite."""
    conn = _dimcode_db_connect()
    if not conn:
        return []
    sessions = []
    count_msgs = not discovery_only_active()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.sessionId, s.title, s.cwd, s.createdAt, s.status,
               {msg_count} as msg_count
        FROM sessions s
        ORDER BY s.createdAt DESC
        LIMIT ?
    """.format(msg_count="(SELECT COUNT(*) FROM messages m WHERE m.sessionId = s.sessionId)"
                      if count_msgs else "0"), (sql_limit(limit * 2),))
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
        path = f"dimcode://{sid}"
        sessions.append({
            "id": sid,
            "session_id": sid,
            "title": title,
            "created": created, "modified": "",
            "message_count": msg_count,
            "path": path,
            "full_path": path,
            "agent": "DimCode", "model": "",
        })
        if limit and len(sessions) >= limit:
            break
    return sessions


def _dimcode_normalize_session_id(session_id):
    """Accept dimcode://URI, dimcode:id, or bare sessionId."""
    s = str(session_id or "")
    if s.startswith("dimcode://"):
        return s[len("dimcode://"):]
    if s.startswith("dimcode:"):
        return s.split(":", 1)[1]
    if s.startswith("dimcode:") is False and "/" not in s and s.startswith("sess_"):
        return s
    if ":" in s and s.split(":", 1)[0] in ("dimcode", "dim"):
        return s.split(":", 1)[1]
    return s


def _dimcode_tool_input_preview(inp) -> str:
    if isinstance(inp, dict):
        return (
            inp.get("command") or inp.get("CommandLine") or inp.get("file_path")
            or inp.get("path") or inp.get("pattern") or inp.get("query")
            or json.dumps(inp, ensure_ascii=False)
        )[:150]
    return str(inp or "")[:150]


def _dimcode_extract_tool_result_text(part: dict) -> str:
    c = part.get("content", "")
    if isinstance(c, str) and c.strip():
        return c.replace("\n", " ").replace("\t", " ").strip()[:400]
    if isinstance(c, list):
        texts = [str(b.get("text", "")) for b in c if isinstance(b, dict) and b.get("text")]
        if texts:
            return " ".join(texts).replace("\n", " ").replace("\t", " ").strip()[:400]
    sc = part.get("structuredContent")
    if isinstance(sc, dict):
        msg = sc.get("error") or sc.get("message") or sc.get("code") or ""
        if msg:
            return str(msg)[:400]
    return ""


def _dimcode_parse_messages_and_tools(cur, session_id: str, *, thinking_limit: int = 0):
    """Single SQL pass over messages producing messages, tools, user/assistant/tool/error counts."""
    cur.execute("""
        SELECT messageId, role, parts, toolMetadata, createdAt
        FROM messages
        WHERE sessionId = ?
        ORDER BY orderKey ASC
    """, (session_id,))

    messages = []
    calls = []  # (call_id, ts, name, key_input)
    results = {}  # call_id -> (is_error, preview)
    user_count = 0
    assistant_count = 0
    tool_count = 0
    error_count = 0
    files_edited = 0
    last_ts = ""

    for row in cur.fetchall():
        raw_role = (row["role"] or "").lower()
        ts = row["createdAt"] or ""
        if ts and ts > last_ts:
            last_ts = ts
        parts_raw = row["parts"]
        meta_raw = row["toolMetadata"] if "toolMetadata" in row.keys() else None
        meta_obj = {}
        if meta_raw:
            try:
                meta_obj = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw if isinstance(meta_raw, dict) else {})
            except (json.JSONDecodeError, ValueError):
                meta_obj = {}

        parsed = None
        if parts_raw:
            try:
                parsed = json.loads(parts_raw) if isinstance(parts_raw, str) else parts_raw
            except (json.JSONDecodeError, ValueError):
                parsed = None

        if raw_role == "user":
            user_count += 1
        elif raw_role == "assistant":
            assistant_count += 1
        elif raw_role in ("tool", "function"):
            tool_count += 1

        # Extract text + tool_use + tool_result from parts
        text_chunks = []
        items = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
        for p in items:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type", "")
            if ptype == "text":
                t = p.get("text", "")
                if t:
                    text_chunks.append(t)
            elif ptype == "thinking" and thinking_limit != -1:
                th = (p.get("thinking") or "").strip()
                if th:
                    if thinking_limit > 0:
                        th = th[:thinking_limit]
                    text_chunks.append(f"[THINKING] {th}")
            elif ptype in ("tool_use", "tool_call"):
                if ptype == "tool_use":
                    name = p.get("name") or "?"
                    cid = p.get("id") or ""
                    inp = p.get("input", {})
                else:
                    fn = p.get("function") if isinstance(p.get("function"), dict) else {}
                    name = fn.get("name") or p.get("name") or "?"
                    cid = p.get("id") or ""
                    inp = fn.get("arguments") or p.get("arguments") or ""
                if raw_role == "assistant":
                    tool_count += 1
                if name in _EDIT_TOOLS:
                    files_edited += 1
                key_inp = _dimcode_tool_input_preview(inp)
                text_chunks.append(f"[TOOL: {name}] {key_inp}".strip())
                calls.append((cid, ts, name, key_inp))
            elif ptype == "tool_result":
                cid = p.get("tool_use_id") or meta_obj.get("toolCallId") or ""
                is_err = bool(p.get("is_error")) or (meta_obj.get("status") in ("error", "failed"))
                sc = p.get("structuredContent")
                if not is_err and isinstance(sc, dict) and (sc.get("status") == "failed" or sc.get("success") is False):
                    is_err = True
                if is_err:
                    error_count += 1
                preview = _dimcode_extract_tool_result_text(p)
                if cid:
                    results[cid] = (is_err, preview)

        if not items and parts_raw and raw_role in ("user", "assistant"):
            text_chunks.append(str(parts_raw)[:200])

        if raw_role in ("user", "assistant"):
            joined = "\n".join(c for c in text_chunks if c).strip()
            if joined:
                messages.append({
                    "role": "USER" if raw_role == "user" else "ASSISTANT",
                    "timestamp": ts,
                    "text": joined,
                })

    tools = []
    for cid, ts, name, key_inp in calls:
        is_err, preview = results.get(cid, (False, ""))
        tools.append({
            "timestamp": ts,
            "name": name or "[tool]",
            "status": "error" if is_err else "ok",
            "key_input": key_inp,
            "result_preview": preview,
        })

    return {
        "messages": messages,
        "tools": tools,
        "user_messages": user_count,
        "assistant_messages": assistant_count,
        "tool_calls": tool_count,
        "errors": error_count,
        "files_edited": files_edited,
        "last_ts": last_ts,
    }


def dimcode_parse_session(session_id, *, thinking_limit: int = 0):
    """Single-pass DimCode SQLite parser returning {'stats', 'messages', 'tools'}."""
    from echolib._helpers import _empty_stats

    session_id = _dimcode_normalize_session_id(session_id)
    conn = _dimcode_db_connect()
    if not conn:
        return {"stats": _empty_stats("dimcode"), "messages": [], "tools": []}

    stats = _empty_stats("dimcode")
    stats["model"] = ""
    messages = []
    tools = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT title, createdAt, updatedAt, cwd FROM sessions WHERE sessionId = ?", (session_id,))
        row = cur.fetchone()
        if row:
            stats["slug"] = session_id[:20]
            stats["summary"] = (row["title"] or "")[:200]
            stats["started"] = row["createdAt"] or ""
            try:
                stats["ended"] = row["updatedAt"] or ""
            except (IndexError, KeyError, TypeError):
                stats["ended"] = ""
            cwd = row["cwd"] if "cwd" in row.keys() else ""
            if cwd:
                stats["project"] = os.path.basename(str(cwd).rstrip("/"))

        parsed = _dimcode_parse_messages_and_tools(cur, session_id, thinking_limit=thinking_limit)
        messages = parsed["messages"]
        tools = parsed["tools"]
        stats["user_messages"] = parsed["user_messages"]
        stats["assistant_messages"] = parsed["assistant_messages"]
        stats["tool_calls"] = parsed["tool_calls"]
        stats["errors"] = parsed["errors"]
        stats["files_edited"] = parsed["files_edited"]
        if parsed["last_ts"] and not stats.get("ended"):
            stats["ended"] = parsed["last_ts"]

        # Role from session_relations (optional table in minimal test DBs)
        try:
            rel_row = cur.execute(
                "SELECT relationType FROM session_relations WHERE targetSessionId = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            if rel_row and (rel_row[0] or "").lower() == "subagent":
                stats["session_role"] = "subagent"
            elif "subagent" in session_id:
                stats["session_role"] = "subagent"
            else:
                stats["session_role"] = "main"
        except sqlite3.Error:
            if "subagent" in session_id:
                stats["session_role"] = "subagent"

        # Compactions from compaction_states (optional table)
        try:
            comp_row = cur.execute(
                "SELECT COUNT(*) FROM compaction_states WHERE sessionId = ?",
                (session_id,),
            ).fetchone()
            if comp_row:
                stats["compactions"] = _as_int(comp_row[0])
        except sqlite3.Error:
            pass

        # Token usage + primary model in one query
        try:
            urow = cur.execute("""
                SELECT COALESCE(SUM(inputTokens), 0) as inp,
                       COALESCE(SUM(outputTokens), 0) as out,
                       COALESCE(SUM(cacheReadTokens), 0) as cr,
                       COALESCE(SUM(cacheWriteTokens), 0) as cw
                FROM usage_run_stats WHERE sessionId = ?
            """, (session_id,)).fetchone()
            if urow:
                stats["input_tokens"] = _as_int(urow["inp"])
                stats["output_tokens"] = _as_int(urow["out"])
                stats["cache_read_tokens"] = _as_int(urow["cr"])
                stats["cache_create_tokens"] = _as_int(urow["cw"])
        except sqlite3.Error:
            # Older schema without cache columns
            try:
                urow = cur.execute("""
                    SELECT COALESCE(SUM(inputTokens), 0) as inp,
                           COALESCE(SUM(outputTokens), 0) as out
                    FROM usage_run_stats WHERE sessionId = ?
                """, (session_id,)).fetchone()
                if urow:
                    stats["input_tokens"] = _as_int(urow["inp"])
                    stats["output_tokens"] = _as_int(urow["out"])
            except sqlite3.Error as exc:
                _log.debug("dimcode usage_run_stats failed for %s: %s", session_id, exc)

        # Primary model from usage_run_stats, fallback to session_states
        try:
            mrow = cur.execute("""
                SELECT modelId, SUM(COALESCE(inputTokens, 0)) as w
                FROM usage_run_stats
                WHERE sessionId = ? AND modelId IS NOT NULL AND modelId != ''
                GROUP BY modelId ORDER BY w DESC LIMIT 1
            """, (session_id,)).fetchone()
            if mrow and mrow[0]:
                stats["model"] = normalize_model_name(str(mrow[0]))
        except sqlite3.Error:
            pass
        if not stats["model"]:
            try:
                srow = cur.execute(
                    "SELECT selectedModelId FROM session_states WHERE sessionId = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
                if srow and srow[0]:
                    stats["model"] = normalize_model_name(str(srow[0]))
            except sqlite3.Error:
                pass
    except sqlite3.Error as exc:
        _log.debug("dimcode_parse_session failed for %s: %s", session_id, exc)

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return {"stats": stats, "messages": messages, "tools": tools}


def dimcode_session_stats(session_id):
    """Get stats for a DimCode session from SQLite."""
    return dimcode_parse_session(session_id, thinking_limit=-1)["stats"]


def dimcode_extract_messages(session_id, role="both", limit=0, thinking_limit=0):
    """Extract messages from a DimCode session via SQLite."""
    session_id = _dimcode_normalize_session_id(session_id)
    conn = _dimcode_db_connect()
    if not conn:
        return
    count = 0
    cur = conn.cursor()
    cur.execute("""
        SELECT messageId, role, parts, createdAt
        FROM messages
        WHERE sessionId = ?
        ORDER BY orderKey ASC
    """, (session_id,))
    for row in cur.fetchall():
        raw_role = (row["role"] or "user").lower()
        if raw_role not in ("user", "assistant"):
            continue
        msg_role = raw_role.upper()
        if role not in ("both", raw_role, msg_role):
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
                        if not isinstance(p, dict):
                            continue
                        ptype = p.get("type")
                        if ptype == "text":
                            texts.append(p.get("text", ""))
                        elif ptype == "thinking" and thinking_limit != -1:
                            th = (p.get("thinking") or "").strip()
                            if th:
                                if thinking_limit > 0:
                                    th = th[:thinking_limit]
                                texts.append(f"[THINKING] {th}")
                        elif ptype in ("tool_use", "tool_call"):
                            fn = p.get("name") or (p.get("function") or {}).get("name", "?")
                            texts.append(f"[TOOL: {fn}]")
                    text = "\n".join(texts)
                elif isinstance(parsed, dict) and parsed.get("type") == "text":
                    text = parsed.get("text", "")
            except (json.JSONDecodeError, ValueError):
                text = str(parts)[:200]
        if not text:
            continue
        yield {"role": msg_role, "timestamp": ts, "text": text}
        count += 1
        if limit and count >= limit:
            return


def dimcode_extract_tools(session_id, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls and outcomes from DimCode session via SQLite."""
    session_id = _dimcode_normalize_session_id(session_id)
    conn = _dimcode_db_connect()
    if not conn:
        return
    parsed = _dimcode_parse_messages_and_tools(conn.cursor(), session_id, thinking_limit=-1)
    count = 0
    for t in parsed["tools"]:
        if tool_filter and t["name"] != tool_filter:
            continue
        if errors_only and t["status"] != "error":
            continue
        yield t
        count += 1
        if limit and count >= limit:
            return


def dimcode_session_path(cwd, session_id=None):
    """Resolve DimCode session identifier."""
    if session_id and str(session_id).startswith("dimcode://"):
        return str(session_id)
    return f"dimcode://{session_id}" if session_id else str(_resolve_dimcode_db_path())


def dimcode_skill_events(days: int | None = None) -> list[dict]:
    """Read native skill invocation counts from DimCode's usage_skill_events table."""
    conn = _dimcode_db_connect()
    if not conn:
        return []
    try:
        where = ""
        params = []
        if days and days > 0:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
            where = "WHERE createdAt >= ?"
            params.append(cutoff)
        rows = conn.execute(
            f"""SELECT skillName, COUNT(*) as calls, COUNT(DISTINCT sessionId) as sessions,
                       MAX(createdAt) as last_used
                FROM usage_skill_events {where}
                GROUP BY skillName ORDER BY calls DESC""",
            params,
        ).fetchall()
        return [
            {"skill": r[0], "calls": r[1], "sessions": r[2], "last_used": r[3] or ""}
            for r in rows if r[0]
        ]
    except sqlite3.Error:
        return []
