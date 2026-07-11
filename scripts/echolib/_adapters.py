import json
import math
import os
import re
import sqlite3
import sys
import concurrent.futures
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import time as _time

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
    DIMCODE_DB_PATH,
    DIM_DIR,
    GROK_DIR,
    KIMI_CODE_DIR,
    KIMI_DIR,
    REASONIX_DIR,
    TRAE_DIR,
    WORKBUDDY_DIR,
    ZCODE_DIR,
    _extract_content_text,
    _iter_jsonl,
    _match_call_results,
    _strip_system_reminder,
)
from echolib._models import (
    Record,
    SessionMeta,
)





def _encode_grok_cwd(cwd):
    """Encode a path to Grok's URL-encoded format."""
    import urllib.parse
    return urllib.parse.quote(cwd, safe='')

def _decode_grok_cwd(encoded):
    """Decode Grok's URL-encoded path back to filesystem path."""
    import urllib.parse
    return urllib.parse.unquote(encoded)

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
                # Build full_path from cwd + session_id
                encoded_cwd = _encode_grok_cwd(session_cwd)
                full_path = str(GROK_DIR / encoded_cwd / sid)
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
        except Exception:
            pass  # Fall through to filesystem scan

    # Fallback: scan summary.json files
    import urllib.parse
    entries = []
    for d in sorted(GROK_DIR.iterdir()):
        if not d.is_dir():
            continue
        session_cwd = _decode_grok_cwd(d.name)
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
                updated = info.get("updated_at") or summary.get("updated_at") or ""
                title = (summary.get("session_summary")
                         or summary.get("generated_title")
                         or summary.get("summary")
                         or "")
                entries.append(SessionMeta(
                    session_id=sid,
                    full_path=str(session_dir),
                    created=_normalize_timestamp(created),
                    modified=_normalize_timestamp(updated),
                    message_count=summary.get("num_messages", 0),
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
    """
    if not GROK_DIR.exists():
        return None

    encoded_cwd = _encode_grok_cwd(cwd)
    cwd_dir = GROK_DIR / encoded_cwd
    if not cwd_dir.exists():
        return None

    if session_id:
        session_dir = cwd_dir / session_id
        if session_dir.is_dir():
            return session_dir
        return None

    # Find most recent session
    sessions = sorted(
        [d for d in cwd_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
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
    Get session statistics for a Kimi Code session.

    Reads wire.jsonl and state.json. Returns a dict compatible with session_stats().
    """
    session_dir = Path(session_dir)
    wire_file = session_dir / "wire.jsonl"
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

    # Count from wire.jsonl
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
        except OSError:
            pass

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

def kimi_code_list_sessions(cwd=None, limit=50, keyword=""):
    """
    List Kimi Code sessions from ~/.kimi-code/sessions/.

    Directory structure: ~/.kimi-code/sessions/<project_dir>/<session_uuid>/
    Each session has state.json + agents/main/wire.jsonl
    """
    if not KIMI_CODE_DIR.exists():
        return []

    entries = []
    for project_dir in sorted(KIMI_CODE_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        for session_dir in sorted(project_dir.iterdir()):
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

            sid = session_dir.name.replace("session_", "")
            title = (state.get("title") or "")[:100]
            created_at = state.get("createdAt", "")
            updated_at = state.get("updatedAt", "")

            # Count messages and extract first user prompt from wire.jsonl
            msg_count = 0
            first_msg = title
            if wire_file.exists():
                try:
                    with open(wire_file, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            try:
                                rec = json.loads(line.strip())
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if rec.get("type") == "turn.prompt":
                                msg_count += 1
                                if first_msg == title:
                                    inputs = rec.get("input", [])
                                    for inp in (inputs if isinstance(inputs, list) else []):
                                        if isinstance(inp, dict) and inp.get("type") == "text":
                                            first_msg = inp["text"][:100].replace("\n", " ")
                                            break
                except OSError:
                    pass

            # Filter by keyword
            if keyword and keyword.lower() not in title.lower() and keyword.lower() not in first_msg.lower():
                continue

            entries.append(SessionMeta(
                session_id=sid,
                full_path=str(session_dir),
                created=created_at,
                modified=updated_at or created_at,
                message_count=msg_count,
                git_branch="",
                summary=title,
                first_prompt=first_msg,
                project_path=str(project_dir),
            ))

    entries.sort(key=lambda e: str(e.created), reverse=True)
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

    # First pass: collect tool results by toolCallId
    results_by_id = {}
    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("type") != "context.append_loop_event":
                    continue
                event = rec.get("event", {})
                if not isinstance(event, dict) or event.get("type") != "tool.result":
                    continue
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
    except OSError:
        pass

    # Second pass: yield tool calls
    count = 0
    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("type") != "context.append_loop_event":
                    continue
                event = rec.get("event", {})
                if not isinstance(event, dict) or event.get("type") != "tool.call":
                    continue

                name = event.get("name", "")
                if tool_filter and name != tool_filter:
                    continue

                tid = event.get("toolCallId", event.get("uuid", ""))
                ts = rec.get("time", rec.get("timestamp", ""))
                args = event.get("args", event.get("arguments", ""))
                if isinstance(args, dict):
                    key_input = json.dumps(args, ensure_ascii=False)[:150]
                elif isinstance(args, str):
                    key_input = args[:150]
                else:
                    key_input = ""

                # Get result
                result_info = results_by_id.get(tid, {"preview": "(no result)", "is_error": False})
                status = "error" if result_info["is_error"] else "ok"

                if errors_only and status != "error":
                    continue

                if limit and count >= limit:
                    return
                yield {
                    "timestamp": _normalize_timestamp(ts) if ts else "",
                    "name": name,
                    "status": status,
                    "key_input": key_input,
                    "result_preview": result_info["preview"],
                }
                count += 1
    except OSError:
        pass

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
    """
    agents = agent_filter if isinstance(agent_filter, (list, tuple)) else \
             list(ADAPTER_REGISTRY.keys()) if agent_filter is None else [agent_filter]

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
                    # Handle both dict and object return types
                    if isinstance(s, dict):
                        all_sessions.append({
                            "agent": s.get("agent", display),
                            "session_id": s.get("session_id", s.get("id", "")),
                            "created": s.get("created", ""),
                            "summary": s.get("summary", s.get("title", "")),
                            "first_prompt": s.get("first_prompt", ""),
                            "msg_count": s.get("msg_count", s.get("message_count", 0)),
                            "full_path": s.get("full_path", s.get("path", "")),
                        })
                    else:
                        all_sessions.append({
                            "agent": display,
                            "session_id": s.session_id,
                            "created": s.created,
                            "summary": s.summary,
                            "first_prompt": s.first_prompt,
                            "msg_count": s.message_count,
                            "full_path": s.full_path,
                        })
            except Exception as exc:
                # Adapter error — skip silently; this adapter's sessions are omitted
                pass

    all_sessions.sort(key=lambda s: str(s.get("created", "") or ""), reverse=True)
    # 将空时间戳的会话挪到后面，让有时间戳的会话优先展示
    with_time = sorted([s for s in all_sessions if s.get("created")],
                       key=lambda s: str(s["created"]), reverse=True)
    without_time = [s for s in all_sessions if not s.get("created")]
    all_sessions = with_time + without_time
    return all_sessions[:limit]

def cross_tool_session_stats(session_path):
    """
    Get statistics for a session from any supported agent.
    Auto-detects agent type and dispatches via registry.
    """
    agent = detect_agent_type(session_path)
    if agent in ADAPTER_REGISTRY:
        return ADAPTER_REGISTRY[agent]["session_stats"](session_path)
    return session_stats(session_path)

def dispatch_resolve_agent(path):
    """Detect agent type from path and map to a registered adapter name.

    Resolution order:
    1. Path-based detection (detect_agent_type) → registered adapter
    2. Content-based detection (format-detector signatures) → registered adapter
    3. Fall back to "universal" (SchemaProbe auto-discovery)

    The universal adapter uses SchemaProbe to sample records and infer field
    mappings, so it can handle any JSONL format without dedicated adapters.
    """
    atype = detect_agent_type(path)
    if atype in ADAPTER_REGISTRY:
        return atype
    # Try content-based detection for unknown paths
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

    return best_format if best_score >= 4 else None

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

def codex_list_sessions(cwd=None, limit=50, keyword=""):
    """List Codex sessions from ~/.codex/sessions/ using session_index.jsonl."""
    index_path = CODEX_DIR / "session_index.jsonl"
    if not index_path.exists():
        return codex_list_sessions_fallback(cwd, limit, keyword)

    sessions = []
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
            title = entry.get("thread_name", "")
            updated = entry.get("updated_at", "")

            if keyword and keyword.lower() not in title.lower():
                continue

            rollout_path = _find_codex_rollout(sid)
            msg_count = 0
            first_prompt = ""
            if rollout_path:
                msg_count, first_prompt = _codex_quick_scan(rollout_path)

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

    sessions.sort(key=lambda s: str(s.created or ""), reverse=True)
    return sessions[:limit]

def codex_list_sessions_fallback(cwd=None, limit=50, keyword=""):
    """Fallback: scan rollout files directly when no index exists."""
    sessions_dir = CODEX_DIR / "sessions"
    if not sessions_dir.exists():
        return []

    sessions = []
    for rollout in sorted(sessions_dir.rglob("rollout-*.jsonl"), reverse=True):
        name = rollout.stem
        parts = name.split("-")
        sid = parts[-1] if len(parts) >= 2 else name
        msg_count, first_prompt = _codex_quick_scan(rollout)
        mtime = _normalize_timestamp(rollout.stat().st_mtime)
        if keyword and keyword.lower() not in first_prompt.lower():
            continue
        sessions.append(SessionMeta(
            session_id=sid, full_path=str(rollout),
            created=mtime, modified=mtime,
            message_count=msg_count, git_branch="", summary="",
            first_prompt=first_prompt[:200] if first_prompt else "",
            project_path="",
        ))
    return sessions[:limit]

def _find_codex_rollout(session_id):
    """Find a rollout file by session UUID."""
    sessions_dir = CODEX_DIR / "sessions"
    if not sessions_dir.exists():
        return None
    for p in sessions_dir.rglob(f"*{session_id}*.jsonl"):
        return p
    return None

def _codex_quick_scan(rollout_path):
    """Quick scan: count user messages and extract first prompt."""
    user_count = 0
    first_prompt = ""
    for rec in _iter_jsonl(rollout_path):
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if rtype == "event_msg" and payload.get("type") == "user_message":
            user_count += 1
            if not first_prompt:
                first_prompt = payload.get("message", "")[:200]
    return user_count, first_prompt

def codex_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from a Codex rollout session."""
    path = Path(session_dir)
    if not path.exists():
        return
    calls = {}
    outputs = {}
    for rec in _iter_jsonl(path):
        if rec.get("type") != "response_item":
            continue
        payload = rec.get("payload", {})
        ptype = payload.get("type", "")
        ts = rec.get("timestamp", "")
        if ptype in ("function_call", "custom_tool_call"):
            name = payload.get("name", "")
            if tool_filter and name != tool_filter:
                continue
            call_id = payload.get("call_id", "")
            args = payload.get("arguments", payload.get("input", ""))
            calls[call_id] = {"name": name, "ts": ts, "input_preview": str(args)[:150] if args else ""}
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id", "")
            output = payload.get("output", "")
            is_error = False
            if isinstance(output, str):
                if "Exit Code:" in output and "Exit Code: 0" not in output:
                    is_error = True
                if "Failed" in output:
                    is_error = True
            outputs[call_id] = {"preview": str(output)[:150].replace("\\n", " ") if output else "", "is_error": is_error}
    yield from _match_call_results(calls, outputs, errors_only, limit)

def codex_session_path(cwd, session_id=None):
    """Find a Codex session file."""
    sessions_dir = CODEX_DIR / "sessions"
    if not sessions_dir.exists():
        return None
    if session_id:
        for p in sessions_dir.rglob(f"*{session_id}*.jsonl"):
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


def trae_list_sessions(cwd=None, limit=50, keyword=""):
    """List Trae CN sessions from ~/.trae-cn/memory/projects/."""
    memory_dir = TRAE_DIR / "memory" / "projects"
    if not memory_dir.exists():
        return []
    sessions = {}
    for project_dir in sorted(memory_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        for date_dir in sorted(project_dir.iterdir()):
            if not date_dir.is_dir() or not date_dir.name.isdigit():
                continue
            for jsonl_file in sorted(date_dir.glob("session_memory_*.jsonl")):
                sid = jsonl_file.stem.replace("session_memory_", "")
                if sid not in sessions:
                    sessions[sid] = {
                        "path": str(jsonl_file), "project": project_dir.name,
                        "intents": [], "date": date_dir.name,
                        "mtime": _normalize_timestamp(jsonl_file.stat().st_mtime),
                    }
                sessions[sid]["intents"].extend(_trae_extract_intents(jsonl_file))
    result = []
    for sid, info in sessions.items():
        first_intent = info["intents"][0] if info["intents"] else ""
        summary = " | ".join(info["intents"][:3])
        if keyword and keyword.lower() not in summary.lower():
            continue
        result.append(SessionMeta(
            session_id=sid, full_path=info["path"],
            created=info["mtime"], modified=info["mtime"],
            message_count=len(info["intents"]), git_branch="",
            summary=summary[:100], first_prompt=first_intent[:200],
            project_path=info["project"],
        ))
    result.sort(key=lambda s: str(s.created or ""), reverse=True)
    return result[:limit]

def _trae_extract_intents(jsonl_path):
    """Extract intent strings from a Trae CN memory JSONL."""
    return [rec.get("intent", "") for rec in _iter_jsonl(jsonl_path) if rec.get("intent")]

def trae_session_stats(session_dir):
    """Get stats for a Trae CN session (summary-level only)."""
    path = Path(session_dir)
    if not path.exists():
        return _empty_stats("trae-cn")
    stats = _empty_stats("trae-cn")
    stats["model"] = "trae-cn (summary only)"
    stats["slug"] = path.stem
    for rec in _iter_jsonl(path):
        ts = rec.get("message_summary_time", "")
        if ts:
            if not stats["started"] or ts < stats["started"]:
                stats["started"] = ts
            if ts > stats["ended"]:
                stats["ended"] = ts
        intent = rec.get("intent", "")
        if intent:
            stats["user_messages"] += 1
        outcome = rec.get("outcome", "")
        if outcome:
            stats["assistant_messages"] += 1
        actions = rec.get("actions", [])
        if isinstance(actions, list):
            stats["tool_calls"] += len(actions)
    stats["summary"] = f"Trae CN summary: {stats['user_messages']} turns"
    return stats

def trae_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """Extract summarized messages from a Trae CN session."""
    path = Path(session_dir)
    if not path.exists():
        return
    count = 0
    for rec in _iter_jsonl(path):
        ts = rec.get("message_summary_time", "")
        if role in ("user", "both"):
            intent = rec.get("intent", "")
            if intent:
                yield {"role": "USER", "timestamp": ts, "text": f"[意图] {intent}"}
                count += 1
        if role in ("assistant", "both"):
            parts = []
            actions = rec.get("actions", [])
            if actions:
                parts.append("[动作] " + " | ".join(actions))
            outcome = rec.get("outcome", "")
            if outcome:
                parts.append(f"[结果] {outcome}")
            learned = rec.get("learned", [])
            if learned:
                parts.append("[收获] " + " | ".join(learned))
            if parts:
                yield {"role": "ASSISTANT", "timestamp": ts, "text": "\\n".join(parts)}
                count += 1
        if limit and count >= limit:
            return

def trae_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract action summaries from a Trae CN session."""
    path = Path(session_dir)
    if not path.exists():
        return
    count = 0
    for rec in _iter_jsonl(path):
        ts = rec.get("message_summary_time", "")
        actions = rec.get("actions", [])
        if not isinstance(actions, list):
            continue
        for action in actions:
            if tool_filter and tool_filter.lower() not in action.lower():
                continue
            if limit and count >= limit:
                return
            yield {"timestamp": ts, "name": f"[trae-action] {action[:50]}", "status": "ok", "key_input": action[:150], "result_preview": ""}
            count += 1

def trae_session_path(cwd, session_id=None):
    """Find a Trae CN session file."""
    memory_dir = TRAE_DIR / "memory" / "projects"
    if not memory_dir.exists():
        return None
    if session_id:
        for p in memory_dir.rglob(f"session_memory_{session_id}.jsonl"):
            return p
    return None

def universal_session_path(cwd, session_id=None):
    """Universal path finder: search for any JSONL file matching session_id."""
    home = Path.home()
    if session_id:
        for p in home.rglob(f"*{session_id}*.jsonl"):
            return p
    return None

_SCHEMA_PROBE_CACHE = {}  # path → schema dict

def _probe_schema(jsonl_path, force=False):
    """
    探测 JSONL 文件的字段结构，返回 schema 描述字典。
    结果会缓存，重复调用直接命中。

    Returns:
        dict with keys:
        - style: "nested_message" | "nested_payload" | "flat" | "unknown"
        - type_path: list of keys to find role (e.g. ["type"] or ["message", "type"])
        - content_path: list of keys to navigate to text content
        - timestamp_field: which field holds timestamps
        - model_field: which field holds model name (if any)
        - tool_style: "type_based" if tool calls have a distinct type value
    """
    path_str = str(jsonl_path)
    if not force and path_str in _SCHEMA_PROBE_CACHE:
        return _SCHEMA_PROBE_CACHE[path_str]

    # 采样前 30 条记录
    samples = []
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 30:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    samples.append(rec)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    schema = {
        "style": "unknown",
        "type_path": ["type"],        # where to find role indicator
        "content_path": ["content"],  # where to find text content
        "timestamp_field": "timestamp",
        "model_field": None,
        "tool_style": None,
    }

    if not samples:
        _SCHEMA_PROBE_CACHE[path_str] = schema
        return schema

    # ---------- 1. 检测嵌套结构 ----------
    has_message_nest = any(isinstance(r.get("message"), dict) for r in samples)
    has_payload_nest = any(isinstance(r.get("payload"), dict) for r in samples)
    # Grok/Cline style: flat structure with type/content at top level
    has_flat_type = any(r.get("type") in ("user", "assistant", "system", "tool_result",
                                            "reasoning", "tool_use", "tool_call")
                        for r in samples)

    if has_message_nest:
        schema["style"] = "nested_message"
        # 内容可能在 message.content 或 message.payload.user_input
        # 检查 message 内部更深层的结构
        msg_content_found = False
        for r in samples:
            msg = r.get("message", {})
            if not isinstance(msg, dict):
                continue
            # Kimi style: message.payload.user_input[].text
            payload = msg.get("payload", {})
            if isinstance(payload, dict) and payload.get("user_input"):
                schema["content_path"] = ["message", "payload", "user_input"]
                msg_content_found = True
                break
            # Claude style: message.content (string or list of blocks)
            content = msg.get("content")
            if content:
                # Check if content has text-like data
                if isinstance(content, (str, list)):
                    schema["content_path"] = ["message", "content"]
                    msg_content_found = True
                    break
        if not msg_content_found:
            schema["content_path"] = ["message", "content"]
        # Model field
        for r in samples:
            msg = r.get("message", {})
            if isinstance(msg, dict) and msg.get("model"):
                schema["model_field"] = ["message", "model"]
                break
    elif has_payload_nest:
        schema["style"] = "nested_payload"
        # Codex style: payload.content[] where blocks have type "input_text"/"output_text"
        # 以及 payload.role 存放角色信息
        schema["content_path"] = ["payload", "content"]
    elif has_flat_type:
        schema["style"] = "flat"
        # Grok/Cline style: content is string or list of text blocks at top level
        schema["content_path"] = ["content"]
        # Model field for flat formats (Grok uses model_id)
        for r in samples:
            if r.get("type") == "assistant" and r.get("model_id"):
                schema["model_field"] = "model_id"
                break

    # ---------- 2. 确定 type/role 字段路径 ----------
    # 常见的 user/assistant 指示值
    # 尽可能覆盖已知格式的角色指示值
    user_vals = frozenset({
        "user", "human", "turn.prompt", "user_message", "turnbegin",
        "user_msg", "prompt",
        # zcode trace: turn_started contains user input
        "turn_started",
    })
    assistant_vals = frozenset({
        "assistant", "ai", "bot", "agent", "text", "content.part",
        "agent_message", "contentpart", "assistant_msg",
        "reasoning",
        # Kimi Code: context.append_loop_event contains assistant-generated content
        "context.append_loop_event",
        # zcode trace: model_complete has the final response content
        "model_complete",
    })
    tool_vals = frozenset({
        "tool_call", "function_call", "tool.call", "toolcall",
        "toolcall", "functioncall",
        # zcode trace: tool_call_scheduled has toolName and input
        "tool_call_scheduled",
    })

    # 候选字段路径：从最外层到最内层
    candidate_paths = []
    # 顶层字段
    for r in samples:
        for key in ("type", "role"):
            if r.get(key):
                candidate_paths.append([key])
                break
        break
    # 嵌套字段
    if has_message_nest:
        candidate_paths.append(["message", "type"])
        candidate_paths.append(["message", "role"])
    if has_payload_nest:
        candidate_paths.append(["payload", "type"])
        candidate_paths.append(["payload", "role"])

    # 测试每条路径，找能区分 user/assistant 的最佳路径
    # 评分策略：有 user 匹配 AND assistant 匹配 > 只有一类匹配 > 无匹配
    best_path = ["type"]
    best_score = -1
    for path in candidate_paths:
        n_user = 0
        n_ass = 0
        for r in samples:
            cur = r
            for key in path:
                if isinstance(cur, dict):
                    cur = cur.get(key, {})
                else:
                    cur = {}
                    break
            val = str(cur).lower() if not isinstance(cur, dict) else ""
            if val in user_vals:
                n_user += 1
            elif val in assistant_vals:
                n_ass += 1
        # 评分：有区分度（user + ass > 0）> 只有一类 > 无匹配
        if n_user > 0 and n_ass > 0:
            score = 100 + n_user + n_ass
        elif n_user > 0 or n_ass > 0:
            score = n_user + n_ass
        else:
            score = 0
        # Tiebreaker: prefer shorter paths (top-level > nested) when scores are equal.
        # This prevents ['message', 'role'] from beating ['type'] in Kimi Code where
        # both paths can find user/assistant values, but ['type'] is the canonical
        # discriminator (turn.prompt vs context.append_loop_event).
        if score > best_score or (score == best_score and len(path) < len(best_path)):
            best_score = score
            best_path = path

    schema["type_path"] = best_path

    # ---------- 3. 检测工具调用模式 ----------
    for r in samples:
        # 顶层类型检测
        t = str(r.get("type", "")).lower()
        if t in tool_vals:
            schema["tool_style"] = "type_based"
            break
        # 嵌套 payload.type 检测（如 Codex payload.type = "function_call"）
        for nest_key in ("message", "payload"):
            nest = r.get(nest_key, {})
            if isinstance(nest, dict):
                nt = str(nest.get("type", "")).lower()
                if nt in tool_vals or "function_call" in nt:
                    schema["tool_style"] = "nested"
                    break
        # Kimi Code 风格：context.append_loop_event 中的 event.type=tool.call
        if r.get("type") == "context.append_loop_event":
            event = r.get("event", {})
            if isinstance(event, dict):
                et = str(event.get("type", "")).lower()
                if et in tool_vals or "tool.call" in et:
                    schema["tool_style"] = "nested_event"
                    break
        # Grok 风格：assistant 消息中有 tool_calls 数组
        if r.get("type") == "assistant" and isinstance(r.get("tool_calls"), list):
            schema["tool_style"] = "embedded_array"
            break
        if schema["tool_style"]:
            break

    # ---------- 4. 检测关键字段 ----------
    for key in ("timestamp", "time", "ts", "createTime", "created_at", "date", "updated_at", "updateTime"):
        if any(r.get(key) for r in samples):
            schema["timestamp_field"] = key
            break

    if not schema["model_field"]:
        # Check nested model field first (e.g. message.model for Claude)
        for key in ("model", "model_name", "model_id", "engine"):
            if any(r.get(key) for r in samples):
                schema["model_field"] = key
                break
        # Also check model_id inside assistant messages (Grok style)
        if not schema["model_field"]:
            for r in samples:
                if r.get("type") == "assistant" and r.get("model_id"):
                    schema["model_field"] = "model_id"
                    break

    _SCHEMA_PROBE_CACHE[path_str] = schema
    return schema

def _schema_get_text(schema, rec):
    """用 schema 从单条记录中提取文本内容。"""
    path = schema["content_path"]
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
                bt = block.get("text", "")
                if bt:
                    texts.append(str(bt))
            elif isinstance(block, str):
                texts.append(block)
        if texts:
            return "\n".join(texts)[:500]

    # Kimi 风格回退：message.payload.text
    if len(path) >= 2 and path[:2] == ["message", "payload"]:
        payload = rec.get("message", {}).get("payload", {})
        if isinstance(payload, dict):
            text = payload.get("text", "")
            if isinstance(text, str) and text.strip():
                return text[:500]
            user_input = payload.get("user_input", [])
            if isinstance(user_input, list):
                texts = [u.get("text", "") for u in user_input if isinstance(u, dict) and u.get("text")]
                if texts:
                    return "\n".join(texts)[:500]

    # Codex 风格回退：payload.content
    if schema.get("style") == "nested_payload":
        payload = rec.get("payload", {})
        if isinstance(payload, dict):
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

    # 回退：直接搜常见文本字段
    for key in ("text", "input", "prompt", "query", "message_text"):
        val = rec.get(key, "")
        if isinstance(val, str) and val.strip():
            return val[:500]
    return ""

def _schema_get_timestamp(schema, rec):
    """用 schema 从单条记录中提取时间戳。"""
    ts = rec.get(schema["timestamp_field"], "")
    if not ts:
        # Fallback: check common timestamp field variants
        for key in ("timestamp", "time", "ts", "createTime", "created_at"):
            if key != schema["timestamp_field"]:
                val = rec.get(key, "")
                if val:
                    return val
    return ts

def _schema_get_model(schema, rec):
    """用 schema 从单条记录中提取模型名。"""
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
        return str(current)
    return str(rec.get(path, ""))

def _schema_is_role(schema, rec, target):
    """判断记录是否匹配目标角色（支持嵌套路径）。"""
    type_path = schema.get("type_path", ["type"])
    cur = rec
    for key in type_path:
        if isinstance(cur, dict):
            cur = cur.get(key, "")
        else:
            cur = ""
            break
    val = str(cur).lower() if not isinstance(cur, dict) else ""
    if target == "user":
        return val in ("user", "human", "turn.prompt", "user_message", "turnbegin", "user_msg", "prompt",
                       "turn_started")
    elif target == "assistant":
        return val in ("assistant", "ai", "bot", "agent", "text", "content.part", "contentpart",
                       "agent_message", "assistant_msg", "reasoning", "response_item",
                       "context.append_loop_event", "model_complete")
    elif target == "tool_call":
        return val in ("tool_call", "function_call", "tool.call", "toolcall", "functioncall",
                       "toolcall", "tool_call_scheduled") or "function_call" in val
    return False

def _schema_is_user(schema, rec):
    return _schema_is_role(schema, rec, "user")

def _schema_is_assistant(schema, rec):
    return _schema_is_role(schema, rec, "assistant")

def _schema_is_tool_call(schema, rec):
    return _schema_is_role(schema, rec, "tool_call")

def universal_list_sessions(home_dir=None, env_name="unknown", limit=50, keyword=""):
    """Universal session discovery: find JSONL files in any environment directory."""
    if home_dir is None:
        home_dir = Path.home()
    search_dirs = []
    for pattern in ["sessions", "projects", "memory", "data"]:
        candidate = home_dir / pattern
        if candidate.exists():
            search_dirs.append(candidate)
    jsonl_files = []
    for search_dir in search_dirs:
        jsonl_files.extend(search_dir.rglob("*.jsonl"))
    if not jsonl_files:
        jsonl_files = list(home_dir.rglob("*.jsonl"))
    sessions = []
    for jf in sorted(jsonl_files, key=lambda p: p.stat().st_mtime, reverse=True):
        if jf.stat().st_size < 100:
            continue
        sid = jf.stem
        mtime = _normalize_timestamp(jf.stat().st_mtime)
        first_prompt = _universal_quick_scan(jf)
        if keyword and keyword.lower() not in first_prompt.lower():
            continue
        sessions.append(SessionMeta(
            session_id=sid, full_path=str(jf),
            created=mtime, modified=mtime,
            message_count=0, git_branch="",
            summary=f"[{env_name}] {jf.parent.name}",
            first_prompt=first_prompt[:200],
            project_path=str(jf.parent),
        ))
    return sessions[:limit]

def _universal_quick_scan(jsonl_path):
    """快速扫描：用 SchemaProbe 提取第一条用户消息。"""
    schema = _probe_schema(jsonl_path)
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if _schema_is_user(schema, rec):
                    text = _schema_get_text(schema, rec)
                    if text:
                        return text[:200]
                # 也尝试直接提取任何文本
                text = _schema_get_text(schema, rec)
                if text:
                    return text[:200]
    except OSError:
        pass
    return ""

def universal_session_stats(session_path):
    """Universal stats: 用 SchemaProbe 识别消息类型后统计。"""
    path = Path(session_path)
    stats = _empty_stats("unknown")
    stats["slug"] = path.stem
    if not path.exists():
        return stats
    schema = _probe_schema(session_path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                # 时间戳
                ts = _schema_get_timestamp(schema, rec)
                if ts:
                    nt = _normalize_timestamp(ts)
                    if nt:
                        if not stats["started"] or nt < stats["started"]:
                            stats["started"] = nt
                        if nt > stats["ended"]:
                            stats["ended"] = nt
                # 模型
                if not stats["model"]:
                    model = _schema_get_model(schema, rec)
                    if model:
                        stats["model"] = model
                # 角色计数 — 用 schema 判定，不再靠硬编码关键词
                if _schema_is_user(schema, rec):
                    stats["user_messages"] += 1
                elif _schema_is_assistant(schema, rec):
                    stats["assistant_messages"] += 1
                elif _schema_is_tool_call(schema, rec):
                    stats["tool_calls"] += 1
    except OSError:
        pass
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats

def universal_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Universal message extraction: 用 SchemaProbe 精准提取。"""
    path = Path(session_path)
    if not path.exists():
        return
    schema = _probe_schema(session_path)
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = _schema_get_timestamp(schema, rec)
                nt = _normalize_timestamp(ts) if ts else ""

                if role in ("user", "both") and _schema_is_user(schema, rec):
                    text = _schema_get_text(schema, rec)
                    # zcode trace: turn_started has payload.input
                    if not text and rec.get("type") == "turn_started":
                        payload = rec.get("payload", {})
                        if isinstance(payload, dict):
                            inp = payload.get("input", "")
                            if isinstance(inp, str) and inp.strip():
                                text = inp[:500]
                            elif isinstance(inp, list):
                                texts = [str(i.get("text", "")) for i in inp if isinstance(i, dict) and i.get("text")]
                                if texts:
                                    text = "\n".join(texts)[:500]
                    if text:
                        # Filter system-reminder noise (qoder and similar)
                        stripped = text.strip()
                        if stripped.startswith("<system-reminder>"):
                            # Strip the system-reminder block, keep real content after it
                            if "</system-reminder>" in stripped:
                                after = stripped.split("</system-reminder>", 1)[-1].strip()
                                if after:
                                    text = after
                                else:
                                    continue
                            else:
                                continue
                        if stripped.startswith("<runtime_context>"):
                            continue
                        yield {"role": "USER", "timestamp": nt, "text": text}
                        count += 1
                        continue
                if role in ("assistant", "both") and _schema_is_assistant(schema, rec):
                    text = _schema_get_text(schema, rec)
                    # zcode trace: model_complete has payload.content
                    if not text and rec.get("type") == "model_complete":
                        payload = rec.get("payload", {})
                        if isinstance(payload, dict):
                            content = payload.get("content", [])
                            if isinstance(content, list):
                                texts = []
                                for block in content:
                                    if isinstance(block, dict):
                                        for k in ("text", "message"):
                                            v = block.get(k, "")
                                            if isinstance(v, str) and v.strip():
                                                texts.append(v)
                                    elif isinstance(block, str) and block.strip():
                                        texts.append(block)
                                if texts:
                                    text = "\n".join(texts)[:500]
                            elif isinstance(content, str) and content.strip():
                                text = content[:500]
                    if text:
                        yield {"role": "ASSISTANT", "timestamp": nt, "text": text}
                        count += 1
                        continue
                if limit and count >= limit:
                    return
    except OSError:
        pass

def universal_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Universal tool extraction: 用 SchemaProbe 检测工具调用模式。"""
    path = Path(session_path)
    if not path.exists():
        return
    schema = _probe_schema(session_path)
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not _schema_is_tool_call(schema, rec):
                    continue
                ts = _schema_get_timestamp(schema, rec)
                nt = _normalize_timestamp(ts) if ts else ""
                name = rec.get("name", rec.get("tool_name", ""))
                if tool_filter and name and name != tool_filter:
                    continue
                if errors_only:
                    # Check for error indicators in the record
                    is_err = rec.get("isError", False) or rec.get("status") in ("error", "failed") or bool(rec.get("error"))
                    if not is_err:
                        continue
                    status = "error"
                else:
                    status = rec.get("status", "ok")
                if limit and count >= limit:
                    return
                args = rec.get("arguments", rec.get("input", rec.get("args", "")))
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                yield {"timestamp": nt, "name": name or f"[tool]", "status": status, "key_input": str(args)[:150] if args else "", "result_preview": ""}
                count += 1
    except OSError:
        pass

ENV_REGISTRY = {
    "claude": {"name": "Claude Code", "root": "~/.claude/projects/", "format": "jsonl", "adapter": "claude"},
    "grok": {"name": "Grok Build", "root": "~/.grok/sessions/", "format": "jsonl", "adapter": "grok"},
    "kimi_code": {"name": "Kimi Code", "root": "~/.kimi-code/sessions/", "format": "jsonl", "adapter": "kimi_code"},
    "codex": {"name": "Codex (OpenAI)", "root": "~/.codex/sessions/", "format": "jsonl", "adapter": "codex"},
    "workbuddy": {"name": "WorkBuddy", "root": "~/.workbuddy/projects/", "format": "jsonl", "adapter": "workbuddy"},
    "trae_cn": {"name": "Trae CN (ByteDance)", "root": "~/.trae-cn/memory/projects/", "format": "jsonl-summary", "adapter": "trae_cn"},
    "zcode": {"name": "ZCode (Z-AI)", "root": "~/.zcode/cli/agents/", "format": "jsonl-trace", "adapter": "zcode"},
    "dim": {"name": "DIM (Memory)", "root": "~/.dim/memory/", "format": "jsonl-summary", "adapter": "dim"},
    "dimcode": {"name": "DimCode (SQLite)", "root": "~/.dimcode/v2/dimcode.sqlite", "format": "sqlite", "adapter": "dimcode"},
    "reasonix": {"name": "Reasonix", "root": "~/.reasonix/sessions/", "format": "jsonl", "adapter": "reasonix"},
}

KNOWN_UNADAPTED = {
    "mimo": {"name": "MiMo", "root": "~/.mimo/projects/"},
    "qwen": {"name": "Qwen Code", "root": "~/.qwen/projects/"},
    "qoder": {"name": "Qoder", "root": "~/.qoder/cache/projects/"},
    "openclaw-autoclaw": {"name": "OpenClaw AutoClaw", "root": "~/.openclaw-autoclaw/agents/"},
    "gstack": {"name": "GStack", "root": "~/.gstack/sessions/"},
    "codebuddy": {"name": "CodeBuddy", "root": "~/.codebuddy/sessions/"},
    "cc-switch": {"name": "CC-Switch", "root": "~/.cc-switch/"},
}

def scan_all_environments_parallel():
    """Parallel scan of all known and unknown environments."""
    results = []

    def _scan_env(env_id, env_info, is_registered=True):
        root = Path(os.path.expanduser(env_info["root"]))
        exists = root.exists()
        session_count = 0
        if exists:
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
            except Exception:
                pass

    # Scan home directory for unknown environments
    home = Path.home()
    known_dirs = set()
    for e in list(ENV_REGISTRY.values()) + list(KNOWN_UNADAPTED.values()):
        known_dirs.add(os.path.expanduser(e["root"]).split("/")[0])
    known_dirs.update(str(home / d) for d in (".claude", ".zcode", ".agents", ".config", ".cache", ".npm", ".cargo", ".ssh", ".local"))

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
            except Exception:
                pass

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
    }

def _grok_resolve_path(path):
    """Resolve Grok session dir to chat_history.jsonl file path."""
    p = Path(path)
    if p.is_dir():
        chat = p / "chat_history.jsonl"
        if chat.exists():
            return str(chat)
    return str(p)

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

def _grok_extract_messages(path, role="both", limit=0, thinking_limit=0):
    """Dedicated message extraction for Grok sessions.

    Grok's chat_history.jsonl has:
    - type=user: content is a string or list of text blocks. Many are
      system-reminder/system context, not real user messages.
    - type=assistant: content is text, tool_calls may be present.
    - type=reasoning: summary field with thinking content.

    We filter user messages to exclude system-reminder, user_info, and
    system-reminder blocks, keeping only real user queries.
    Timestamps are read from summary.json (session-level, not per-message).
    """
    import re

    resolved = _grok_resolve_path(path)

    # Get session-level timestamp from summary.json
    session_ts = ""
    summary_file = Path(resolved).parent / "summary.json"
    if summary_file.exists():
        try:
            with open(summary_file, encoding="utf-8") as f:
                summary = json.load(f)
            created = summary.get("created_at", "")
            if created:
                session_ts = str(_normalize_timestamp(created)) if _normalize_timestamp(created) else ""
        except (json.JSONDecodeError, OSError):
            pass

    count = 0
    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                rtype = rec.get("type", "")
                ts = session_ts  # Grok has no per-message timestamp

                if rtype == "user" and role in ("user", "both"):
                    content = rec.get("content", "")
                    text = ""
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ).strip()

                    if not text:
                        continue

                    # Filter out system context messages
                    if text.startswith("<system-reminder>"):
                        continue
                    if text.startswith("<user_info>"):
                        continue
                    # Extract real user query from <user_query> tags
                    query_match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
                    if query_match:
                        text = query_match.group(1).strip()
                    # Skip if still too short or looks like system noise
                    if len(text) < 2:
                        continue

                    yield {"role": "USER", "timestamp": ts, "text": text[:500]}
                    count += 1
                    if limit and count >= limit:
                        return

                elif rtype == "assistant" and role in ("assistant", "both"):
                    content = rec.get("content", "")
                    text = ""
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ).strip()
                    if text:
                        yield {"role": "ASSISTANT", "timestamp": ts, "text": text[:500]}
                        count += 1
                        if limit and count >= limit:
                            return

                elif rtype == "reasoning" and role in ("assistant", "both") and thinking_limit != -1:
                    summary = rec.get("summary", "")
                    if isinstance(summary, str) and summary.strip():
                        text = summary.strip()
                        if thinking_limit > 0:
                            text = text[:thinking_limit]
                        yield {"role": "ASSISTANT", "timestamp": ts, "text": "[THINKING] " + text}
                        count += 1
                        if limit and count >= limit:
                            return
    except OSError:
        pass

def _grok_session_stats(path):
    """Dedicated stats for Grok sessions.

    Grok's chat_history.jsonl has no timestamps — we read summary.json for
    created_at/updated_at. Tool calls are embedded in assistant messages'
    tool_calls array, not as separate records.
    """
    resolved = _grok_resolve_path(path)
    stats = _empty_stats("unknown")
    stats["slug"] = Path(resolved).stem

    # Timestamps, model, and summary from summary.json
    session_dir = Path(resolved).parent
    summary_file = session_dir / "summary.json"
    if summary_file.exists():
        try:
            with open(summary_file, encoding="utf-8") as f:
                summary = json.load(f)
            info = summary.get("info", {})
            created = summary.get("created_at", "")
            updated = summary.get("updated_at", "")
            if created:
                stats["started"] = _normalize_timestamp(created)
            if updated:
                stats["ended"] = _normalize_timestamp(updated)
            stats["summary"] = (summary.get("session_summary") or "")[:100]
            model = summary.get("current_model_id", "")
            if model:
                stats["model"] = model
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: use file mtime if no timestamps from summary
    if not stats["started"] or not stats["ended"]:
        try:
            import os
            mtime = os.path.getmtime(resolved)
            nt = _normalize_timestamp(mtime)
            if nt:
                if not stats["started"]:
                    stats["started"] = nt
                if not stats["ended"]:
                    stats["ended"] = nt
        except OSError:
            pass

    # Count errors from events.jsonl (authoritative source)
    events_file = session_dir / "events.jsonl"
    if events_file.exists():
        try:
            with open(events_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if event.get("type") == "tool_completed" and event.get("outcome") == "error":
                        stats["errors"] += 1
        except OSError:
            pass

    # Count from chat_history.jsonl
    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                rtype = rec.get("type", "")
                if rtype == "user":
                    stats["user_messages"] += 1
                elif rtype == "assistant":
                    stats["assistant_messages"] += 1
                    # Tool calls are embedded in assistant messages
                    tool_calls = rec.get("tool_calls", [])
                    if isinstance(tool_calls, list):
                        stats["tool_calls"] += len(tool_calls)
                    # Model from assistant message
                    if not stats["model"]:
                        model_id = rec.get("model_id", "")
                        if model_id:
                            stats["model"] = model_id
                elif rtype == "tool_result":
                    # Detect errors in tool results
                    content = rec.get("content", "")
                    if isinstance(content, str):
                        if "Exit Code:" in content and "Exit Code: 0" not in content:
                            stats["errors"] += 1
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                text = block.get("text", "")
                                if isinstance(text, str) and "Exit Code:" in text and "Exit Code: 0" not in text:
                                    stats["errors"] += 1
                                    break
    except OSError:
        pass
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats

def kimi_code_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """
    Extract messages from Kimi Code wire.jsonl.

    Kimi Code format:
      - turn.prompt: user input (input[].text)
      - context.append_message: echoed messages (role=user|assistant, content[].text)
      - context.append_loop_event: assistant content (event.type=content.part, event.part.type=text|think)
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
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                rtype = rec.get("type", "")
                ts = rec.get("time", rec.get("timestamp", ""))

                # User input: turn.prompt
                if rtype == "turn.prompt" and role in ("user", "both"):
                    inputs = rec.get("input", [])
                    parts = []
                    for inp in (inputs if isinstance(inputs, list) else []):
                        if isinstance(inp, dict) and inp.get("type") == "text":
                            t = inp.get("text", "").strip()
                            if t:
                                parts.append(t)
                    if parts:
                        yield {"role": "USER", "timestamp": _normalize_timestamp(ts) if ts else "", "text": "\n".join(parts)}
                        count += 1
                        if limit and count >= limit:
                            return

                # context.append_message is an echo of turn.prompt — skip to
                # avoid duplicate user messages. turn.prompt is the authoritative
                # source for user input.

                # Assistant content: context.append_loop_event with event.type=content.part
                elif rtype == "context.append_loop_event" and role in ("assistant", "both"):
                    event = rec.get("event", {})
                    if isinstance(event, dict) and event.get("type") == "content.part":
                        part = event.get("part", {})
                        if isinstance(part, dict):
                            pt = part.get("type", "")
                            text = part.get("text", "").strip()
                            if pt == "text" and text:
                                yield {"role": "ASSISTANT", "timestamp": _normalize_timestamp(ts) if ts else "", "text": text[:500]}
                                count += 1
                                if limit and count >= limit:
                                    return
                            elif pt == "think" and text and thinking_limit != -1:
                                if thinking_limit > 0:
                                    text = text[:thinking_limit]
                                yield {"role": "ASSISTANT", "timestamp": _normalize_timestamp(ts) if ts else "", "text": "[THINKING] " + text}
                                count += 1
                                if limit and count >= limit:
                                    return
    except OSError:
        pass

def kimi_code_session_stats(session_path):
    """Stats for Kimi Code session using dedicated extractor."""
    resolved = _kimi_code_resolve_path(session_path)
    stats = _empty_stats("kimi")
    stats["slug"] = Path(resolved).stem

    # Get title/model from state.json if available
    p = Path(session_path)
    if p.is_dir():
        state_file = p / "state.json"
        if state_file.exists():
            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)
                stats["summary"] = (state.get("title") or "")[:100]
                if state.get("model"):
                    stats["model"] = state["model"]
            except (json.JSONDecodeError, OSError):
                pass
    elif p.is_file() and p.parent.parent.parent.name:
        # If given a wire.jsonl path, look for state.json in session dir
        state_file = p.parent.parent.parent / "state.json"
        if state_file.exists():
            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)
                stats["summary"] = (state.get("title") or "")[:100]
                if state.get("model"):
                    stats["model"] = state["model"]
            except (json.JSONDecodeError, OSError):
                pass

    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
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
                elif rtype == "context.append_loop_event":
                    event = rec.get("event", {})
                    if not isinstance(event, dict):
                        continue
                    etype = event.get("type", "")
                    if etype == "content.part":
                        stats["assistant_messages"] += 1
                    elif etype == "tool.call":
                        stats["tool_calls"] += 1
                    elif etype == "tool.result":
                        # Detect errors: isError flag or error in result
                        result = event.get("result", {})
                        if isinstance(result, dict):
                            if result.get("isError"):
                                stats["errors"] += 1
                            elif isinstance(result.get("output", ""), str) and \
                                    "Exit Code:" in result.get("output", "") and \
                                    "Exit Code: 0" not in result.get("output", ""):
                                stats["errors"] += 1
                elif rtype == "usage.record":
                    usage = rec.get("usage", {})
                    if isinstance(usage, dict):
                        stats["input_tokens"] += int(usage.get("inputOther", usage.get("inputTokens", 0)))
                        stats["output_tokens"] += int(usage.get("output", usage.get("outputTokens", 0)))
                        stats["cache_read_tokens"] += int(usage.get("inputCacheRead", usage.get("cacheReadTokens", 0)))
                        stats["cache_create_tokens"] += int(usage.get("inputCacheCreation", usage.get("cacheCreationTokens", 0)))
                elif rtype == "full_compaction.begin":
                    stats["compactions"] += 1
    except OSError:
        pass
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
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
    stats["slug"] = p.stem
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
        if rtype == "event_msg" and ptype == "user_message":
            stats["user_messages"] += 1
        elif rtype == "event_msg" and ptype == "agent_message":
            stats["assistant_messages"] += 1
        elif rtype == "response_item" and ptype == "message":
            stats["assistant_messages"] += 1
        elif rtype == "response_item" and ptype in ("function_call", "custom_tool_call", "tool_search_call"):
            stats["tool_calls"] += 1
        elif rtype == "response_item" and ptype in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
            output = payload.get("output", "")
            if isinstance(output, str) and ("Exit Code:" in output and "Exit Code: 0" not in output):
                stats["errors"] += 1
    return stats

# ── ZCode / DIM / DimCode adapters (delegates to _adapters_zcode.py)
from echolib._adapters_zcode import (
    zcode_list_sessions, zcode_session_stats, zcode_extract_messages,
    zcode_extract_tools, zcode_session_path,
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

register_adapter("codex", "Codex (OpenAI)",
    list_sessions=codex_list_sessions,
    session_stats=codex_session_stats_dedicated,
    extract_messages=codex_extract_messages,
    extract_tools=codex_extract_tools,  # 保留：exit code 错误检测
    session_path=codex_session_path,
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
