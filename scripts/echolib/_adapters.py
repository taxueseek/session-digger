from __future__ import annotations

import json
import os
import re
import sqlite3
import concurrent.futures
import urllib.parse
from pathlib import Path

from echolib._claude import (
    _fast_find_jsonl,
    _normalize_timestamp,
    broad_list_claude_sessions,
    detect_agent_type,
    extract_messages,
    extract_tools,
    find_project_dir,
    session_stats,
)
from echolib._helpers import (
    CLAUDE_DIR,
    CODEX_DIR,
    CODEX_ROLLOUT_RE,
    CURSOR_DIR,
    DIMCODE_DB_PATH,
    DIM_DIR,
    GROK_DIR,
    GROK_SEARCH_DB,
    KIMI_CODE_DIR,
    KIMI_DIR,
    REASONIX_DIR,
    TRAE_DIR,
    WORKBUDDY_DIR,
    ZCODE_DIR,
    _codex_home,
    _codex_homes,
    _extract_content_text,
    _iter_jsonl,
    _match_call_results,
    _strip_system_reminder,
    attach_cache_hit_rates,
    compute_cache_hit_rate,
)
from echolib._models import (
    Record,
    SessionMeta,
)
# 模块级 logger：bare except 吞噬异常时保留一行痕迹，便于排查
# 设计原则是「静默不能吃错；至少留给懂的人一条线索」。
import logging as _logging
_log = _logging.getLogger("echolib.adapters")





def _encode_grok_cwd(cwd):
    """Encode a path to Grok's URL-encoded format."""
    return urllib.parse.quote(cwd, safe='')


def _decode_grok_cwd(encoded):
    """Decode Grok's URL-encoded path back to filesystem path."""
    return urllib.parse.unquote(encoded)


def _resolve_grok_project_cwd(group_dir):
    """Resolve a Grok sessions group directory to the original project cwd.

    Official layout (user-guide 17-sessions):
    - Normal: directory name is URL-encoded cwd
    - Long paths (>255 bytes): slug+hash directory + sibling ``.cwd`` file
      with the original path. Prefer ``.cwd`` when present.
    """
    group_dir = Path(group_dir)
    cwd_file = group_dir / ".cwd"
    if cwd_file.is_file():
        try:
            text = cwd_file.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return _decode_grok_cwd(group_dir.name)


def _grok_session_dir_for(session_cwd, session_id):
    """Locate session dir: URL-encoded name first, then ``.cwd`` group scan."""
    encoded = _encode_grok_cwd(session_cwd)
    direct = GROK_DIR / encoded / session_id
    if direct.is_dir():
        return direct
    # Long-path / rewritten groups: match via .cwd content
    if GROK_DIR.is_dir():
        for group in GROK_DIR.iterdir():
            if not group.is_dir():
                continue
            if _resolve_grok_project_cwd(group) != session_cwd:
                continue
            candidate = group / session_id
            if candidate.is_dir():
                return candidate
    return direct  # may not exist; caller handles


def grok_list_sessions(cwd=None, limit=50, keyword=""):
    """
    List Grok sessions. Uses session_search.sqlite if available,
    otherwise falls back to scanning summary.json files.

    Returns list of SessionMeta (reusing the existing class).
    """
    if not GROK_DIR.exists():
        return []

    # Try SQLite FTS5 search first
    if GROK_SEARCH_DB.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(GROK_SEARCH_DB))
            if keyword:
                rows = conn.execute("""
                    SELECT s.session_id, s.cwd, s.updated_at, s.title, s.content
                    FROM session_docs s
                    JOIN session_docs_fts fts ON s.rowid = fts.rowid
                    WHERE session_docs_fts MATCH ?
                    ORDER BY s.updated_at DESC LIMIT ?
                """, (keyword, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT session_id, cwd, updated_at, title, content
                    FROM session_docs
                    ORDER BY updated_at DESC LIMIT ?
                """, (limit,)).fetchall()
            conn.close()

            entries = []
            for row in rows:
                sid, session_cwd, updated_at, title, content = row
                # Filter by cwd if specified
                if cwd and session_cwd != cwd:
                    continue
                # Prefer on-disk path (handles .cwd long-path groups)
                full_path = str(_grok_session_dir_for(session_cwd, sid))
                created = updated_at
                entries.append(SessionMeta(
                    session_id=sid,
                    full_path=full_path,
                    created=_normalize_timestamp(updated_at),
                    modified=_normalize_timestamp(updated_at),
                    message_count=0,
                    git_branch="",
                    summary=title or "",
                    first_prompt=(content or "")[:100],
                    project_path=session_cwd,
                ))
            return entries
        except Exception as exc:  # 磁盘/权限坏 → 留痕 + 降级
            _log.warning("grok fallback index load failed: %s", exc, exc_info=True)

    # Fallback: scan summary.json files
    entries = []
    for d in sorted(GROK_DIR.iterdir()):
        if not d.is_dir():
            continue
        session_cwd = _resolve_grok_project_cwd(d)
        if cwd and session_cwd != cwd:
            continue
        for session_dir in sorted(d.iterdir()):
            if not session_dir.is_dir():
                continue
            summary_file = session_dir / "summary.json"
            if not summary_file.exists():
                continue
            try:
                with open(summary_file, encoding="utf-8") as f:
                    summary = json.load(f)
                info = summary.get("info", {})
                sid = info.get("id") or summary.get("session_id") or session_dir.name
                created = info.get("created_at") or summary.get("created_at") or ""
                updated = (
                    info.get("updated_at")
                    or summary.get("updated_at")
                    or summary.get("last_active_at")
                    or ""
                )
                title = (summary.get("session_summary")
                         or summary.get("generated_title")
                         or summary.get("summary")
                         or "")
                entries.append(SessionMeta(
                    session_id=sid,
                    full_path=str(session_dir),
                    created=_normalize_timestamp(created),
                    modified=_normalize_timestamp(updated),
                    message_count=summary.get("num_messages") or summary.get("num_chat_messages") or 0,
                    git_branch="",
                    summary=title,
                    first_prompt="",
                    project_path=session_cwd,
                ))
            except (json.JSONDecodeError, OSError):
                continue

    entries.sort(key=lambda e: str(e.created), reverse=True)
    return entries[:limit]

def _grok_join_content(content):
    """Join Grok content into a single string, handling char arrays and text blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)  # char-array defense
            elif isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "").strip()
                if t:
                    parts.append(t)
        return "".join(parts)  # join without spaces for char arrays
    return ""

