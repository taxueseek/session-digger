"""Qwen Code (Alibaba / 通义灵码) adapter — transcripts, token accounting, cache hits.

Storage layout:
    ~/.qwen/projects/<project-hash>/chats/<uuid>.jsonl

Qwen transcript format:
- JSONL records with uuid, sessionId, timestamp, type, cwd, version.
- type=user: contains message: {role: "user", parts: [{text: "..."}]}
- type=assistant: contains model, message: {role: "model", parts: [...]},
  usageMetadata: {promptTokenCount, candidatesTokenCount, cachedContentTokenCount, totalTokenCount}
- type=tool_result: contains toolCallResult.
- function calls live in assistant message.parts: [{functionCall: {id, name, args}}]

Token Policy:
- input_includes_cache = True (promptTokenCount includes cachedContentTokenCount).
- cache_is_subset = True.
- cache_hit_rate = cachedContentTokenCount / promptTokenCount (when promptTokenCount > 0).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from echolib._helpers import (
    _iter_jsonl,
    attach_cache_hit_rates,
    cap,
    discovery_only_active,
)
from echolib._models import normalize_model_name

QWEN_DIR = Path.home() / ".qwen"


def _qwen_projects_dir(root: str | Path | None = None) -> Path:
    if root:
        p = Path(root).expanduser()
        return p if p.is_dir() else Path(root)
    return QWEN_DIR / "projects"


def _iter_qwen_chat_files(root: str | Path | None = None) -> Generator[Path, None, None]:
    p_dir = _qwen_projects_dir(root)
    if not p_dir.is_dir():
        return
    try:
        for p in p_dir.iterdir():
            if not p.is_dir():
                continue
            chats = p / "chats"
            if chats.is_dir():
                for f in chats.iterdir():
                    if f.name.endswith(".jsonl") and not f.name.startswith("."):
                        yield f
    except OSError:
        return


def qwen_list_sessions(cwd=None, limit=50, keyword="", root=None) -> list[dict]:
    """List Qwen Code chat sessions."""
    sessions = []
    cwd_n = os.path.normpath(cwd) if cwd else ""
    keyword_l = keyword.lower() if keyword else ""
    disc_only = discovery_only_active()

    for path in _iter_qwen_chat_files(root):
        sid = path.stem
        try:
            st = path.stat()
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
                "full_path": str(path),
                "agent": "qwen",
            })
            continue

        # Peek first/last records for session metadata
        first_prompt = ""
        first_ts = ""
        last_ts = ""
        project_path = ""
        msg_count = 0

        for rec in _iter_jsonl(path):
            t = rec.get("type")
            ts = rec.get("timestamp") or ""
            if not first_ts and ts:
                first_ts = ts
            if ts:
                last_ts = ts

            if not project_path and rec.get("cwd"):
                project_path = rec.get("cwd")

            if t in ("user", "assistant"):
                msg_count += 1
                if t == "user" and not first_prompt:
                    msg = rec.get("message") or {}
                    parts = msg.get("parts") or []
                    for pt in parts:
                        if isinstance(pt, dict) and pt.get("text"):
                            first_prompt = pt["text"].strip()
                            break

        if cwd_n and project_path:
            p_norm = os.path.normpath(project_path)
            if p_norm != cwd_n and not p_norm.startswith(cwd_n + os.sep):
                continue

        if keyword_l:
            match = (keyword_l in sid.lower() or
                     keyword_l in first_prompt.lower() or
                     (project_path and keyword_l in project_path.lower()))
            if not match:
                continue

        sessions.append({
            "session_id": sid,
            "project_path": project_path,
            "created": first_ts,
            "modified": last_ts or first_ts,
            "message_count": msg_count,
            "mtime": mtime,
            "full_path": str(path),
            "agent": "qwen",
            "first_prompt": cap(first_prompt, 120),
        })

    sessions.sort(key=lambda s: s.get("mtime", 0), reverse=True)
    return cap(sessions, limit)


def qwen_session_path(session_id: str, cwd=None, root=None) -> str | None:
    """Find session file path by session_id."""
    target = f"{session_id}.jsonl"
    for path in _iter_qwen_chat_files(root):
        if path.name == target or path.stem == session_id:
            return str(path)
    return None


def qwen_extract_messages(session_path: str, limit=None) -> list[dict]:
    """Extract user and assistant messages from Qwen Code transcript."""
    messages = []
    p = Path(session_path)
    if not p.is_file():
        return []

    for rec in _iter_jsonl(p):
        t = rec.get("type")
        if t not in ("user", "assistant"):
            continue

        role = "user" if t == "user" else "assistant"
        msg = rec.get("message") or {}
        parts = msg.get("parts") or []
        texts = []
        for pt in parts:
            if isinstance(pt, dict):
                # Ignore pure thought unless that's all there is
                txt = pt.get("text", "")
                if txt and not pt.get("thought"):
                    texts.append(txt)
                elif txt and not texts:
                    texts.append(txt)

        full_text = "\n".join(texts).strip()
        if not full_text and not parts:
            continue

        messages.append({
            "role": role,
            "text": full_text,
            "timestamp": rec.get("timestamp") or "",
            "uuid": rec.get("uuid") or "",
        })

    return cap(messages, limit)


def qwen_extract_tools(session_path: str, limit=None) -> list[dict]:
    """Extract tool calls and results from Qwen Code transcript."""
    tools = []
    p = Path(session_path)
    if not p.is_file():
        return []

    pending_calls = {}

    for rec in _iter_jsonl(p):
        t = rec.get("type")
        ts = rec.get("timestamp") or ""

        if t == "assistant":
            msg = rec.get("message") or {}
            parts = msg.get("parts") or []
            for pt in parts:
                if isinstance(pt, dict) and "functionCall" in pt:
                    fc = pt["functionCall"]
                    call_id = fc.get("id") or f"call_{len(tools)}"
                    entry = {
                        "name": fc.get("name") or "unknown",
                        "input": fc.get("args") or {},
                        "id": call_id,
                        "timestamp": ts,
                        "status": "success",
                    }
                    tools.append(entry)
                    pending_calls[call_id] = entry

        elif t == "tool_result":
            # Match tool result if possible
            t_res = rec.get("toolCallResult") or {}
            c_id = rec.get("parentUuid") or t_res.get("id")
            if c_id and c_id in pending_calls:
                target = pending_calls[c_id]
                # Check for errors in result
                res_str = str(t_res)
                if "error" in res_str.lower() or t_res.get("error"):
                    target["status"] = "error"

    return cap(tools, limit)


def qwen_session_stats(session_path: str) -> dict:
    """Compute rich session statistics for Qwen Code."""
    p = Path(session_path)
    if not p.is_file():
        return {}

    msg_count = 0
    user_count = 0
    asst_count = 0
    tool_calls = 0
    errors = 0
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    first_ts = ""
    last_ts = ""
    first_prompt = ""
    cwd = ""
    model_votes: dict[str, int] = {}
    tool_usage: dict[str, int] = {}
    tool_errors: dict[str, int] = {}

    for rec in _iter_jsonl(p):
        t = rec.get("type")
        ts = rec.get("timestamp") or ""
        if not first_ts and ts:
            first_ts = ts
        if ts:
            last_ts = ts

        if not cwd and rec.get("cwd"):
            cwd = rec.get("cwd")

        if t == "user":
            user_count += 1
            msg_count += 1
            if not first_prompt:
                msg = rec.get("message") or {}
                for pt in msg.get("parts") or []:
                    if isinstance(pt, dict) and pt.get("text"):
                        first_prompt = pt["text"].strip()
                        break

        elif t == "assistant":
            asst_count += 1
            msg_count += 1
            m = rec.get("model")
            if m:
                clean_m = normalize_model_name(str(m))
                model_votes[clean_m] = model_votes.get(clean_m, 0) + 1

            # Extract token usage
            usage = rec.get("usageMetadata") or {}
            if usage:
                inp = int(usage.get("promptTokenCount") or 0)
                out = int(usage.get("candidatesTokenCount") or 0)
                cache = int(usage.get("cachedContentTokenCount") or 0)
                input_tokens += inp
                output_tokens += out
                cache_read += cache

            # Extract tool calls
            msg = rec.get("message") or {}
            for pt in msg.get("parts") or []:
                if isinstance(pt, dict) and "functionCall" in pt:
                    tool_calls += 1
                    fn_name = pt["functionCall"].get("name") or "unknown"
                    tool_usage[fn_name] = tool_usage.get(fn_name, 0) + 1

        elif t == "tool_result":
            res = str(rec.get("toolCallResult") or "")
            if "error" in res.lower():
                errors += 1

    primary_model = max(model_votes.items(), key=lambda kv: kv[1])[0] if model_votes else ""

    # Calculate duration
    duration = 0.0
    if first_ts and last_ts:
        try:
            t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00")).timestamp()
            t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00")).timestamp()
            duration = max(0.0, t1 - t0)
        except Exception:
            pass

    stats = {
        "session_id": p.stem,
        "project_path": cwd,
        "created": first_ts,
        "modified": last_ts or first_ts,
        "message_count": msg_count,
        "user_messages": user_count,
        "assistant_messages": asst_count,
        "tool_calls": tool_calls,
        "errors": errors,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
        "model": primary_model,
        "duration_seconds": duration,
        "first_prompt": cap(first_prompt, 200),
        "tool_usage_json": json.dumps(tool_usage),
        "tool_errors_json": json.dumps(tool_errors),
        "agent": "qwen",
    }
    # Attach cache hit rate (input_includes_cache=True for Qwen)
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return stats
