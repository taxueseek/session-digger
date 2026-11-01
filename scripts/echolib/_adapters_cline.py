"""Cline / Roo Code adapter — tasks, UI messages, token accounting.

Storage layout:
    ~/.cline/tasks/<task-id>/ui_messages.json
    ~/.cline/tasks/<task-id>/api_conversation_history.json
Also supports VS Code / Cursor globalStorage paths:
    ~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/
    ~/Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline/tasks/

Cline task format:
- ui_messages.json: JSON list of objects:
  {ts, type, say, text, ask, partial, ...}
  - say="task" or say="user_feedback": user message
  - say="text": assistant text response
  - say="tool" or ask="tool": tool execution
  - say="api_req_started": API request metadata (tokensIn, tokensOut, cacheReads, cacheWrites, request)
- api_conversation_history.json: standard message array [{role, content: [...]}]

Token Policy:
- input_includes_cache = False (Anthropic / Claude standard tokensIn does not include cacheReads).
- cache_read_tokens = cacheReads.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from echolib._helpers import (
    attach_cache_hit_rates,
    cap,
    discovery_only_active,
)
from echolib._models import normalize_model_name

CLINE_DIR = Path.home() / ".cline"


def _cline_task_dirs(root: str | Path | None = None) -> list[Path]:
    """Candidate directories containing Cline tasks."""
    if root:
        p = Path(root).expanduser()
        return [p] if p.is_dir() else []
    dirs = [
        CLINE_DIR / "tasks",
        Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks",
        Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "tasks",
        Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks",
    ]
    return [d for d in dirs if d.is_dir()]


def _iter_cline_tasks(root: str | Path | None = None) -> Generator[Path, None, None]:
    for parent in _cline_task_dirs(root):
        try:
            for task_dir in parent.iterdir():
                if task_dir.is_dir() and not task_dir.name.startswith("."):
                    if (task_dir / "ui_messages.json").is_file() or (task_dir / "api_conversation_history.json").is_file():
                        yield task_dir
        except OSError:
            continue


def _format_cline_ts(ts_val) -> str:
    """Format millisecond integer or float timestamp to ISO 8601 string."""
    if not ts_val:
        return ""
    try:
        if isinstance(ts_val, (int, float)):
            # Usually epoch millis
            sec = ts_val / 1000.0 if ts_val > 1e11 else float(ts_val)
            return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat()
        return str(ts_val)
    except Exception:
        return ""


def cline_list_sessions(cwd=None, limit=50, keyword="", root=None) -> list[dict]:
    """List Cline / Roo Code task sessions."""
    sessions = []
    cwd_n = os.path.normpath(cwd) if cwd else ""
    keyword_l = keyword.lower() if keyword else ""
    disc_only = discovery_only_active()

    for task_dir in _iter_cline_tasks(root):
        sid = task_dir.name
        ui_file = task_dir / "ui_messages.json"
        api_file = task_dir / "api_conversation_history.json"
        target_file = ui_file if ui_file.is_file() else api_file

        try:
            st = target_file.stat()
            mtime = st.st_mtime
        except OSError:
            continue

        if disc_only:
            sessions.append({
                "session_id": sid,
                "project_path": "",
                "created": "",
                "modified": "",
                "message_count": 0,
                "mtime": mtime,
                "full_path": str(target_file),
                "agent": "cline",
            })
            continue

        first_prompt = ""
        first_ts = ""
        last_ts = ""
        msg_count = 0

        if ui_file.is_file():
            try:
                with open(ui_file, encoding="utf-8", errors="replace") as f:
                    items = json.load(f)
                    if isinstance(items, list):
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            ts = _format_cline_ts(item.get("ts"))
                            if not first_ts and ts:
                                first_ts = ts
                            if ts:
                                last_ts = ts
                            say = item.get("say")
                            ask = item.get("ask")
                            text = item.get("text") or ""
                            if say in ("task", "user_feedback") or ask in ("followup", "command"):
                                msg_count += 1
                                if not first_prompt and text:
                                    first_prompt = text.strip()
                            elif say == "text":
                                msg_count += 1
            except Exception:
                pass

        if keyword_l:
            match = (keyword_l in sid.lower() or keyword_l in first_prompt.lower())
            if not match:
                continue

        sessions.append({
            "session_id": sid,
            "project_path": "",
            "created": first_ts,
            "modified": last_ts or first_ts,
            "message_count": msg_count,
            "mtime": mtime,
            "full_path": str(target_file),
            "agent": "cline",
            "first_prompt": cap(first_prompt, 120),
        })

    sessions.sort(key=lambda s: s.get("mtime", 0), reverse=True)
    return cap(sessions, limit)


def cline_session_path(session_id: str, cwd=None, root=None) -> str | None:
    """Find Cline session path by task directory name."""
    for task_dir in _iter_cline_tasks(root):
        if task_dir.name == session_id:
            ui = task_dir / "ui_messages.json"
            if ui.is_file():
                return str(ui)
            api = task_dir / "api_conversation_history.json"
            if api.is_file():
                return str(api)
            return str(task_dir)
    return None


def cline_extract_messages(session_path: str, limit=None) -> list[dict]:
    """Extract messages from Cline task ui_messages.json or api_conversation_history.json."""
    messages = []
    p = Path(session_path)
    if p.is_dir():
        ui = p / "ui_messages.json"
        api = p / "api_conversation_history.json"
        p = ui if ui.is_file() else api

    if not p.is_file():
        return []

    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return []

    if isinstance(data, list):
        if p.name == "ui_messages.json":
            for item in data:
                if not isinstance(item, dict):
                    continue
                say = item.get("say")
                ask = item.get("ask")
                text = item.get("text") or ""
                ts = _format_cline_ts(item.get("ts"))

                if say in ("task", "user_feedback") or ask in ("followup", "command"):
                    if text:
                        messages.append({"role": "user", "text": text, "timestamp": ts})
                elif say == "text":
                    if text:
                        messages.append({"role": "assistant", "text": text, "timestamp": ts})
        elif p.name == "api_conversation_history.json":
            for item in data:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = item.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    texts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("text"):
                            texts.append(c["text"])
                    text = "\n".join(texts)
                if text:
                    messages.append({"role": role, "text": text, "timestamp": ""})

    return cap(messages, limit)


def cline_extract_tools(session_path: str, limit=None) -> list[dict]:
    """Extract tool calls from Cline task."""
    tools = []
    p = Path(session_path)
    if p.is_dir():
        p = p / "ui_messages.json"
    elif p.name != "ui_messages.json" and (p.parent / "ui_messages.json").is_file():
        p = p.parent / "ui_messages.json"

    if not p.is_file():
        return []

    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return []

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            say = item.get("say")
            ask = item.get("ask")
            ts = _format_cline_ts(item.get("ts"))

            if say == "tool" or ask == "tool":
                text = item.get("text") or ""
                tool_data = {}
                if text and text.startswith("{") and text.endswith("}"):
                    try:
                        tool_data = json.loads(text)
                    except Exception:
                        pass
                tool_name = tool_data.get("tool") or item.get("tool") or "unknown"
                tools.append({
                    "name": tool_name,
                    "input": tool_data,
                    "timestamp": ts,
                    "status": "success",
                })

    return cap(tools, limit)


def cline_session_stats(session_path: str) -> dict:
    """Compute rich session statistics for Cline / Roo Code task."""
    p = Path(session_path)
    if p.is_dir():
        ui = p / "ui_messages.json"
        api = p / "api_conversation_history.json"
    else:
        ui = p if p.name == "ui_messages.json" else p.parent / "ui_messages.json"
        api = p if p.name == "api_conversation_history.json" else p.parent / "api_conversation_history.json"

    user_count = 0
    asst_count = 0
    tool_calls = 0
    errors = 0
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    first_ts = ""
    last_ts = ""
    first_prompt = ""
    model_votes: dict[str, int] = {}
    tool_usage: dict[str, int] = {}

    if ui.is_file():
        try:
            with open(ui, encoding="utf-8", errors="replace") as f:
                items = json.load(f)
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        ts = _format_cline_ts(item.get("ts"))
                        if not first_ts and ts:
                            first_ts = ts
                        if ts:
                            last_ts = ts

                        say = item.get("say")
                        ask = item.get("ask")
                        text = item.get("text") or ""

                        if say in ("task", "user_feedback") or ask in ("followup", "command"):
                            user_count += 1
                            if not first_prompt and text:
                                first_prompt = text.strip()
                        elif say == "text":
                            asst_count += 1
                        elif say == "tool" or ask == "tool":
                            tool_calls += 1
                            t_name = item.get("tool") or "tool"
                            tool_usage[t_name] = tool_usage.get(t_name, 0) + 1
                        elif say == "error":
                            errors += 1
                        elif say == "api_req_started":
                            # Parse token counts from text if JSON
                            if text and text.startswith("{"):
                                try:
                                    req_meta = json.loads(text)
                                    req_obj = req_meta.get("request") or {}
                                    m = req_obj.get("model") or req_meta.get("model")
                                    if m:
                                        clean_m = normalize_model_name(str(m))
                                        model_votes[clean_m] = model_votes.get(clean_m, 0) + 1
                                    inp = int(req_meta.get("tokensIn") or 0)
                                    out = int(req_meta.get("tokensOut") or 0)
                                    cr = int(req_meta.get("cacheReads") or 0)
                                    cw = int(req_meta.get("cacheWrites") or 0)
                                    input_tokens += inp
                                    output_tokens += out
                                    cache_read += cr
                                    cache_write += cw
                                except Exception:
                                    pass
        except Exception:
            pass

    # If messages weren't counted from ui_messages, try api_conversation_history
    if user_count == 0 and asst_count == 0 and api.is_file():
        try:
            with open(api, encoding="utf-8", errors="replace") as f:
                records = json.load(f)
                if isinstance(records, list):
                    for rec in records:
                        if isinstance(rec, dict):
                            r = rec.get("role")
                            if r == "user":
                                user_count += 1
                            elif r == "assistant":
                                asst_count += 1
        except Exception:
            pass

    primary_model = max(model_votes.items(), key=lambda kv: kv[1])[0] if model_votes else ""

    duration = 0.0
    if first_ts and last_ts:
        try:
            t0 = datetime.fromisoformat(first_ts).timestamp()
            t1 = datetime.fromisoformat(last_ts).timestamp()
            duration = max(0.0, t1 - t0)
        except Exception:
            pass

    task_id = ui.parent.name if ui.is_file() else (api.parent.name if api.is_file() else "unknown")

    stats = {
        "session_id": task_id,
        "project_path": "",
        "created": first_ts,
        "modified": last_ts or first_ts,
        "message_count": user_count + asst_count,
        "user_messages": user_count,
        "assistant_messages": asst_count,
        "tool_calls": tool_calls,
        "errors": errors,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_write,
        "total_tokens": input_tokens + output_tokens,
        "model": primary_model,
        "duration_seconds": duration,
        "first_prompt": cap(first_prompt, 200),
        "tool_usage_json": json.dumps(tool_usage),
        "tool_errors_json": "{}",
        "agent": "cline",
    }
    attach_cache_hit_rates(stats, input_includes_cache=False)
    return stats