def grok_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """
    Extract tool calls from a Grok session.

    Strategy: chat_history.jsonl is the primary source — assistant messages
    contain tool_calls arrays with id/name/arguments, and tool_result messages
    contain the matching output by tool_call_id. We join these by ID.

    events.jsonl provides timestamps and outcome status as a supplement.

    Yields tool call dicts: {timestamp, name, status, key_input, result_preview}.
    """
    session_dir = Path(session_dir)
    events_file = session_dir / "events.jsonl"
    chat_file = session_dir / "chat_history.jsonl"

    # --- Phase 1: Collect timestamps and outcomes from events.jsonl ---
    # Grok events don't have tool_call_id, so we match by sequential order:
    # each tool_started is followed by a tool_completed with the same tool_name.
    event_tools = []  # list of {ts, name, outcome}
    if events_file.exists():
        started_queue = []  # pending tool_started entries
        with open(events_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                etype = event.get("type", "")
                if etype == "tool_started":
                    name = event.get("tool_name", "")
                    ts = event.get("ts", "")
                    started_queue.append({"ts": ts, "name": name, "outcome": "success"})
                elif etype == "tool_completed":
                    name = event.get("tool_name", "")
                    outcome = event.get("outcome", "success")
                    # Match to the oldest pending started with same name
                    for i, s in enumerate(started_queue):
                        if s["name"] == name:
                            s["outcome"] = outcome
                            event_tools.append(started_queue.pop(i))
                            break
                    else:
                        # No matching started event — add anyway
                        event_tools.append({"ts": "", "name": name, "outcome": outcome})

    # --- Phase 2: Collect tool calls and results from chat_history.jsonl ---
    # assistant messages have tool_calls arrays; tool_result messages have
    # tool_call_id + content.
    tool_calls_list = []  # [{id, name, args}]
    results_by_id = {}   # tool_call_id -> preview
    if chat_file.exists():
        with open(chat_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                rtype = record.get("type", "")
                if rtype == "assistant":
                    calls = record.get("tool_calls", [])
                    if isinstance(calls, list):
                        for call in calls:
                            if not isinstance(call, dict):
                                continue
                            tid = call.get("id", "")
                            name = call.get("name", "")
                            args = call.get("arguments", "")
                            if isinstance(args, str):
                                key_input = args[:150]
                            elif isinstance(args, dict):
                                key_input = json.dumps(args, ensure_ascii=False)[:150]
                            else:
                                key_input = ""
                            tool_calls_list.append({"id": tid, "name": name, "key_input": key_input})
                elif rtype == "tool_result":
                    tid = record.get("tool_call_id", "")
                    rc = record.get("content", "")
                    if isinstance(rc, str):
                        preview = rc[:150].replace("\n", " ").replace("\t", " ")
                    elif isinstance(rc, list):
                        preview = " ".join(
                            b.get("text", "")[:100]
                            for b in rc if isinstance(b, dict)
                        )
                    else:
                        preview = ""
                    results_by_id[tid] = preview

    # --- Phase 3: Yield tool calls, joining with results and event timestamps ---
    # Match event_tools by sequential order (same count expected)
    count = 0
    for i, tc in enumerate(tool_calls_list):
        name = tc["name"]
        if tool_filter and name != tool_filter:
            continue

        # Get timestamp and outcome from event_tools (sequential match)
        ts = ""
        outcome = "success"
        if i < len(event_tools):
            ts = event_tools[i].get("ts", "")
            outcome = event_tools[i].get("outcome", "success")

        status = "error" if outcome in ("failure", "error") else "ok"

        # Get result preview by tool_call_id
        preview = results_by_id.get(tc["id"], "(no result)")

        # Also check result content for error indicators
        if status == "ok" and preview != "(no result)":
            if "Exit Code:" in preview and "Exit Code: 0" not in preview:
                status = "error"

        if errors_only and status != "error":
            continue

        if limit and count >= limit:
            return
        yield {
            "timestamp": ts,
            "name": name,
            "status": status,
            "key_input": tc["key_input"],
            "result_preview": preview,
        }
        count += 1

def grok_session_path(cwd, session_id=None):
    """
    Find a Grok session directory by CWD and optional session ID.

    Returns the session directory Path, or None if not found.
    Honour URL-encoded groups and long-path groups with ``.cwd``.
    """
    if not GROK_DIR.exists():
        return None

    if session_id:
        session_dir = _grok_session_dir_for(cwd, session_id)
        return session_dir if session_dir.is_dir() else None

    # Collect all group dirs that resolve to this cwd
    group_dirs = []
    encoded_cwd = _encode_grok_cwd(cwd)
    direct = GROK_DIR / encoded_cwd
    if direct.is_dir():
        group_dirs.append(direct)
    for group in GROK_DIR.iterdir():
        if not group.is_dir() or group in group_dirs:
            continue
        if _resolve_grok_project_cwd(group) == cwd:
            group_dirs.append(group)

    if not group_dirs:
        return None

    sessions = []
    for cwd_dir in group_dirs:
        sessions.extend(d for d in cwd_dir.iterdir() if d.is_dir())
    sessions.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return sessions[0] if sessions else None

def kimi_list_sessions(cwd=None, limit=50, keyword=""):
    """
    List Kimi Code sessions.

    Scans ~/.kimi/sessions/<project_hash>/<session_uuid>/ directories.
    Returns list of SessionMeta.
    """
    if not KIMI_DIR.exists():
        return []

    entries = []
    for project_hash in sorted(KIMI_DIR.iterdir()):
        if not project_hash.is_dir():
            continue
        for session_dir in sorted(project_hash.iterdir()):
            if not session_dir.is_dir():
                continue

            meta_file = session_dir / "metadata.json"
            wire_file = session_dir / "wire.jsonl"

            if not meta_file.exists():
                continue

            try:
                with open(meta_file, encoding="utf-8") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            sid = meta.get("session_id", session_dir.name)
            title = (meta.get("title") or "")[:100]
            # wire_mtime is a Unix timestamp float
            wire_mtime = meta.get("wire_mtime")
            created = _normalize_timestamp(wire_mtime) if wire_mtime else ""

            # Quick message count from wire.jsonl if available
            msg_count = 0
            first_msg = title  # fallback first message from title
            if wire_file.exists():
                try:
                    with open(wire_file, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            try:
                                rec = json.loads(line.strip())
                                msg = rec.get("message", {})
                                if msg.get("type") == "TurnBegin":
                                    msg_count += 1
                                    if first_msg == title:  # extract first real user message
                                        payload = msg.get("payload", {})
                                        user_input = payload.get("user_input", [])
                                        for ui in (user_input if isinstance(user_input, list) else []):
                                            if isinstance(ui, dict) and ui.get("type") == "text":
                                                first_msg = ui["text"][:100].replace("\n", " ")
                                                break
                            except (json.JSONDecodeError, ValueError):
                                continue
                except OSError:
                    pass

            # Filter by keyword
            if keyword and keyword.lower() not in title.lower() and keyword.lower() not in first_msg.lower():
                continue

            # Filter by project path if cwd specified
            project_path = str(project_hash)
            if cwd and cwd not in str(session_dir):
                # Rough filter — Kimi doesn't store cwd in metadata
                pass

            entries.append(SessionMeta(
                session_id=sid,
                full_path=str(session_dir),
                created=created,
                modified=created,
                message_count=msg_count,
                git_branch="",
                summary=title,
                first_prompt=first_msg,
                project_path=project_path,
            ))

    entries.sort(key=lambda e: str(e.created), reverse=True)
    return entries[:limit]

def kimi_session_stats(session_dir):
    """
    Get session statistics for a standalone Kimi session (``~/.kimi/sessions``).

    Accepts either a session directory or a path to ``wire.jsonl``.
    Returns a dict compatible with session_stats().
    """
    p = Path(session_dir)
    if p.is_file():
        wire_file = p
        session_dir = p.parent
    else:
        wire_file = p / "wire.jsonl"
        session_dir = p
    state_file = session_dir / "state.json"
    meta_file = session_dir / "metadata.json"

    stats = {
        "slug": "",
        "model": "kimi",
        "branch": "",
        "started": "",
        "ended": "",
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
        "files_edited": "N/A",
        "errors": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "compactions": 0,
        "summary": "",
    }

    # Model from state.json
    if state_file.exists():
        try:
            with open(state_file, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("model"):
                stats["model"] = state["model"]
        except (json.JSONDecodeError, OSError):
            pass

    # Timestamps and title from metadata.json
    if meta_file.exists():
        try:
            with open(meta_file, encoding="utf-8") as f:
                meta = json.load(f)
            wire_mtime = meta.get("wire_mtime")
            if wire_mtime:
                stats["started"] = _normalize_timestamp(wire_mtime)
            stats["summary"] = (meta.get("title") or "")[:100]
        except (json.JSONDecodeError, OSError):
            pass

    # Count from wire.jsonl (+ StatusUpdate token_usage when present)
    if wire_file.exists():
        try:
            with open(wire_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line.strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = rec.get("message", {})
                    mt = msg.get("type", "")
                    if mt == "TurnBegin":
                        stats["user_messages"] += 1
                    elif mt in ("ContentPart", "ToolCallPart"):
                        stats["assistant_messages"] += 1
                    elif mt == "ToolCall":
                        stats["tool_calls"] += 1
                    elif mt == "StatusUpdate":
                        kp = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
                        tu = kp.get("token_usage") if isinstance(kp, dict) else None
                        if isinstance(tu, dict):
                            # input_other = non-cached leg (same shape as Kimi Code)
                            stats["cache_read_tokens"] += int(tu.get("input_cache_read") or 0)
                            stats["cache_create_tokens"] += int(tu.get("input_cache_creation") or 0)
                            stats["input_tokens"] += int(tu.get("input_other") or 0)
                            stats["output_tokens"] += int(tu.get("output") or 0)
        except OSError:
            pass

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    attach_cache_hit_rates(stats, input_includes_cache=False)
    return stats

def kimi_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """
    Extract human-readable messages from a Kimi Code session.

    Reads wire.jsonl. Kimi stores user input in TurnBegin.payload.user_input[].text
    and assistant text in ContentPart.payload.text.

    Yields dicts with keys: role, timestamp, text.
    """
    session_dir = Path(session_dir)
    wire_file = session_dir / "wire.jsonl"
    if not wire_file.exists():
        return

    count = 0
    with open(wire_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            raw_ts = rec.get("timestamp") or ""
            ts = _normalize_timestamp(raw_ts) if raw_ts else ""

            msg = rec.get("message", {})
            mt = msg.get("type", "")

            if mt == "TurnBegin" and role in ("user", "both"):
                payload = msg.get("payload", {})
                user_input = payload.get("user_input", [])
                parts = []
                for ui in (user_input if isinstance(user_input, list) else []):
                    if isinstance(ui, dict) and ui.get("type") == "text":
                        t = ui.get("text", "").strip()
                        if t:
                            parts.append(t)
                if parts:
                    yield {"role": "USER", "timestamp": ts, "text": "\n".join(parts)}
                    count += 1

            elif mt == "ContentPart" and role in ("assistant", "both"):
                payload = msg.get("payload", {})
                if payload.get("type") == "text":
                    text = payload.get("text", "").strip()
                    if text:
                        yield {"role": "ASSISTANT", "timestamp": ts, "text": text}
                        count += 1
                elif payload.get("type") == "tool_call":
                    name = payload.get("function", {}).get("name", "?")
                    yield {"role": "ASSISTANT", "timestamp": ts, "text": "[TOOL: {}]".format(name)}
                    count += 1

            if limit and count >= limit:
                return

def kimi_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """
    Extract tool calls from a Kimi Code session.

    Kimi stores ToolCall (name, id) and ToolResult (tool_call_id, return_value)
    in wire.jsonl. We join by tool_call_id.

    Yields tool call dicts: {timestamp, name, status, key_input, result_preview}.
    """
    session_dir = Path(session_dir)
    wire_file = session_dir / "wire.jsonl"
    if not wire_file.exists():
        return

    # Two-pass: collect ToolCall by id, then ToolResult by id
    calls = {}  # tool_call_id -> {name, ts, input_preview}
    results = {}  # tool_call_id -> result_preview

    with open(wire_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            raw_ts = rec.get("timestamp") or ""
            ts = _normalize_timestamp(raw_ts) if raw_ts else ""
            msg = rec.get("message", {})
            mt = msg.get("type", "")

            if mt == "ToolCall":
                payload = msg.get("payload", {})
                tid = payload.get("id", "")
                name = payload.get("function", {}).get("name", "")
                if tool_filter and name != tool_filter:
                    continue
                inp = payload.get("function", {}).get("arguments", "")
                if isinstance(inp, dict):
                    inp = json.dumps(inp, ensure_ascii=False)
                calls[tid] = {
                    "name": name,
                    "ts": ts,
                    "input_preview": str(inp)[:150] if inp else "",
                }

            elif mt == "ToolResult":
                payload = msg.get("payload", {})
                tid = payload.get("tool_call_id", "")
                rv = payload.get("return_value", "")
                if isinstance(rv, str):
                    results[tid] = rv[:150].replace("\n", " ")
                elif isinstance(rv, dict):
                    results[tid] = json.dumps(rv, ensure_ascii=False)[:150]
                else:
                    results[tid] = str(rv)[:150]

    count = 0
    for tid, info in sorted(calls.items()):
        name = info["name"]
        ts = info["ts"]
        inp = info["input_preview"]
        result = results.get(tid, "(no result)")
        status = "ok"  # Kimi doesn't expose error status in ToolResult format
        if errors_only:
            continue  # No error tracking in Kimi ToolResult; skip in errors_only mode

        if limit and count >= limit:
            return
        yield {
            "timestamp": ts,
            "name": name,
            "status": status,
            "key_input": inp,
            "result_preview": result,
        }
        count += 1

def kimi_session_path(cwd, session_id=None):
    """
    Find a Kimi Code session directory.

    Kimi doesn't store per-project CWD the same way Grok/Claude do,
    so this is a simpler lookup.
    """
    if not KIMI_DIR.exists():
        return None

    if session_id:
        for project_dir in KIMI_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            session_dir = project_dir / session_id
            if session_dir.is_dir():
                return session_dir
        return None

    return KIMI_DIR if KIMI_DIR.is_dir() else None

def _kimi_code_session_id(session_dir: Path) -> str:
    name = session_dir.name
    return name[8:] if name.startswith("session_") else name


def _kimi_code_wire_quick_scan(wire_file: Path):
    """First prompt + turn.prompt count from wire.jsonl."""
    msg_count = 0
    first_msg = ""
    for rec in _iter_jsonl(wire_file):
        if rec.get("type") != "turn.prompt":
            continue
        msg_count += 1
        if first_msg:
            continue
        inputs = rec.get("input", [])
        for inp in (inputs if isinstance(inputs, list) else []):
            if isinstance(inp, dict) and inp.get("type") == "text":
                first_msg = (inp.get("text") or "")[:200].replace("\n", " ")
                break
    return msg_count, first_msg


def kimi_code_list_sessions(cwd=None, limit=50, keyword=""):
    """
    List Kimi Code sessions from ~/.kimi-code/sessions/.

    Directory structure: ~/.kimi-code/sessions/<project_dir>/<session_uuid>/
    Each session has state.json + agents/main/wire.jsonl
    """
    if not KIMI_CODE_DIR.exists():
        return []

    entries = []
    keyword_l = keyword.lower() if keyword else ""
    cwd_n = os.path.normpath(cwd) if cwd else ""

    for project_dir in sorted(KIMI_CODE_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        try:
            session_dirs = list(project_dir.iterdir())
        except OSError:
            continue
        for session_dir in session_dirs:
            if not session_dir.is_dir():
                continue

            state_file = session_dir / "state.json"
            wire_file = session_dir / "agents" / "main" / "wire.jsonl"

            if not state_file.exists():
                continue

            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            work_dir = state.get("workDir") or state.get("cwd") or ""
            if cwd_n and work_dir:
                if os.path.normpath(work_dir) != cwd_n:
                    continue

            sid = _kimi_code_session_id(session_dir)
            title = (state.get("title") or "")[:100]
            created_at = _normalize_timestamp(state.get("createdAt", "")) or state.get("createdAt", "")
            updated_at = _normalize_timestamp(state.get("updatedAt", "")) or state.get("updatedAt", "")

            msg_count = 0
            first_msg = title
            if wire_file.exists():
                msg_count, wire_first = _kimi_code_wire_quick_scan(wire_file)
                if wire_first:
                    first_msg = wire_first
            if not first_msg:
                first_msg = (state.get("lastPrompt") or "")[:200]

            if keyword_l and keyword_l not in title.lower() and keyword_l not in first_msg.lower():
                continue

            entries.append(SessionMeta(
                session_id=sid,
                full_path=str(session_dir),
                created=created_at,
                modified=updated_at or created_at,
                message_count=msg_count,
                git_branch="",
                summary=title or first_msg[:100],
                first_prompt=first_msg[:200] if first_msg else "",
                project_path=work_dir or str(project_dir),
            ))

    entries.sort(key=lambda e: str(e.modified or e.created or ""), reverse=True)
    return entries[:limit]

def kimi_code_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """
    Extract tool calls from a Kimi Code session.

    Kimi stores tool calls as context.append_loop_event with event.type="tool.call".
    Tool results are in event.type="tool.result" with matching toolCallId.

    Yields tool call dicts: {timestamp, name, status, key_input, result_preview}.
    """
    resolved = _kimi_code_resolve_path(session_path)
    if not Path(resolved).exists():
        return

    # Single pass: collect calls + results, then match (Codex-style join)
    calls = {}
    results_by_id = {}
    for rec in _iter_jsonl(resolved):
        if rec.get("type") != "context.append_loop_event":
            continue
        event = rec.get("event", {})
        if not isinstance(event, dict):
            continue
        etype = event.get("type", "")
        ts = rec.get("time", rec.get("timestamp", ""))
        if etype == "tool.result":
            tid = event.get("toolCallId", event.get("parentUuid", ""))
            result = event.get("result", {})
            is_error = False
            preview = ""
            if isinstance(result, dict):
                is_error = bool(result.get("isError"))
                output = result.get("output", "")
                if isinstance(output, str):
                    preview = output[:150].replace("\n", " ").replace("\t", " ")
                    if "Exit Code:" in output and "Exit Code: 0" not in output:
                        is_error = True
            elif isinstance(result, str):
                preview = result[:150].replace("\n", " ").replace("\t", " ")
            if tid:
                results_by_id[tid] = {"preview": preview, "is_error": is_error}
        elif etype == "tool.call":
            name = event.get("name", "")
            if tool_filter and name != tool_filter:
                continue
            tid = event.get("toolCallId", event.get("uuid", ""))
            args = event.get("args", event.get("arguments", ""))
            if isinstance(args, dict):
                key_input = json.dumps(args, ensure_ascii=False)[:150]
            elif isinstance(args, str):
                key_input = args[:150]
            else:
                key_input = ""
            calls[tid or f"anon-{len(calls)}"] = {
                "name": name,
                "ts": _normalize_timestamp(ts) if ts else "",
                "input_preview": key_input,
            }

    # Adapt to _match_call_results shape
    yield from _match_call_results(calls, results_by_id, errors_only=errors_only, limit=limit)

def kimi_code_session_path(cwd, session_id=None):
    """
    Find a Kimi Code session directory.
    """
    if not KIMI_CODE_DIR.exists():
        return None

    if session_id:
        for project_dir in KIMI_CODE_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            # Try with and without session_ prefix
            for sid in [session_id, f"session_{session_id}"]:
                session_dir = project_dir / sid
                if session_dir.is_dir():
                    return session_dir
        return None

    return KIMI_CODE_DIR if KIMI_CODE_DIR.is_dir() else None

def cross_tool_list_sessions(limit=50, keyword="", agent_filter=None):
    """
    跨工具列出所有会话，并行处理多个适配器以提高性能。

    Default excludes ``universal``: it is a fallback parser for unknown paths,
    not a peer environment. Including it floods results with home-tree noise
    (e.g. memtrace) that outranks real Claude/Codex/Cursor sessions by mtime.
    Pass agent_filter=\"universal\" or a list containing it when needed.
    """
    if isinstance(agent_filter, (list, tuple)):
        agents = list(agent_filter)
    elif agent_filter is None:
        agents = [name for name in ADAPTER_REGISTRY if name != "universal"]
    else:
        agents = [agent_filter]

    all_sessions = []
    # 每个适配器请求更多结果，确保全局排序后各环境都有代表
    per_adapter_limit = max(limit * 2, 20)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for name in agents:
            if name not in ADAPTER_REGISTRY:
                continue
            adapter = ADAPTER_REGISTRY[name]
            fn = adapter["list_sessions"]
            futures[executor.submit(fn, limit=per_adapter_limit, keyword=keyword)] = name

        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                sessions = future.result()
                display = ADAPTER_REGISTRY[name]["display_name"]
                for s in sessions:
                    # Handle both dict and object return types.
                    # Prefer registry id for agent (stable machine key); keep
                    # display name for human-facing UIs.
                    if isinstance(s, dict):
                        all_sessions.append({
                            "agent": name,
                            "agent_display": s.get("agent_display", s.get("agent", display)),
                            "session_id": s.get("session_id", s.get("id", "")),
                            "created": s.get("created", ""),
                            "summary": s.get("summary", s.get("title", "")),
                            "first_prompt": s.get("first_prompt", ""),
                            "msg_count": s.get("msg_count", s.get("message_count", 0)),
                            "full_path": s.get("full_path", s.get("path", "")),
                        })
                    else:
                        all_sessions.append({
                            "agent": name,
                            "agent_display": display,
                            "session_id": s.session_id,
                            "created": s.created,
                            "summary": s.summary,
                            "first_prompt": s.first_prompt,
                            "msg_count": s.message_count,
                            "full_path": s.full_path,
                        })
            except Exception as exc:
                # One bad adapter must not blank the whole cross view
                _log.warning("cross_tool list_sessions failed for %s: %s", name, exc)

    # Fair merge: pure global mtime sort lets one hot agent (e.g. Grok memtrace
    # noise historically, or a busy env) occupy the entire top-N and hide Claude
    # /Codex/Cursor. Round-robin by agent keeps every environment visible.
    by_agent = {}
    for s in all_sessions:
        by_agent.setdefault(s.get("agent") or "?", []).append(s)
    for agent, items in by_agent.items():
        with_time = [x for x in items if x.get("created")]
        without_time = [x for x in items if not x.get("created")]
        with_time.sort(key=lambda x: str(x["created"]), reverse=True)
        by_agent[agent] = with_time + without_time

    result = []
    cursors = {agent: 0 for agent in by_agent}
    while len(result) < limit and cursors:
        progress = False
        for agent in list(cursors.keys()):
            idx = cursors[agent]
            bucket = by_agent[agent]
            if idx >= len(bucket):
                del cursors[agent]
                continue
            result.append(bucket[idx])
            cursors[agent] = idx + 1
            progress = True
            if len(result) >= limit:
                break
        if not progress:
            break
    return result

def cross_tool_session_stats(session_path):
    """
    Get statistics for a session from any supported agent.
    Auto-detects agent type and dispatches via registry.
    """
    return dispatch_session_stats(session_path)

def dispatch_resolve_agent(path):
    """Detect agent type and map to a registered adapter name.

    Resolution order (confidence-gated — lesson from specialized adapters):
    1. **Path** markers / filename cues (highest trust)
    2. **Structural** JSON signatures only (Claude/Codex/Cursor/Kimi shapes)
       — never free-text model-name substrings (user saying "sonnet" ≠ Claude)
    3. **universal** SchemaProbe for everything else (unknown / weird envs)

    This is the "one schema to rule them" gate: dedicated adapters when sure,
    dynamic probe when not.
    """
    atype = detect_agent_type(path)
    if atype and atype not in ("unknown", "both") and atype in ADAPTER_REGISTRY:
        return atype
    # Structural content only (high score thresholds inside the detector)
    detected = _detect_format_from_content(path)
    if detected and detected in ADAPTER_REGISTRY:
        return detected
    return "universal"

def _detect_format_from_content(path):
    """Quick content-based format detection by sampling first 20 lines.

    Returns adapter name or None. This is a lightweight version of
    format-detector.py that can be called without subprocess overhead.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= 20:
                    break
                lines.append(line)
    except OSError:
        return None

    # Score each known format
    best_format = None
    best_score = 0

    # Claude Code: sessionId/uuid, cwd/gitBranch, toolUseResult, message.model
    # Use unique indicator set — not per-record scoring — to avoid false positives
    # from environments that share some fields (e.g. deepcode has sessionId but no type/cwd)
    claude_indicators = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            score = 0
            break
        if isinstance(obj, dict):
            if "sessionId" in obj or "uuid" in obj:
                claude_indicators.add("session_id")
            if obj.get("type") in ("user", "assistant", "system", "tool_use", "tool_result"):
                claude_indicators.add("type_field")
            if "cwd" in obj or "gitBranch" in obj:
                claude_indicators.add("cwd")
            if "toolUseResult" in obj:
                claude_indicators.add("toolUseResult")
            msg = obj.get("message", {})
            if isinstance(msg, dict) and msg.get("model", "").startswith("claude"):
                claude_indicators.add("claude_model")
    # Score: type_field is required, plus at least one strong indicator
    score = 0
    if "type_field" in claude_indicators:
        score += 2
    if "cwd" in claude_indicators:
        score += 4
    if "toolUseResult" in claude_indicators:
        score += 4
    if "claude_model" in claude_indicators:
        score += 3
    if "session_id" in claude_indicators:
        score += 1
    if score > best_score:
        best_score = score
        best_format = "claude"

    # Grok: type=reasoning, assistant+tool_calls, model_id, synthetic_reason
    # Use unique indicator set — tool_result type alone is not Grok-specific
    # (Claude Code also uses tool_result)
    grok_indicators = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            grok_indicators = set()
            break
        if isinstance(obj, dict):
            rtype = obj.get("type", "")
            if rtype == "reasoning":
                grok_indicators.add("reasoning")
            if rtype == "assistant" and "tool_calls" in obj:
                grok_indicators.add("assistant_tool_calls")
            if "model_id" in obj:
                grok_indicators.add("model_id")
            if "synthetic_reason" in obj:
                grok_indicators.add("synthetic_reason")
    score = 0
    if "reasoning" in grok_indicators:
        score += 4
    if "assistant_tool_calls" in grok_indicators:
        score += 4
    if "model_id" in grok_indicators:
        score += 3
    if "synthetic_reason" in grok_indicators:
        score += 2
    if score > best_score:
        best_score = score
        best_format = "grok"

    # Kimi Code: protocol_version, context.append_loop_event, turn.prompt
    # But NOT kimi non-code (which has protocol_version but uses TurnBegin/StepBegin/ContentPart)
    # Note: 'metadata' type is common to both formats (protocol-level), so it's not a kimi_code indicator
    score = 0
    has_kimi_code_types = False
    has_kimi_noncode_types = False
    kimi_code_specific = {"config.update", "turn.prompt", "context.append_loop_event",
                          "context.append_message", "tools.set_active_tools", "usage.record"}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            score = 0
            break
        if isinstance(obj, dict):
            rtype = obj.get("type", "")
            if rtype in kimi_code_specific:
                score += 2
                has_kimi_code_types = True
            if "protocol_version" in obj:
                score += 5
            # Detect kimi non-code message types (TurnBegin/StepBegin/ContentPart)
            msg = obj.get("message", {})
            if isinstance(msg, dict):
                msg_type = msg.get("type", "")
                if msg_type in ("TurnBegin", "StepBegin", "ContentPart", "ToolCallBegin", "ToolCallEnd"):
                    has_kimi_noncode_types = True
    # If kimi non-code types are present but kimi code types are not,
    # this is kimi non-code format — don't classify as kimi_code
    if has_kimi_noncode_types and not has_kimi_code_types:
        score = 0
    if score > best_score:
        best_score = score
        best_format = "kimi_code"

    # Codex: payload.type = function_call/message, timestamp
    score = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            score = 0
            break
        if isinstance(obj, dict):
            payload = obj.get("payload", {})
            if isinstance(payload, dict):
                ptype = payload.get("type", "")
                if ptype in ("message", "function_call", "reasoning"):
                    score += 2
            if obj.get("type") == "response_item" and "payload" in obj:
                score += 3
    if score > best_score:
        best_score = score
        best_format = "codex"

    # Cursor agent-transcript: top-level role + message.content tool_use.
    # Do NOT claim Cursor solely from <user_query> — Qoder/Claude-like exports
    # also wrap prompts that way (false positive → wrong dedicated adapter).
    score = 0
    cursor_roles = set()
    has_user_query = False
    has_tool_use = False
    has_top_type = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            score = 0
            break
        if isinstance(obj, dict):
            if obj.get("type") in ("user", "assistant", "system"):
                has_top_type = True
            role = obj.get("role")
            if isinstance(role, str):
                cursor_roles.add(role.lower())
            msg = obj.get("message")
            content = msg.get("content") if isinstance(msg, dict) else obj.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in ("tool_use", "tool_call"):
                        has_tool_use = True
                    text = block.get("text")
                    if isinstance(text, str) and "<user_query>" in text:
                        has_user_query = True
            elif isinstance(content, str) and "<user_query>" in content:
                has_user_query = True
    if cursor_roles & {"user", "assistant"} and not has_top_type:
        score += 2
    if has_tool_use and "user" in cursor_roles and not has_top_type:
        score += 5  # real Cursor agent-transcript signal
    if has_user_query and has_tool_use:
        score += 2
    if score > best_score:
        best_score = score
        best_format = "cursor"

    # Require solid structural evidence (≥5) for content-only routing.
    # Borderline scores fall through to universal SchemaProbe.
    return best_format if best_score >= 5 else None

def dispatch_session_stats(path) -> SessionStats:
    """Get session stats via the correct adapter for this session's agent."""
    agent = dispatch_resolve_agent(path)
    fn = ADAPTER_REGISTRY.get(agent, {}).get("session_stats")
    if fn:
        return fn(path)
    return session_stats(path)

def dispatch_extract_messages(path, role="both", no_tools=False, limit=0, thinking_limit=0):
    """Extract messages via the correct adapter for this session's agent.

    Handles per-adapter signature differences (e.g. grok has no no_tools arg).
    """
    agent = dispatch_resolve_agent(path)
    fn = ADAPTER_REGISTRY.get(agent, {}).get("extract_messages")
    if fn:
        return fn(path, role=role, limit=limit, thinking_limit=thinking_limit)
    return extract_messages(path, role=role, no_tools=no_tools, limit=limit, thinking_limit=thinking_limit)

def dispatch_extract_tools(path, errors_only=False, limit=0, tool_filter=""):
    """Extract tool calls via the correct adapter for this session's agent.

    Handles per-adapter path differences (e.g. grok expects session_dir, not
    the .jsonl file path).
    """
    agent = dispatch_resolve_agent(path)
    fn = ADAPTER_REGISTRY.get(agent, {}).get("extract_tools")
    if fn:
        if agent == "grok":
            # grok_extract_tools expects session_dir, not the .jsonl file path
            session_dir = str(Path(path).parent)
            return fn(session_dir, tool_filter=tool_filter, errors_only=errors_only, limit=limit)
        return fn(path, tool_filter=tool_filter, errors_only=errors_only, limit=limit)
    return extract_tools(path, tool_filter=tool_filter, errors_only=errors_only, limit=limit)

def _codex_id_from_path(path):
    """Extract full session UUID from a Codex rollout filename."""
    name = Path(path).name
    match = CODEX_ROLLOUT_RE.match(name)
    if match:
        return match.group(1)
    # Fallback: strip .zst then .jsonl suffixes and take trailing UUID-shaped tail
    stem = name
    if stem.endswith(".jsonl.zst"):
        stem = stem[:-10]
    elif stem.endswith(".jsonl"):
        stem = stem[:-6]
    if stem.startswith("rollout-") and len(stem) >= 36:
        return stem[-36:]
    return stem


def _iter_codex_rollouts():
    """Yield rollout paths under every known Codex home (plain + .zst)."""
    seen = set()
    for home in _codex_homes():
        sessions_dir = home / "sessions"
        if not sessions_dir.is_dir():
            continue
        # plain first, then compressed; reverse=True on mtime-ish name order
        for pattern in ("rollout-*.jsonl", "rollout-*.jsonl.zst"):
            try:
                paths = sorted(sessions_dir.rglob(pattern), reverse=True)
            except OSError:
                continue
            for p in paths:
                # Avoid double-counting: rglob('*.jsonl') also matches '*.jsonl.zst' on some globs?
                # pathlib: '*.jsonl' does NOT match '*.jsonl.zst' — good.
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                yield p


def codex_list_sessions(cwd=None, limit=50, keyword=""):
    """List Codex sessions from session_index.jsonl across CODEX_HOME + ~/.codex."""
    sessions = []
    seen_ids = set()
    index_found = False

    for home in _codex_homes():
        index_path = home / "session_index.jsonl"
        if not index_path.exists():
            continue
        index_found = True
        try:
            with open(index_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    sid = entry.get("id", "")
                    if not sid or sid in seen_ids:
                        continue
                    title = entry.get("thread_name", "") or ""
                    updated = entry.get("updated_at", "")

                    if keyword and keyword.lower() not in title.lower():
                        continue

                    rollout_path = _find_codex_rollout(sid)
                    msg_count = 0
                    first_prompt = ""
                    if rollout_path:
                        msg_count, first_prompt = _codex_quick_scan(rollout_path)

                    seen_ids.add(sid)
                    sessions.append(SessionMeta(
                        session_id=sid,
                        full_path=str(rollout_path) if rollout_path else "",
                        created=updated,
                        modified=updated,
                        message_count=msg_count,
                        git_branch="",
                        summary=title[:100] if title else "",
                        first_prompt=first_prompt[:200] if first_prompt else "",
                        project_path="",
                    ))
        except OSError:
            continue

    if not index_found:
        return codex_list_sessions_fallback(cwd, limit, keyword)

    sessions.sort(key=lambda s: str(s.created or ""), reverse=True)
    return sessions[:limit]

def codex_list_sessions_fallback(cwd=None, limit=50, keyword=""):
    """Fallback: scan rollout files (including .jsonl.zst) when no index exists."""
    sessions = []
    for rollout in _iter_codex_rollouts():
        sid = _codex_id_from_path(rollout)
        msg_count, first_prompt = _codex_quick_scan(rollout)
        try:
            mtime = _normalize_timestamp(rollout.stat().st_mtime)
        except OSError:
            mtime = ""
        if keyword and keyword.lower() not in first_prompt.lower():
            continue
        sessions.append(SessionMeta(
            session_id=sid, full_path=str(rollout),
            created=mtime, modified=mtime,
            message_count=msg_count, git_branch="", summary="",
            first_prompt=first_prompt[:200] if first_prompt else "",
            project_path="",
        ))
        if limit and len(sessions) >= limit:
            break
    return sessions[:limit]

def _find_codex_rollout(session_id):
    """Find a rollout file by session UUID across all Codex homes."""
    if not session_id:
        return None
    for home in _codex_homes():
        sessions_dir = home / "sessions"
        if not sessions_dir.is_dir():
            continue
        # Prefer exact uuid match; accept both plain and zst
        for p in sessions_dir.rglob(f"*{session_id}*.jsonl*"):
            name = p.name
            if name.endswith(".jsonl") or name.endswith(".jsonl.zst"):
                return p
    return None

def _codex_quick_scan(rollout_path):
    """Quick scan: count user messages and extract first prompt."""
    user_count = 0
    first_prompt = ""
    for rec in _iter_jsonl(rollout_path):
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if rtype == "event_msg" and payload.get("type") == "user_message":
            user_count += 1
            if not first_prompt:
                first_prompt = (payload.get("message") or "")[:200]
        # Older/alternate shape: response_item message role=user
        elif rtype == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            user_count += 1
            if not first_prompt:
                first_prompt = _extract_content_text(payload.get("content", ""), max_len=200)
    return user_count, first_prompt

def codex_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from a Codex rollout session.

    Covers function_call / custom_tool_call / local_shell_call (resume-session parity).
    """
    path = Path(session_dir)
    if not path.exists():
        return
    calls = {}
    outputs = {}
    _CALL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")
    _OUT_TYPES = ("function_call_output", "custom_tool_call_output")
    for rec in _iter_jsonl(path):
        if rec.get("type") != "response_item":
            continue
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")
        ts = rec.get("timestamp", "")
        if ptype in _CALL_TYPES:
            if ptype == "local_shell_call":
                name = "local_shell"
                args = payload.get("action", payload.get("arguments", ""))
            else:
                name = payload.get("name", "")
                args = payload.get("arguments", payload.get("input", ""))
            if tool_filter and name != tool_filter:
                continue
            call_id = payload.get("call_id") or payload.get("id") or ""
            calls[call_id] = {
                "name": name,
                "ts": ts,
                "input_preview": str(args)[:150] if args else "",
            }
        elif ptype in _OUT_TYPES:
            call_id = payload.get("call_id") or payload.get("id") or ""
            output = payload.get("output", "")
            if isinstance(output, dict):
                output = output.get("body") or output.get("text") or output
            is_error = False
            if isinstance(output, str):
                if "Exit Code:" in output and "Exit Code: 0" not in output:
                    is_error = True
                if "Failed" in output:
                    is_error = True
            elif payload.get("success") is False:
                is_error = True
            outputs[call_id] = {
                "preview": str(output)[:150].replace("\\n", " ") if output else "",
                "is_error": is_error,
            }
    yield from _match_call_results(calls, outputs, errors_only, limit)

def codex_session_path(cwd, session_id=None):
    """Find a Codex session file across all Codex homes."""
    if session_id:
        found = _find_codex_rollout(session_id)
        if found:
            return found
    # Newest rollout as default
    for p in _iter_codex_rollouts():
        return p
    return None

# ── WorkBuddy adapter (delegates to _adapters_workbuddy.py) ──────────
from echolib._adapters_workbuddy import (
    workbuddy_list_sessions,
    workbuddy_session_stats,
    workbuddy_extract_messages,
    workbuddy_extract_tools,
    workbuddy_session_path,
)

# ── Cursor adapter (delegates to _adapters_cursor.py) ────────────────
from echolib._adapters_cursor import (
    cursor_list_sessions,
    cursor_session_stats,
    cursor_extract_messages,
    cursor_extract_tools,
    cursor_session_path,
)


def _trae_decode_project_slug(slug: str) -> str:
    """Decode Trae project dir like ``-Users-name-Documents-GPT`` → readable path."""
    if not slug:
        return ""
    # Claude-style dash encoding: leading - becomes /, remaining - become /
    if slug.startswith("-"):
        return "/" + slug[1:].replace("-", "/")
    return slug.replace("-", "/")


def _trae_norm_ts(raw) -> str:
    """Normalize Trae ``message_summary_time`` (often ``YYYY-MM-DD HH:MM:SS``)."""
    if not raw:
        return ""
    s = str(raw).strip()
    # Promote space-separated local times to ISO-ish so cross-tool sort works
    if len(s) >= 19 and s[10] == " " and "T" not in s:
        s = s[:10] + "T" + s[11:]
    n = _normalize_timestamp(s)
    return n or s


def _trae_session_id_from_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("session_memory_"):
        return stem[len("session_memory_"):]
    return stem


def _trae_scan_memory_file(jsonl_path: Path) -> dict:
    """One-pass scan of a Trae session_memory file."""
    intents = []
    outcomes = []
    action_n = 0
    learned_n = 0
    started = ended = ""
    for rec in _iter_jsonl(jsonl_path):
        ts = _trae_norm_ts(rec.get("message_summary_time", ""))
        if ts:
            if not started or ts < started:
                started = ts
            if not ended or ts > ended:
                ended = ts
        intent = (rec.get("intent") or "").strip()
        if intent:
            intents.append(intent)
        outcome = (rec.get("outcome") or "").strip()
        if outcome:
            outcomes.append(outcome)
        actions = rec.get("actions") or []
        if isinstance(actions, list):
            action_n += len(actions)
        learned = rec.get("learned") or []
        if isinstance(learned, list):
            learned_n += len(learned)
    return {
        "intents": intents,
        "outcomes": outcomes,
        "action_n": action_n,
        "learned_n": learned_n,
        "started": started,
        "ended": ended or started,
        "turns": len(intents),
    }


def trae_list_sessions(cwd=None, limit=50, keyword=""):
    """List Trae CN sessions from ~/.trae-cn/memory/projects/.

    Trae only stores **summary cards** (intent/actions/outcome/learned), not full
    transcripts — adapter surfaces that honestly, with multi-day files merged
    per session_id and the latest shard as full_path.
    """
    memory_dir = TRAE_DIR / "memory" / "projects"
    if not memory_dir.exists():
        return []

    sessions = {}  # sid → merged meta
    keyword_l = keyword.lower() if keyword else ""
    cwd_n = os.path.normpath(cwd) if cwd else ""

    try:
        project_dirs = [d for d in memory_dir.iterdir() if d.is_dir()]
    except OSError:
        return []

    for project_dir in project_dirs:
        project_slug = project_dir.name
        decoded = _trae_decode_project_slug(project_slug)
        if cwd_n and decoded:
            # soft match: project path equals or is parent/child of cwd
            try:
                dn = os.path.normpath(decoded)
                if dn != cwd_n and not cwd_n.startswith(dn + os.sep) and not dn.startswith(cwd_n + os.sep):
                    # also allow slug containment of cwd basename
                    if Path(cwd_n).name not in project_slug:
                        continue
            except Exception:
                pass

        try:
            date_dirs = [d for d in project_dir.iterdir() if d.is_dir() and d.name.isdigit()]
        except OSError:
            continue

        for date_dir in date_dirs:
            try:
                files = list(date_dir.glob("session_memory_*.jsonl"))
            except OSError:
                continue
            for jsonl_file in files:
                sid = _trae_session_id_from_path(jsonl_file)
                try:
                    scan = _trae_scan_memory_file(jsonl_file)
                    mtime = jsonl_file.stat().st_mtime
                except OSError:
                    continue

                if sid not in sessions:
                    sessions[sid] = {
                        "path": str(jsonl_file),
                        "project": project_slug,
                        "project_decoded": decoded,
                        "intents": list(scan["intents"]),
                        "outcomes": list(scan["outcomes"]),
                        "action_n": scan["action_n"],
                        "learned_n": scan["learned_n"],
                        "started": scan["started"],
                        "ended": scan["ended"],
                        "mtime": mtime,
                    }
                else:
                    info = sessions[sid]
                    info["intents"].extend(scan["intents"])
                    info["outcomes"].extend(scan["outcomes"])
                    info["action_n"] += scan["action_n"]
                    info["learned_n"] += scan["learned_n"]
                    if scan["started"] and (not info["started"] or scan["started"] < info["started"]):
                        info["started"] = scan["started"]
                    if scan["ended"] and (not info["ended"] or scan["ended"] > info["ended"]):
                        info["ended"] = scan["ended"]
                    # Keep latest shard as canonical path
                    if mtime >= info["mtime"]:
                        info["mtime"] = mtime
                        info["path"] = str(jsonl_file)

    result = []
    for sid, info in sessions.items():
        first_intent = info["intents"][0] if info["intents"] else ""
        summary = first_intent or " | ".join(info["intents"][:3])
        if not summary and info["outcomes"]:
            summary = info["outcomes"][0]
        if keyword_l and keyword_l not in summary.lower() and keyword_l not in sid.lower():
            continue
        result.append(SessionMeta(
            session_id=sid,
            full_path=info["path"],
            created=info["started"] or _normalize_timestamp(info["mtime"]),
            modified=info["ended"] or _normalize_timestamp(info["mtime"]),
            message_count=len(info["intents"]),
            git_branch="",
            summary=summary[:100],
            first_prompt=first_intent[:200],
            project_path=info.get("project_decoded") or info["project"],
        ))

    result.sort(key=lambda s: str(s.modified or s.created or ""), reverse=True)
    return result[:limit]


def _trae_extract_intents(jsonl_path):
    """Extract intent strings from a Trae CN memory JSONL."""
    return [
        rec.get("intent", "")
        for rec in _iter_jsonl(jsonl_path)
        if (rec.get("intent") or "").strip()
    ]


def trae_session_stats(session_dir):
    """Stats for Trae CN session_memory (summary-level — no full transcript)."""
    path = Path(session_dir)
    if not path.exists():
        return _empty_stats("trae_cn")
    stats = _empty_stats("trae_cn")
    stats["model"] = "trae-cn"
    stats["slug"] = _trae_session_id_from_path(path)

    first_intent = ""
    for rec in _iter_jsonl(path):
        ts = _trae_norm_ts(rec.get("message_summary_time", ""))
        if ts:
            if not stats["started"] or ts < stats["started"]:
                stats["started"] = ts
            if not stats["ended"] or ts > stats["ended"]:
                stats["ended"] = ts
        intent = (rec.get("intent") or "").strip()
        if intent:
            stats["user_messages"] += 1
            if not first_intent:
                first_intent = intent
        outcome = (rec.get("outcome") or "").strip()
        if outcome:
            stats["assistant_messages"] += 1
            # soft error signal in Chinese/English outcome text
            if any(k in outcome for k in ("失败", "错误", "报错", "failed", "error", "exception")):
                stats["errors"] += 1
        actions = rec.get("actions") or []
        if isinstance(actions, list):
            stats["tool_calls"] += len(actions)
        learned = rec.get("learned") or []
        if isinstance(learned, list) and learned:
            # reuse files_edited as "learned items" is wrong; keep tool_calls only
            pass

    stats["summary"] = (first_intent or f"{stats['user_messages']} turns")[:100]
    stats["total_tokens"] = 0  # summary format has no token accounting
    attach_cache_hit_rates(stats)
    return stats


def trae_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """Extract summarized turns from Trae CN session_memory JSONL.

    Each record is already a structured summary card — surface intent as USER
    and actions/outcome/learned as ASSISTANT (not a full chat transcript).
    """
    path = Path(session_dir)
    if not path.exists():
        return
    count = 0
    for rec in _iter_jsonl(path):
        ts = _trae_norm_ts(rec.get("message_summary_time", ""))
        if role in ("user", "both"):
            intent = (rec.get("intent") or "").strip()
            if intent:
                yield {"role": "USER", "timestamp": ts, "text": intent[:500]}
                count += 1
                if limit and count >= limit:
                    return
        if role in ("assistant", "both"):
            parts = []
            actions = rec.get("actions") or []
            if isinstance(actions, list) and actions:
                parts.append("[动作] " + " | ".join(str(a) for a in actions if a))
            outcome = (rec.get("outcome") or "").strip()
            if outcome:
                parts.append(f"[结果] {outcome}")
            learned = rec.get("learned") or []
            if isinstance(learned, list) and learned:
                parts.append("[收获] " + " | ".join(str(x) for x in learned if x))
            if parts:
                # Real newlines (was previously literal \\n — display bug)
                yield {"role": "ASSISTANT", "timestamp": ts, "text": "\n".join(parts)[:500]}
                count += 1
                if limit and count >= limit:
                    return


def trae_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract action list items as synthetic tools (Trae has no real tool IDs)."""
    path = Path(session_dir)
    if not path.exists():
        return
    count = 0
    for rec in _iter_jsonl(path):
        ts = _trae_norm_ts(rec.get("message_summary_time", ""))
        actions = rec.get("actions") or []
        if not isinstance(actions, list):
            continue
        outcome = (rec.get("outcome") or "").strip()
        is_error = any(
            k in outcome for k in ("失败", "错误", "报错", "failed", "error", "exception")
        )
        if errors_only and not is_error:
            continue
        for action in actions:
            action_s = str(action).strip()
            if not action_s:
                continue
            if tool_filter and tool_filter.lower() not in action_s.lower():
                continue
            if limit and count >= limit:
                return
            yield {
                "timestamp": ts,
                "name": action_s[:60],
                "status": "error" if is_error else "ok",
                "key_input": action_s[:150],
                "result_preview": (outcome[:150] if outcome else ""),
            }
            count += 1


def trae_session_path(cwd, session_id=None):
    """Find a Trae CN session_memory file (prefer latest date shard)."""
    memory_dir = TRAE_DIR / "memory" / "projects"
    if not memory_dir.exists():
        return None
    if session_id:
        candidates = []
        sid = session_id.replace("session_memory_", "")
        for p in memory_dir.rglob(f"session_memory_{sid}.jsonl"):
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                continue
        if candidates:
            candidates.sort(reverse=True)
            return str(candidates[0][1])
        return None
    # newest overall
    newest = None
    newest_m = -1
    try:
        for p in memory_dir.rglob("session_memory_*.jsonl"):
            m = p.stat().st_mtime
            if m > newest_m:
                newest_m = m
                newest = p
    except OSError:
        pass
    return str(newest) if newest else None

def universal_session_path(cwd, session_id=None):
    """Universal path finder: search for any JSONL file matching session_id."""
    home = Path.home()
    if session_id:
        for p in home.rglob(f"*{session_id}*.jsonl"):
            return p
    return None

_SCHEMA_PROBE_CACHE = {}  # path → schema dict

# Role vocab distilled from specialized adapters (Claude/Codex/Cursor/ZCode/Kimi/WB/Trae)
_USER_VALS = frozenset({
    "user", "human", "turn.prompt", "user_message", "turnbegin",
    "user_msg", "prompt", "turn_started", "human_message",
})
_ASSISTANT_VALS = frozenset({
    "assistant", "ai", "bot", "agent", "text", "content.part",
    "agent_message", "contentpart", "assistant_msg", "reasoning",
    "context.append_loop_event", "model_complete", "model",
})
_TOOL_VALS = frozenset({
    "tool_call", "function_call", "tool.call", "toolcall", "functioncall",
    "tool_call_scheduled", "tool", "tool_use", "tool_result",
})
_SYSTEM_VALS = frozenset({"system", "developer", "instruction", "instructions", "preamble"})
_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>", flags=re.DOTALL | re.IGNORECASE
)


def _probe_schema(jsonl_path, force=False):
    """Dynamic schema probe — family-aware, format-agnostic.

    Distills specialized-adapter experience into soft families:
      nested_message (Claude-like), nested_payload (Codex-like),
      flat_role (role+content), history_display (CLI prompt logs),
      summary_card (Trae-like intent/outcome), flat (type+content)

    Unknown / weird environments fall into the closest family without
    requiring a dedicated adapter. Results are cached per path.
    """
    path_str = str(jsonl_path)
    if not force and path_str in _SCHEMA_PROBE_CACHE:
        return _SCHEMA_PROBE_CACHE[path_str]

    samples = []
    for rec in _iter_jsonl(jsonl_path):
        if isinstance(rec, dict):
            samples.append(rec)
        if len(samples) >= 40:
            break

    schema = {
        "style": "unknown",
        "type_path": ["type"],
        "content_path": ["content"],
        "timestamp_field": "timestamp",
        "model_field": None,
        "tool_style": None,
        "family": "unknown",  # soft family label for diagnostics
        "skip_system": True,
    }

    if not samples:
        _SCHEMA_PROBE_CACHE[path_str] = schema
        return schema

    # ---------- Family detection (ordered by specificity) ----------
    has_message_nest = any(isinstance(r.get("message"), dict) for r in samples)
    has_payload_nest = any(isinstance(r.get("payload"), dict) for r in samples)
    has_flat_type = any(
        r.get("type") in (
            "user", "assistant", "system", "tool_result", "reasoning",
            "tool_use", "tool_call", "function_call",
        )
        for r in samples
    )
    has_top_role = any(
        isinstance(r.get("role"), str) and r.get("role").lower() in (
            "user", "assistant", "system", "tool", "human", "model"
        )
        for r in samples
    )
    # CLI history logs (codebuddy / mimo / similar): display + timestamp, no role
    has_display_history = (
        sum(1 for r in samples if isinstance(r.get("display"), str) and r.get("display").strip()) >= 1
        and not has_top_role
        and not has_flat_type
        and not has_message_nest
    )
    # Trae-like summary cards
    has_summary_card = any(
        r.get("intent") and (r.get("outcome") is not None or r.get("actions") is not None)
        for r in samples
    )

    if has_summary_card and not has_message_nest and not has_flat_type:
        schema["style"] = "summary_card"
        schema["family"] = "summary"
        schema["type_path"] = ["intent"]  # presence of intent = user turn
        schema["content_path"] = ["intent"]
        schema["timestamp_field"] = "message_summary_time"
    elif has_display_history:
        schema["style"] = "history_display"
        schema["family"] = "history"
        schema["type_path"] = ["display"]  # every display line = user prompt
        schema["content_path"] = ["display"]
    elif has_message_nest:
        schema["style"] = "nested_message"
        schema["family"] = "claude_like"
        msg_content_found = False
        for r in samples:
            msg = r.get("message", {})
            if not isinstance(msg, dict):
                continue
            payload = msg.get("payload", {})
            if isinstance(payload, dict) and payload.get("user_input"):
                schema["content_path"] = ["message", "payload", "user_input"]
                msg_content_found = True
                break
            content = msg.get("content")
            if content is not None and isinstance(content, (str, list)):
                schema["content_path"] = ["message", "content"]
                msg_content_found = True
                break
        if not msg_content_found:
            schema["content_path"] = ["message", "content"]
        for r in samples:
            msg = r.get("message", {})
            if isinstance(msg, dict) and msg.get("model"):
                schema["model_field"] = ["message", "model"]
                break
    elif has_payload_nest:
        schema["style"] = "nested_payload"
        schema["family"] = "codex_like"
        schema["content_path"] = ["payload", "content"]
    elif has_top_role:
        schema["style"] = "flat_role"
        schema["family"] = "role_content"
        # content may be string, list, or nested
        if any(isinstance(r.get("content"), (str, list)) for r in samples):
            schema["content_path"] = ["content"]
        elif any(isinstance(r.get("displayContent"), str) for r in samples):
            schema["content_path"] = ["displayContent"]
        else:
            schema["content_path"] = ["content"]
        schema["type_path"] = ["role"]
    elif has_flat_type:
        schema["style"] = "flat"
        schema["family"] = "type_content"
        schema["content_path"] = ["content"]

    # ---------- Role path scoring ----------
    candidate_paths = []
    if schema["style"] == "history_display":
        candidate_paths = [["display"]]
    elif schema["style"] == "summary_card":
        candidate_paths = [["intent"]]
    else:
        for r in samples:
            for key in ("role", "type"):
                if r.get(key):
                    candidate_paths.append([key])
            break
        if has_message_nest:
            candidate_paths.extend([["message", "type"], ["message", "role"]])
        if has_payload_nest:
            candidate_paths.extend([["payload", "type"], ["payload", "role"]])
        # Prefer role over type when both present at top level (deepcode/newmax)
        if has_top_role:
            candidate_paths.insert(0, ["role"])

    best_path = schema.get("type_path") or ["type"]
    best_score = -1
    for path in candidate_paths:
        n_user = n_ass = 0
        for r in samples:
            cur = r
            ok = True
            for key in path:
                if isinstance(cur, dict):
                    cur = cur.get(key, {})
                else:
                    ok = False
                    break
            if not ok:
                continue
            # history_display: any non-empty display is a user turn
            if path == ["display"] and isinstance(cur, str) and cur.strip():
                n_user += 1
                continue
            if path == ["intent"] and isinstance(cur, str) and cur.strip():
                n_user += 1
                continue
            val = str(cur).lower() if not isinstance(cur, dict) else ""
            if val in _USER_VALS:
                n_user += 1
            elif val in _ASSISTANT_VALS:
                n_ass += 1
        if n_user > 0 and n_ass > 0:
            score = 100 + n_user + n_ass
        elif n_user > 0 or n_ass > 0:
            score = n_user + n_ass
        else:
            score = 0
        if score > best_score or (score == best_score and len(path) < len(best_path)):
            best_score = score
            best_path = path
    schema["type_path"] = best_path

    # ---------- Tool style ----------
    for r in samples:
        t = str(r.get("type", "")).lower()
        role = str(r.get("role", "")).lower()
        if t in _TOOL_VALS or role == "tool":
            schema["tool_style"] = "type_based"
            break
        for nest_key in ("message", "payload"):
            nest = r.get(nest_key, {})
            if isinstance(nest, dict):
                nt = str(nest.get("type", "")).lower()
                if nt in _TOOL_VALS or "function_call" in nt:
                    schema["tool_style"] = "nested"
                    break
        if r.get("type") == "context.append_loop_event":
            event = r.get("event", {})
            if isinstance(event, dict):
                et = str(event.get("type", "")).lower()
                if et in _TOOL_VALS or "tool.call" in et:
                    schema["tool_style"] = "nested_event"
                    break
        if r.get("type") == "assistant" and isinstance(r.get("tool_calls"), list):
            schema["tool_style"] = "embedded_array"
            break
        if isinstance(r.get("content"), list):
            for b in r["content"]:
                if isinstance(b, dict) and b.get("type") in ("tool_use", "tool_call"):
                    schema["tool_style"] = "content_blocks"
                    break
        if schema["tool_style"]:
            break

    # ---------- Timestamp / model ----------
    for key in (
        "timestamp", "time", "ts", "createTime", "created_at", "createdAt",
        "date", "updated_at", "updateTime", "update_time", "_createdAt",
        "message_summary_time",
    ):
        if any(r.get(key) not in (None, "") for r in samples):
            schema["timestamp_field"] = key
            break

    if not schema["model_field"]:
        for key in ("model", "model_name", "model_id", "engine", "requestModelName"):
            if any(r.get(key) for r in samples):
                schema["model_field"] = key
                break
        if not schema["model_field"]:
            for r in samples:
                msg = r.get("message")
                if isinstance(msg, dict) and msg.get("model"):
                    schema["model_field"] = ["message", "model"]
                    break
                pd = r.get("providerData") or r.get("providerInfo")
                if isinstance(pd, dict):
                    for mk in ("requestModelName", "model", "modelId", "model_id"):
                        if pd.get(mk):
                            schema["model_field"] = (
                                ["providerData", mk] if "providerData" in r
                                else ["providerInfo", mk]
                            )
                            break
                if schema["model_field"]:
                    break

    _SCHEMA_PROBE_CACHE[path_str] = schema
    return schema

def _schema_clean_user_text(text: str) -> str:
    """Strip wrappers learned from Cursor/WorkBuddy/Qoder transcripts."""
    if not text:
        return ""
    stripped = text.strip()
    # Prefer <user_query> body
    matches = _USER_QUERY_RE.findall(stripped)
    if matches:
        joined = "\n".join(m.strip() for m in matches if m.strip())
        if joined:
            return joined
    cleaned = _strip_system_reminder(stripped)
    if cleaned is None:
        return ""
    cleaned = cleaned.strip()
    if cleaned.startswith(("<runtime_context", "<environment_context", "<user_info")):
        return ""
    # Skip pure system/env JSON dumps
    if cleaned.startswith("{") and '"env"' in cleaned[:80] and '"MODEL"' in cleaned[:200]:
        return ""
    return cleaned


def _schema_get_text(schema, rec):
    """Extract text via schema; multi-family fallbacks from specialized adapters."""
    path = schema.get("content_path") or ["content"]
    style = schema.get("style", "")

    # Summary card: intent for user, outcome/actions for assistant handled by caller
    if style == "summary_card":
        intent = rec.get("intent") or ""
        return str(intent)[:500] if intent else ""

    # History display: prompt-only logs
    if style == "history_display":
        disp = rec.get("display") or ""
        return str(disp)[:500] if isinstance(disp, str) else ""

    current = rec
    for key in path:
        if isinstance(current, dict):
            current = current.get(key, {})
        else:
            current = {}
            break

    if isinstance(current, str) and current.strip():
        return current[:500]
    if isinstance(current, list):
        texts = []
        for block in current:
            if isinstance(block, dict):
                # Prefer human text blocks; skip tool_use shells
                if block.get("type") in ("tool_use", "tool_call", "tool_result"):
                    continue
                bt = block.get("text") or block.get("content") or ""
                if bt:
                    texts.append(str(bt))
            elif isinstance(block, str):
                texts.append(block)
        if texts:
            return "\n".join(texts)[:500]

    # Kimi nested fallback
    if len(path) >= 2 and path[:2] == ["message", "payload"]:
        payload = rec.get("message", {}).get("payload", {}) if isinstance(rec.get("message"), dict) else {}
        if isinstance(payload, dict):
            text = payload.get("text", "")
            if isinstance(text, str) and text.strip():
                return text[:500]
            user_input = payload.get("user_input", [])
            if isinstance(user_input, list):
                texts = [u.get("text", "") for u in user_input if isinstance(u, dict) and u.get("text")]
                if texts:
                    return "\n".join(texts)[:500]

    # Codex nested_payload
    if style == "nested_payload":
        payload = rec.get("payload", {})
        if isinstance(payload, dict):
            # role-message with content blocks
            content = payload.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        for k in ("text", "message"):
                            v = block.get(k, "")
                            if isinstance(v, str) and v.strip():
                                return v[:500]
                    elif isinstance(block, str) and block.strip():
                        return block[:500]
            elif isinstance(content, str) and content.strip():
                return content[:500]
            # event_msg style
            for k in ("message", "text"):
                v = payload.get(k)
                if isinstance(v, str) and v.strip():
                    return v[:500]

    # Broad fallback field hunt (weird envs)
    for key in (
        "text", "input", "prompt", "query", "message_text", "display",
        "displayContent", "body", "output",
    ):
        val = rec.get(key, "")
        if isinstance(val, str) and val.strip():
            return val[:500]
    return ""

def _schema_get_timestamp(schema, rec):
    """Extract timestamp via schema + common variants."""
    ts = rec.get(schema.get("timestamp_field") or "timestamp", "")
    if not ts:
        for key in (
            "timestamp", "time", "ts", "createTime", "created_at", "createdAt",
            "updateTime", "_createdAt", "message_summary_time",
        ):
            if key != schema.get("timestamp_field"):
                val = rec.get(key, "")
                if val not in (None, ""):
                    return val
    return ts

def _schema_get_model(schema, rec):
    """Extract model name via schema path."""
    path = schema.get("model_field")
    if not path:
        return ""
    if isinstance(path, list):
        current = rec
        for key in path:
            if isinstance(current, dict):
                current = current.get(key, "")
            else:
                return ""
        return str(current) if current else ""
    return str(rec.get(path, "") or "")

def _schema_is_role(schema, rec, target):
    """Role match supporting nested paths + family-specific rules."""
    style = schema.get("style", "")
    type_path = schema.get("type_path", ["type"])

    # history_display: every non-empty display is a user prompt log
    if style == "history_display":
        if target == "user":
            d = rec.get("display")
            return isinstance(d, str) and bool(d.strip())
        return False

    # summary_card: intent → user; outcome/actions → assistant
    if style == "summary_card":
        if target == "user":
            return bool((rec.get("intent") or "").strip())
        if target == "assistant":
            return bool((rec.get("outcome") or "").strip() or rec.get("actions"))
        if target == "tool_call":
            return bool(rec.get("actions"))
        return False

    cur = rec
    for key in type_path:
        if isinstance(cur, dict):
            cur = cur.get(key, "")
        else:
            cur = ""
            break
    val = str(cur).lower() if not isinstance(cur, dict) else ""

    # Skip pure system noise when asking user/assistant
    if val in _SYSTEM_VALS and target in ("user", "assistant"):
        return False

    if target == "user":
        return val in _USER_VALS
    if target == "assistant":
        # Don't treat bare "text" as assistant when style is flat and type means block type
        if val == "text" and style not in ("flat", "nested_payload"):
            return False
        return val in _ASSISTANT_VALS
    if target == "tool_call":
        if val in _TOOL_VALS or "function_call" in val:
            return True
        if str(rec.get("role", "")).lower() == "tool":
            return True
        # content-block tool_use
        content = rec.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") in ("tool_use", "tool_call"):
                    return True
        return False
    return False

def _schema_is_user(schema, rec):
    return _schema_is_role(schema, rec, "user")

def _schema_is_assistant(schema, rec):
    return _schema_is_role(schema, rec, "assistant")

def _schema_is_tool_call(schema, rec):
    return _schema_is_role(schema, rec, "tool_call")

def universal_list_sessions(home_dir=None, env_name="unknown", limit=50, keyword=""):
    """Universal session discovery under sessions/projects/memory/conversations/…"""
    if home_dir is None:
        home_dir = Path.home()
    home_dir = Path(home_dir)
    search_dirs = []
    for pattern in (
        "sessions", "projects", "memory", "data", "conversations",
        "chats", "agent-sessions", "history",
    ):
        candidate = home_dir / pattern
        if candidate.exists():
            search_dirs.append(candidate)
    # Also accept a direct history.jsonl at root
    jsonl_files = []
    for search_dir in search_dirs:
        try:
            jsonl_files.extend(search_dir.rglob("*.jsonl"))
        except OSError:
            continue
    if not jsonl_files:
        # Single-file history logs at env root
        for name in ("history.jsonl", "sessions.jsonl", "chat.jsonl"):
            candidate = home_dir / name
            if candidate.is_file():
                jsonl_files.append(candidate)
    if not jsonl_files and home_dir.is_dir():
        try:
            jsonl_files = list(home_dir.rglob("*.jsonl"))[:500]
        except OSError:
            jsonl_files = []

    sessions = []
    keyword_l = keyword.lower() if keyword else ""
    for jf in sorted(jsonl_files, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        try:
            if jf.stat().st_size < 50:
                continue
        except OSError:
            continue
        sid = jf.stem
        mtime = _normalize_timestamp(jf.stat().st_mtime)
        first_prompt = _universal_quick_scan(jf)
        if keyword_l and keyword_l not in first_prompt.lower() and keyword_l not in sid.lower():
            continue
        sessions.append(SessionMeta(
            session_id=sid,
            full_path=str(jf),
            created=mtime,
            modified=mtime,
            message_count=0,
            git_branch="",
            summary=(first_prompt or f"[{env_name}] {jf.parent.name}")[:100],
            first_prompt=first_prompt[:200],
            project_path=str(jf.parent),
        ))
        if limit and len(sessions) >= limit:
            break
    return sessions[:limit]

def _universal_quick_scan(jsonl_path):
    """SchemaProbe: first user-ish text for list cards."""
    schema = _probe_schema(jsonl_path)
    for rec in _iter_jsonl(jsonl_path):
        if not isinstance(rec, dict):
            continue
        if _schema_is_user(schema, rec):
            text = _schema_clean_user_text(_schema_get_text(schema, rec))
            if text:
                return text[:200]
        # history / summary already user-shaped via is_user
        text = _schema_clean_user_text(_schema_get_text(schema, rec))
        if text and schema.get("style") in ("history_display", "summary_card", "flat_role"):
            return text[:200]
    return ""

def universal_session_stats(session_path):
    """Universal stats via SchemaProbe (works for unknown / weird JSONL).

    Token extraction: scans for common token fields across env families.
    Not every unknown env has tokens — fields stay 0 when absent.
    """
    path = Path(session_path)
    stats = _empty_stats("unknown")
    stats["slug"] = path.stem
    if not path.exists():
        return stats
    schema = _probe_schema(session_path)
    stats["model"] = schema.get("family") or "unknown"

    first_summary = ""
    for rec in _iter_jsonl(path):
        if not isinstance(rec, dict):
            continue
        ts = _schema_get_timestamp(schema, rec)
        if ts:
            nt = _normalize_timestamp(ts)
            if nt:
                if not stats["started"] or nt < stats["started"]:
                    stats["started"] = nt
                if nt > stats["ended"]:
                    stats["ended"] = nt
        model = _schema_get_model(schema, rec)
        if model and stats["model"] in ("unknown", "", schema.get("family")):
            stats["model"] = str(model).split("/")[-1]

        # ── Token extraction (universal — try common field names) ──
        usage = rec.get("usage")
        if not isinstance(usage, dict):
            usage = rec.get("tokenUsage") or rec.get("token_usage") or {}
        if isinstance(usage, dict):
            stats["input_tokens"] += int(
                usage.get("inputTokens") or usage.get("input_tokens")
                or usage.get("prompt_tokens") or usage.get("total_input_tokens") or 0
            )
            stats["output_tokens"] += int(
                usage.get("outputTokens") or usage.get("output_tokens")
                or usage.get("completion_tokens") or usage.get("total_output_tokens") or 0
            )
            stats["cache_read_tokens"] += int(
                usage.get("cacheReadTokens") or usage.get("cache_read_tokens")
                or usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens") or 0
            )
            stats["cache_create_tokens"] += int(
                usage.get("cacheCreateTokens") or usage.get("cache_creation_tokens")
                or usage.get("cache_write_tokens") or 0
            )

        if schema.get("style") == "summary_card":
            intent = (rec.get("intent") or "").strip()
            if intent:
                stats["user_messages"] += 1
                if not first_summary:
                    first_summary = intent[:100]
            if (
                (rec.get("outcome") or "").strip()
                or (isinstance(rec.get("actions"), list) and rec.get("actions"))
                or (isinstance(rec.get("learned"), list) and rec.get("learned"))
            ):
                stats["assistant_messages"] += 1
            actions = rec.get("actions") or []
            if isinstance(actions, list):
                stats["tool_calls"] += len(actions)
        else:
            if _schema_is_user(schema, rec):
                cleaned = _schema_clean_user_text(_schema_get_text(schema, rec))
                if cleaned:
                    stats["user_messages"] += 1
                    if not first_summary:
                        first_summary = cleaned[:100]
            elif _schema_is_assistant(schema, rec):
                text = _schema_get_text(schema, rec)
                if text:
                    stats["assistant_messages"] += 1
            elif _schema_is_tool_call(schema, rec):
                stats["tool_calls"] += 1
                if rec.get("is_error") or rec.get("isError"):
                    stats["errors"] += 1

    if first_summary:
        stats["summary"] = first_summary
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    attach_cache_hit_rates(stats)
    return stats

def universal_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Universal message extraction via SchemaProbe + wrapper cleanup."""
    path = Path(session_path)
    if not path.exists():
        return
    schema = _probe_schema(session_path)
    count = 0
    style = schema.get("style", "")

    for rec in _iter_jsonl(path):
        if not isinstance(rec, dict):
            continue
        ts = _schema_get_timestamp(schema, rec)
        nt = _normalize_timestamp(ts) if ts else ""

        if role in ("user", "both") and _schema_is_user(schema, rec):
            if style == "summary_card":
                text = (rec.get("intent") or "")[:500]
            else:
                text = _schema_clean_user_text(_schema_get_text(schema, rec))
            if text:
                yield {"role": "USER", "timestamp": nt, "text": text[:500]}
                count += 1
                if limit and count >= limit:
                    return

        if role in ("assistant", "both") and _schema_is_assistant(schema, rec):
            if style == "summary_card":
                parts = []
                actions = rec.get("actions") or []
                if isinstance(actions, list) and actions:
                    parts.append("[动作] " + " | ".join(str(a) for a in actions if a))
                if rec.get("outcome"):
                    parts.append(f"[结果] {rec['outcome']}")
                learned = rec.get("learned") or []
                if isinstance(learned, list) and learned:
                    parts.append("[收获] " + " | ".join(str(x) for x in learned if x))
                text = "\n".join(parts)
            else:
                text = _schema_get_text(schema, rec)
            if text:
                yield {"role": "ASSISTANT", "timestamp": nt, "text": str(text)[:500]}
                count += 1
                if limit and count >= limit:
                    return

def universal_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Universal tool extraction via SchemaProbe multi-family tool styles."""
    path = Path(session_path)
    if not path.exists():
        return
    schema = _probe_schema(session_path)
    count = 0
    style = schema.get("style", "")

    for rec in _iter_jsonl(path):
        if not isinstance(rec, dict):
            continue
        ts = _schema_get_timestamp(schema, rec)
        nt = _normalize_timestamp(ts) if ts else ""

        # summary_card: actions list
        if style == "summary_card":
            actions = rec.get("actions") or []
            if not isinstance(actions, list):
                continue
            outcome = str(rec.get("outcome") or "")
            is_error = any(k in outcome for k in ("失败", "错误", "failed", "error"))
            if errors_only and not is_error:
                continue
            for action in actions:
                action_s = str(action).strip()
                if not action_s:
                    continue
                if tool_filter and tool_filter.lower() not in action_s.lower():
                    continue
                yield {
                    "timestamp": nt,
                    "name": action_s[:60],
                    "status": "error" if is_error else "ok",
                    "key_input": action_s[:150],
                    "result_preview": outcome[:150],
                }
                count += 1
                if limit and count >= limit:
                    return
            continue

        # content-block tool_use (Cursor / Claude-like nested content)
        if isinstance(rec.get("content"), list):
            emitted = False
            for block in rec["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") not in ("tool_use", "tool_call"):
                    continue
                name = block.get("name") or "tool"
                if tool_filter and name != tool_filter:
                    continue
                if errors_only:
                    continue
                args = block.get("input") or block.get("arguments") or ""
                yield {
                    "timestamp": nt,
                    "name": name,
                    "status": "ok",
                    "key_input": str(args)[:150],
                    "result_preview": "",
                }
                count += 1
                emitted = True
                if limit and count >= limit:
                    return
            if emitted:
                continue

        if not _schema_is_tool_call(schema, rec):
            continue
        name = (
            rec.get("name")
            or rec.get("tool_name")
            or rec.get("toolName")
            or ""
        )
        # nested payload
        if not name and isinstance(rec.get("payload"), dict):
            name = rec["payload"].get("name") or rec["payload"].get("toolName") or ""
        if not name and isinstance(rec.get("event"), dict):
            name = rec["event"].get("name") or ""
        name = name or "tool"
        if tool_filter and name != tool_filter:
            continue
        is_error = bool(
            rec.get("is_error")
            or rec.get("isError")
            or rec.get("status") in ("error", "failed")
            or rec.get("error")
        )
        if errors_only and not is_error:
            continue
        args = rec.get("arguments", rec.get("input", rec.get("args", "")))
        if not args and isinstance(rec.get("payload"), dict):
            args = rec["payload"].get("arguments") or rec["payload"].get("input") or ""
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        yield {
            "timestamp": nt,
            "name": name,
            "status": "error" if is_error else "ok",
            "key_input": str(args)[:150] if args else "",
            "result_preview": "",
        }
        count += 1
        if limit and count >= limit:
            return

ENV_REGISTRY = {
    "claude": {"name": "Claude Code", "root": "~/.claude/projects/", "format": "jsonl", "adapter": "claude"},
    "grok": {"name": "Grok Build", "root": "~/.grok/sessions/", "format": "jsonl", "adapter": "grok"},
    "kimi": {"name": "Kimi (standalone)", "root": "~/.kimi/sessions/", "format": "jsonl", "adapter": "kimi"},
    "kimi_code": {"name": "Kimi Code", "root": "~/.kimi-code/sessions/", "format": "jsonl", "adapter": "kimi_code"},
    "codex": {"name": "Codex (OpenAI)", "root": "~/.codex/sessions/", "format": "jsonl", "adapter": "codex"},
    "cursor": {"name": "Cursor", "root": "~/.cursor/projects/", "format": "jsonl+sqlite", "adapter": "cursor"},
    "workbuddy": {"name": "WorkBuddy", "root": "~/.workbuddy/projects/", "format": "jsonl", "adapter": "workbuddy"},
    "trae_cn": {"name": "Trae CN (ByteDance)", "root": "~/.trae-cn/memory/projects/", "format": "jsonl-summary", "adapter": "trae_cn"},
    "zcode": {"name": "ZCode (Z-AI)", "root": "~/.zcode/cli/agents/", "format": "jsonl-trace", "adapter": "zcode"},
    "dim": {"name": "DIM (Memory)", "root": "~/.dim/memory/", "format": "jsonl-summary", "adapter": "dim"},
    "dimcode": {"name": "DimCode (SQLite)", "root": "~/.dimcode/v2/dimcode.sqlite", "format": "sqlite", "adapter": "dimcode"},
    "reasonix": {"name": "Reasonix", "root": "~/.reasonix/sessions/", "format": "jsonl", "adapter": "reasonix"},
}

KNOWN_UNADAPTED = {
    # Light discovery only — universal SchemaProbe handles parse when opened
    "mimo": {"name": "MiMo", "root": "~/.mimo/"},
    "qwen": {"name": "Qwen Code", "root": "~/.qwen/projects/"},
    "qoder": {"name": "Qoder", "root": "~/.qoder/cache/projects/"},
    "qoder-cn": {"name": "Qoder CN", "root": "~/.qoder-cn/"},
    "openclaw-autoclaw": {"name": "OpenClaw AutoClaw", "root": "~/.openclaw-autoclaw/agents/"},
    "gstack": {"name": "GStack", "root": "~/.gstack/"},
    "codebuddy": {"name": "CodeBuddy", "root": "~/.codebuddy/"},
    "commandcode": {"name": "CommandCode", "root": "~/.commandcode/"},
    "cc-switch": {"name": "CC-Switch", "root": "~/.cc-switch/"},
    "newmax": {"name": "NewMax", "root": "~/.newmax/conversations/"},
    "proma": {"name": "Proma", "root": "~/.proma/agent-sessions/"},
    "iflow": {"name": "iFlow", "root": "~/.iflow/projects/"},
    "deepcode": {"name": "DeepCode", "root": "~/.deepcode/projects/"},
    "gemini": {"name": "Gemini", "root": "~/.gemini/"},
}

def scan_all_environments_parallel():
    """Parallel scan of all known and unknown environments."""
    results = []

    def _scan_env(env_id, env_info, is_registered=True):
        root = Path(os.path.expanduser(env_info["root"]))
        exists = root.exists()
        session_count = 0
        if exists:
            if root.is_file():
                # SQLite / single-file roots (e.g. dimcode)
                session_count = 1
            else:
                jsonl_files = _fast_find_jsonl(root)
                session_count = len(jsonl_files)
        return {
            "name": env_info["name"], "env_id": env_id,
            "path": str(root), "exists": exists,
            "session_count": session_count,
            "format": env_info.get("format", "unknown"),
            "adapter": env_info.get("adapter", "universal"),
            "status": "adapted" if is_registered else ("unadapted" if exists else "missing"),
        }

    env_tasks = []
    for env_id, env_info in ENV_REGISTRY.items():
        env_tasks.append((env_id, env_info, True))
    for env_id, env_info in KNOWN_UNADAPTED.items():
        env_tasks.append((env_id, env_info, False))

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_scan_env, eid, info, reg): eid for eid, info, reg in env_tasks}
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # 单环境扫描线程失败 → 留痕 + 继续
                _log.warning("adapter thread failed: %s", exc, exc_info=True)

    # Scan home directory for unknown environments
    home = Path.home()
    known_dirs = set()
    for e in list(ENV_REGISTRY.values()) + list(KNOWN_UNADAPTED.values()):
        known_dirs.add(os.path.expanduser(e["root"]).split("/")[0])
    known_dirs.update(str(home / d) for d in (
        ".claude", ".zcode", ".agents", ".config", ".cache", ".npm", ".cargo",
        ".ssh", ".local", ".cursor", ".codex", ".grok",
    ))

    dotdirs = []
    try:
        for dotdir in home.iterdir():
            if not dotdir.is_dir() or not dotdir.name.startswith("."):
                continue
            if str(dotdir) in known_dirs:
                continue
            dotdirs.append(dotdir)
    except OSError:
        pass

    def _scan_unknown_dir(dotdir):
        try:
            jsonl_files = _fast_find_jsonl(dotdir)
            if jsonl_files:
                return {"name": dotdir.name, "env_id": dotdir.name.lstrip("."), "path": str(dotdir), "exists": True, "session_count": len(jsonl_files), "format": "unknown", "adapter": "universal", "status": "discovered"}
        except OSError:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_scan_unknown_dir, d) for d in dotdirs]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as exc:  # 单环境扫描线程失败 → 留痕 + 继续
                _log.warning("adapter thread failed: %s", exc, exc_info=True)

    return results

def _empty_stats(agent_name) -> SessionStats:
    """Return the standard stats dict with empty values.

    Returns:
        SessionStats — a TypedDict with all expected keys for the index/trend pipeline.
    """
    return {
        "slug": "", "model": agent_name, "branch": "",
        "started": "", "ended": "",
        "user_messages": 0, "assistant_messages": 0,
        "tool_calls": 0, "files_edited": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "compactions": 0, "summary": "",
        "total_tokens": 0,
        "cache_hit_rate": None,
    }

# ── Grok family / usage / dedicated stats (delegates to _adapters_grok.py)
from echolib._adapters_grok import (
    _grok_resolve_path,
    _grok_extract_messages,
    _grok_as_int,
    _grok_apply_signals,
    _grok_parse_model_usage_map,
    _grok_parse_usage_object,
    _grok_iter_usage_snapshots,
    _grok_flush_run_last,
    _grok_aggregate_billable_usage,
    _grok_read_billable_usage,
    _grok_read_billable_usage_cached,
    _grok_apply_usage_agg,
    _grok_apply_usage_from_updates,
    _grok_session_token_profile,
    _grok_token_bucket,
    _grok_add_buckets,
    _grok_sub_buckets,
    _grok_find_session_dir,
    grok_list_subagents,
    _grok_sum_model_maps,
    grok_family_usage_report,
    grok_aggregate_model_usage,
    _grok_session_stats,
)


def _kimi_code_resolve_path(path):
    """Resolve Kimi Code session dir to agents/main/wire.jsonl file path."""
    p = Path(path)
    if p.is_dir():
        wire = p / "agents" / "main" / "wire.jsonl"
        if wire.exists():
            return str(wire)
        wire = p / "wire.jsonl"  # old kimi format
        if wire.exists():
            return str(wire)
    return str(p)


def _kimi_code_session_dir(session_path) -> Path | None:
    """Resolve session directory from dir path or wire.jsonl path."""
    p = Path(session_path)
    if p.is_dir() and (p / "state.json").exists():
        return p
    if p.is_file() and p.name == "wire.jsonl":
        # .../session_xxx/agents/main/wire.jsonl → session_xxx
        try:
            return p.parent.parent.parent
        except Exception:
            return None
    return p if p.is_dir() else None


def kimi_code_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """
    Extract messages from Kimi Code wire.jsonl.

    Kimi Code format:
      - turn.prompt: user input (input[].text)
      - context.append_loop_event content.part: assistant text|think (often multi-part)
    Coalesce text parts by turnId so one user turn → one assistant reply (Codex-level UX).
    """
    p = Path(session_path)
    if p.is_dir():
        wire = p / "agents" / "main" / "wire.jsonl"
        if wire.exists():
            p = wire
        else:
            return

    if not p.exists() or not p.is_file():
        return

    count = 0
    # turnId → {"text": [], "think": [], "ts": str}
    asst_buf: dict = {}
    asst_order: list = []

    def _flush_turn(turn_id):
        nonlocal count
        buf = asst_buf.pop(turn_id, None)
        if not buf:
            return False
        if turn_id in asst_order:
            asst_order.remove(turn_id)
        text = "\n".join(buf["text"]).strip()
        think = "\n".join(buf["think"]).strip()
        out = text
        if thinking_limit != -1 and think:
            if thinking_limit > 0:
                think = think[:thinking_limit]
            out = f"[THINKING] {think}\n{text}".strip() if text else f"[THINKING] {think}"
        if not out:
            return False
        yield_item = {
            "role": "ASSISTANT",
            "timestamp": buf.get("ts") or "",
            "text": out[:500],
        }
        return yield_item

    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        ts = rec.get("time", rec.get("timestamp", ""))
        nts = _normalize_timestamp(ts) if ts else ""

        if rtype == "turn.prompt" and role in ("user", "both"):
            # Flush any open assistant buffers before next user turn
            if role in ("assistant", "both"):
                for tid in list(asst_order):
                    item = _flush_turn(tid)
                    if item:
                        yield item
                        count += 1
                        if limit and count >= limit:
                            return
            inputs = rec.get("input", [])
            parts = []
            for inp in (inputs if isinstance(inputs, list) else []):
                if isinstance(inp, dict) and inp.get("type") == "text":
                    t = (inp.get("text") or "").strip()
                    if t:
                        parts.append(t)
            if parts:
                text = "\n".join(parts)
                cleaned = _strip_system_reminder(text) or text
                yield {"role": "USER", "timestamp": nts, "text": cleaned[:500]}
                count += 1
                if limit and count >= limit:
                    return

        elif rtype == "context.append_loop_event" and role in ("assistant", "both"):
            event = rec.get("event", {})
            if not isinstance(event, dict):
                continue
            etype = event.get("type", "")
            turn_id = str(event.get("turnId") or event.get("stepUuid") or nts or "default")

            if etype == "content.part":
                part = event.get("part", {})
                if not isinstance(part, dict):
                    continue
                pt = part.get("type", "")
                text = (part.get("text") or "").strip()
                if not text:
                    continue
                if turn_id not in asst_buf:
                    asst_buf[turn_id] = {"text": [], "think": [], "ts": nts}
                    asst_order.append(turn_id)
                if pt == "text":
                    asst_buf[turn_id]["text"].append(text)
                    asst_buf[turn_id]["ts"] = nts or asst_buf[turn_id]["ts"]
                elif pt == "think" and thinking_limit != -1:
                    asst_buf[turn_id]["think"].append(text)
            elif etype in ("step.end", "tool.call") and turn_id in asst_buf:
                # Natural boundary: flush completed assistant text for this turn
                if asst_buf[turn_id]["text"] or (thinking_limit != -1 and asst_buf[turn_id]["think"]):
                    # only flush on step.end to avoid splitting mid-reply before tools
                    if etype == "step.end" and asst_buf[turn_id]["text"]:
                        item = _flush_turn(turn_id)
                        if item:
                            yield item
                            count += 1
                            if limit and count >= limit:
                                return

    # Flush remaining
    if role in ("assistant", "both"):
        for tid in list(asst_order):
            item = _flush_turn(tid)
            if item:
                yield item
                count += 1
                if limit and count >= limit:
                    return


def kimi_code_session_stats(session_path):
    """Stats for Kimi Code — count user turns & text assistant replies (not think parts)."""
    resolved = _kimi_code_resolve_path(session_path)
    stats = _empty_stats("kimi_code")
    session_dir = _kimi_code_session_dir(session_path)
    if session_dir is not None:
        stats["slug"] = _kimi_code_session_id(session_dir)
    else:
        stats["slug"] = Path(resolved).stem

    # Title / workDir from state.json
    if session_dir is not None:
        state_file = session_dir / "state.json"
        if state_file.exists():
            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)
                stats["summary"] = (state.get("title") or state.get("lastPrompt") or "")[:100]
                if state.get("model"):
                    stats["model"] = state["model"]
            except (json.JSONDecodeError, OSError):
                pass

    text_turns = set()  # turnIds with assistant text (not think-only)
    for rec in _iter_jsonl(resolved):
        rtype = rec.get("type", "")
        ts = rec.get("time", rec.get("timestamp", ""))
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not stats["started"] or nts < stats["started"]:
                    stats["started"] = nts
                if nts > stats["ended"]:
                    stats["ended"] = nts

        if rtype == "turn.prompt":
            stats["user_messages"] += 1
        elif rtype == "llm.request":
            model = rec.get("model") or rec.get("modelAlias")
            if model and (not stats["model"] or stats["model"] in ("kimi", "kimi_code")):
                stats["model"] = str(model)
        elif rtype == "context.append_loop_event":
            event = rec.get("event", {})
            if not isinstance(event, dict):
                continue
            etype = event.get("type", "")
            if etype == "content.part":
                part = event.get("part", {})
                if isinstance(part, dict) and part.get("type") == "text" and (part.get("text") or "").strip():
                    tid = str(event.get("turnId") or event.get("stepUuid") or "")
                    text_turns.add(tid or f"anon-{len(text_turns)}")
            elif etype == "tool.call":
                stats["tool_calls"] += 1
            elif etype == "tool.result":
                result = event.get("result", {})
                if isinstance(result, dict):
                    if result.get("isError"):
                        stats["errors"] += 1
                    else:
                        output = result.get("output", "")
                        if isinstance(output, str) and "Exit Code:" in output and "Exit Code: 0" not in output:
                            stats["errors"] += 1
        elif rtype == "usage.record":
            usage = rec.get("usage", {})
            if isinstance(usage, dict):
                stats["input_tokens"] += int(
                    usage.get("inputOther") or usage.get("inputTokens") or usage.get("input_tokens") or 0
                )
                stats["output_tokens"] += int(
                    usage.get("output") or usage.get("outputTokens") or usage.get("output_tokens") or 0
                )
                stats["cache_read_tokens"] += int(
                    usage.get("inputCacheRead") or usage.get("cacheReadTokens") or 0
                )
                stats["cache_create_tokens"] += int(
                    usage.get("inputCacheCreation") or usage.get("cacheCreationTokens") or 0
                )
            model = rec.get("model")
            if model and (not stats["model"] or stats["model"] in ("kimi", "kimi_code")):
                # usage.record model often "longcat/LongCat-2.0" or
                # "kimi-code/kimi-for-coding"; keep the alias tail as the
                # model id so stats bucket correctly per provider.
                stats["model"] = str(model).split("/")[-1] if "/" in str(model) else str(model)
        elif rtype == "config.update":
            # New-format (.kimi-code) records the active model via a dedicated
            # config.update record (modelAlias). Use it as the fallback when
            # no usage.record has been seen yet or the value is still initial.
            alias = rec.get("modelAlias")
            m = rec.get("model")
            cand = alias or m
            if cand and (not stats["model"] or stats["model"] in ("kimi", "kimi_code")):
                stats["model"] = str(cand).split("/")[-1] if "/" in str(cand) else str(cand)
        elif rtype == "full_compaction.begin":
            stats["compactions"] += 1

    stats["assistant_messages"] = len(text_turns)
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    # Kimi Code usage.record: inputOther is non-cached; inputCacheRead is separate.
    attach_cache_hit_rates(stats, input_includes_cache=False)
    return stats


def codex_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """
    Extract messages from Codex rollout session.

    Codex format:
      - event_msg with payload.type=user_message: payload.message (user text)
      - event_msg with payload.type=agent_message: payload.message (assistant text)
      - response_item with payload.type=message: payload.content[].text (assistant)
      - response_item with payload.type=reasoning: thinking blocks
    """
    p = Path(session_path)
    if not p.exists() or not p.is_file():
        return

    count = 0
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")
        ts = rec.get("timestamp", payload.get("ts", ""))

        if rtype == "event_msg" and ptype == "user_message" and role in ("user", "both"):
            text = payload.get("message", "").strip()
            if text:
                yield {"role": "USER", "timestamp": str(ts), "text": text[:500]}
                count += 1
                if limit and count >= limit:
                    return

        elif rtype == "event_msg" and ptype == "agent_message" and role in ("assistant", "both"):
            text = payload.get("message", "").strip()
            if text:
                yield {"role": "ASSISTANT", "timestamp": str(ts), "text": text[:500]}
                count += 1
                if limit and count >= limit:
                    return

        elif rtype == "response_item" and ptype == "message" and role in ("assistant", "both"):
            content = payload.get("content", [])
            if isinstance(content, list):
                texts = []
                for c in content:
                    if isinstance(c, dict):
                        ct = c.get("type", "")
                        if ct in ("output_text", "text"):
                            t = c.get("text", "").strip()
                            if t:
                                texts.append(t)
                if texts:
                    yield {"role": "ASSISTANT", "timestamp": str(ts), "text": "\n".join(texts)[:500]}
                    count += 1
                    if limit and count >= limit:
                        return

        elif rtype == "response_item" and ptype == "reasoning" and role in ("assistant", "both") and thinking_limit != -1:
            summary = payload.get("summary", "")
            if isinstance(summary, list):
                texts = [s.get("text", "") for s in summary if isinstance(s, dict) and s.get("text")]
                text = " ".join(texts)
            elif isinstance(summary, str):
                text = summary
            else:
                content = payload.get("content", [])
                text = ""
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("text"):
                            text = c["text"]
                            break
            text = text.strip()
            if text:
                if thinking_limit > 0:
                    text = text[:thinking_limit]
                yield {"role": "ASSISTANT", "timestamp": str(ts), "text": "[THINKING] " + text[:300]}
                count += 1
                if limit and count >= limit:
                    return

def codex_session_stats_dedicated(session_path):
    """Stats for Codex using dedicated extractor."""
    p = Path(session_path)
    stats = _empty_stats("codex")
    stats["slug"] = _codex_id_from_path(p)
    if not p.exists() or not p.is_file():
        return stats
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")
        ts = rec.get("timestamp", "")
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not stats["started"] or nts < stats["started"]:
                    stats["started"] = nts
                if nts > stats["ended"]:
                    stats["ended"] = nts
        if rtype == "session_meta":
            # Prefer meta.cwd / model when present
            model = payload.get("model") or payload.get("model_provider")
            if model and (not stats["model"] or stats["model"] == "codex"):
                stats["model"] = str(model)
            git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
            branch = git.get("branch") or payload.get("git_branch")
            if branch:
                stats["branch"] = str(branch)
        if rtype == "event_msg" and ptype == "user_message":
            stats["user_messages"] += 1
        elif rtype == "event_msg" and ptype == "agent_message":
            stats["assistant_messages"] += 1
        elif rtype == "response_item" and ptype == "message":
            # Skip role=user/developer here — same turn is already counted via
            # event_msg.user_message; only assistant content is additive.
            if payload.get("role", "assistant") == "assistant":
                stats["assistant_messages"] += 1
        elif rtype == "response_item" and ptype in (
            "function_call", "custom_tool_call", "tool_search_call", "local_shell_call",
        ):
            stats["tool_calls"] += 1
        elif rtype == "response_item" and ptype in (
            "function_call_output", "custom_tool_call_output", "tool_search_output",
        ):
            output = payload.get("output", "")
            if isinstance(output, dict):
                output = output.get("body") or output.get("text") or ""
            if isinstance(output, str) and ("Exit Code:" in output and "Exit Code: 0" not in output):
                stats["errors"] += 1
            elif payload.get("success") is False:
                stats["errors"] += 1
        elif rtype == "event_msg" and ptype == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            # Current Codex nests counters under total_token_usage / last_token_usage.
            # Older flat keys remain as fallback. Snapshots are cumulative → take max.
            total_u = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
            last_u = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
            inp = int(
                total_u.get("input_tokens")
                or info.get("total_input_tokens")
                or info.get("input_tokens")
                or 0
            )
            out = int(
                total_u.get("output_tokens")
                or info.get("total_output_tokens")
                or info.get("output_tokens")
                or 0
            )
            cache = int(
                total_u.get("cached_input_tokens")
                or last_u.get("cached_input_tokens")
                or info.get("cached_input_tokens")
                or info.get("cache_read_tokens")
                or info.get("cached_tokens")
                or 0
            )
            create = int(
                total_u.get("cache_creation_input_tokens")
                or info.get("cache_creation_tokens")
                or info.get("cache_write_tokens")
                or 0
            )
            if inp > stats["input_tokens"]:
                stats["input_tokens"] = inp
            if out > stats["output_tokens"]:
                stats["output_tokens"] = out
            if cache > stats["cache_read_tokens"]:
                stats["cache_read_tokens"] = cache
            if create > stats["cache_create_tokens"]:
                stats["cache_create_tokens"] = create
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    # Codex total_token_usage.input_tokens includes cached_input_tokens.
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return stats

# ── ZCode / DIM / DimCode adapters (delegates to _adapters_zcode.py)
from echolib._adapters_zcode import (
    zcode_aggregate_model_usage, zcode_family_usage_report,
    zcode_tool_usage_stats, zcode_turn_usage_stats,
    zcode_list_sessions, zcode_session_stats,
    zcode_extract_messages, zcode_extract_tools, zcode_session_path,
    zcode_db_list_sessions, zcode_db_session_stats, zcode_db_extract_tools,
    zcode_db_extract_messages,
    dim_list_sessions, dim_session_stats, dim_extract_messages,
    dim_extract_tools, dim_session_path,
    dimcode_list_sessions, dimcode_session_stats, dimcode_extract_messages,
    dimcode_extract_tools, dimcode_session_path,
)


def reasonix_list_sessions(cwd=None, limit=50, keyword=""):
    """List Reasonix sessions from ~/.reasonix/sessions/."""
    sessions = []
    if not REASONIX_DIR.exists():
        return sessions
    for jf in sorted(REASONIX_DIR.glob("*.jsonl"), reverse=True):
        if jf.name.endswith(".events.jsonl"):
            continue
        try:
            started = ""
            model = ""
            for rec in _iter_jsonl(jf):
                ts = rec.get("timestamp", rec.get("ts", ""))
                if ts and not started:
                    started = _normalize_timestamp(ts)
                if rec.get("model") and not model:
                    model = rec["model"]
                if started and model:
                    break
            sessions.append({
                "id": jf.stem, "title": f"Reasonix {jf.stem[:20]}",
                "created": started, "modified": "",
                "message_count": 0, "path": str(jf),
                "agent": "Reasonix", "model": model,
            })
        except OSError:
            continue
    if keyword:
        keyword_lower = keyword.lower()
        sessions = [s for s in sessions if keyword_lower in s.get("title", "").lower()]
    return sessions[:limit]

def reasonix_session_stats(session_path):
    """Stats for Reasonix flat role/content format."""
    p = Path(session_path)
    stats = _empty_stats("reasonix")
    stats["slug"] = p.stem
    if not p.exists() or not p.is_file():
        return stats
    for rec in _iter_jsonl(p):
        role = rec.get("role", rec.get("type", ""))
        ts = rec.get("timestamp", rec.get("ts", ""))
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not stats["started"] or nts < stats["started"]:
                    stats["started"] = nts
                if nts > stats["ended"]:
                    stats["ended"] = nts
        if not stats["model"] and rec.get("model"):
            stats["model"] = rec["model"]
        if role == "user":
            content = rec.get("content", "")
            if (isinstance(content, str) and content.strip()) or (isinstance(content, list) and content):
                stats["user_messages"] += 1
        elif role in ("assistant", "model"):
            stats["assistant_messages"] += 1
        elif role == "tool":
            stats["tool_calls"] += 1
        if role in ("assistant", "model") and isinstance(rec.get("tool_calls"), list):
            stats["tool_calls"] += len(rec["tool_calls"])
        if role == "tool":
            content = rec.get("content", "")
            if isinstance(content, str) and ("error" in content.lower() or "Error" in content):
                stats["errors"] += 1
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    attach_cache_hit_rates(stats)
    return stats

def reasonix_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from Reasonix flat format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        role_val = rec.get("role", rec.get("type", ""))
        ts = rec.get("timestamp", rec.get("ts", ""))
        nt = _normalize_timestamp(ts) if ts else ""
        text = _extract_content_text(rec.get("content", ""), max_len=500)
        if not text:
            continue
        if role in ("user", "both") and role_val == "user":
            cleaned = _strip_system_reminder(text)
            if cleaned:
                yield {"role": "USER", "timestamp": nt, "text": cleaned}
                count += 1
                if limit and count >= limit:
                    return
            continue
        if role in ("assistant", "both") and role_val in ("assistant", "model"):
            yield {"role": "ASSISTANT", "timestamp": nt, "text": text}
            count += 1
            if limit and count >= limit:
                return

def reasonix_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from Reasonix format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        role = rec.get("role", rec.get("type", ""))
        ts = rec.get("timestamp", rec.get("ts", ""))
        nt = _normalize_timestamp(ts) if ts else ""

        if role in ("assistant", "model") and isinstance(rec.get("tool_calls"), list):
            for tc in rec["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name", tc.get("function", {}).get("name", ""))
                if tool_filter and name != tool_filter:
                    continue
                args = tc.get("arguments", tc.get("function", {}).get("arguments", ""))
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                yield {"timestamp": nt, "name": str(name), "status": "ok",
                       "key_input": str(args)[:150] if args else "", "result_preview": ""}
                count += 1
                if limit and count >= limit:
                    return

        if role == "tool":
            name = rec.get("name", rec.get("tool_name", "tool"))
            if tool_filter and name != tool_filter:
                continue
            content = rec.get("content", "")
            is_error = isinstance(content, str) and ("error" in content.lower())
            if errors_only and not is_error:
                continue
            yield {"timestamp": nt, "name": str(name), "status": "error" if is_error else "ok",
                   "key_input": "", "result_preview": str(content)[:80]}
            count += 1
            if limit and count >= limit:
                return

def reasonix_session_path(cwd, session_id=None):
    """Resolve Reasonix session path."""
    if session_id:
        candidate = REASONIX_DIR / f"{session_id}.jsonl"
        if candidate.exists():
            return str(candidate)
    return str(REASONIX_DIR)

ADAPTER_REGISTRY = {}

def register_adapter(name, display_name, **fns):
    """注册一个环境适配器。

    Args:
        name: 唯一标识符（如 "grok", "codex"）
        display_name: 显示名称（如 "Grok Build"）
        **fns: 必须包含 list_sessions, session_stats, extract_messages, extract_tools, session_path
    """
    ADAPTER_REGISTRY[name] = {"name": name, "display_name": display_name, **fns}


# ── Adapter registrations ──
register_adapter("claude", "Claude Code",
    list_sessions=broad_list_claude_sessions,
    session_stats=session_stats,
    extract_messages=extract_messages,
    extract_tools=extract_tools,
    session_path=lambda cwd, sid=None: find_project_dir(cwd or os.getcwd()),
)

register_adapter("grok", "Grok Build",
    list_sessions=grok_list_sessions,
    session_stats=_grok_session_stats,
    extract_messages=_grok_extract_messages,
    extract_tools=grok_extract_tools,  # 保留：双文件 events+chat_history 关联
    session_path=grok_session_path,
)

register_adapter("kimi_code", "Kimi Code",
    list_sessions=kimi_code_list_sessions,
    session_stats=kimi_code_session_stats,
    extract_messages=kimi_code_extract_messages,
    extract_tools=kimi_code_extract_tools,  # 保留：处理嵌套 event.tool.call 结构
    session_path=kimi_code_session_path,
)

register_adapter("kimi", "Kimi (standalone)",
    list_sessions=kimi_list_sessions,
    session_stats=kimi_session_stats,
    extract_messages=kimi_extract_messages,
    extract_tools=kimi_extract_tools,
    session_path=kimi_session_path,
)

register_adapter("codex", "Codex (OpenAI)",
    list_sessions=codex_list_sessions,
    session_stats=codex_session_stats_dedicated,
    extract_messages=codex_extract_messages,
    extract_tools=codex_extract_tools,  # 保留：exit code 错误检测
    session_path=codex_session_path,
)

register_adapter("cursor", "Cursor",
    list_sessions=cursor_list_sessions,
    session_stats=cursor_session_stats,
    extract_messages=cursor_extract_messages,
    extract_tools=cursor_extract_tools,
    session_path=cursor_session_path,
)

register_adapter("workbuddy", "WorkBuddy",
    list_sessions=workbuddy_list_sessions,
    session_stats=workbuddy_session_stats,  # 保留：providerData 含 model/token
    extract_messages=workbuddy_extract_messages,
    extract_tools=workbuddy_extract_tools,  # 保留：exit code 错误检测
    session_path=workbuddy_session_path,
)

register_adapter("trae_cn", "Trae CN (ByteDance)",
    list_sessions=trae_list_sessions,
    session_stats=trae_session_stats,  # 保留：summary-only 非标准格式
    extract_messages=trae_extract_messages,
    extract_tools=trae_extract_tools,
    session_path=trae_session_path,
)

register_adapter("zcode", "ZCode (Z-AI)",
    list_sessions=zcode_list_sessions,
    session_stats=zcode_session_stats,
    extract_messages=zcode_extract_messages,
    extract_tools=zcode_extract_tools,
    session_path=zcode_session_path,
)

register_adapter("dim", "DIM (Memory)",
    list_sessions=dim_list_sessions,
    session_stats=dim_session_stats,
    extract_messages=dim_extract_messages,
    extract_tools=dim_extract_tools,
    session_path=dim_session_path,
)

register_adapter("dimcode", "DimCode (SQLite)",
    list_sessions=dimcode_list_sessions,
    session_stats=dimcode_session_stats,
    extract_messages=dimcode_extract_messages,
    extract_tools=dimcode_extract_tools,
    session_path=dimcode_session_path,
)

register_adapter("reasonix", "Reasonix",
    list_sessions=reasonix_list_sessions,
    session_stats=reasonix_session_stats,
    extract_messages=reasonix_extract_messages,
    extract_tools=reasonix_extract_tools,
    session_path=reasonix_session_path,
)

register_adapter("universal", "Universal",
    list_sessions=universal_list_sessions,
    session_stats=universal_session_stats,
    extract_messages=universal_extract_messages,
    extract_tools=universal_extract_tools,
    session_path=universal_session_path,
)
