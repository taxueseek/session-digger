"""WorkBuddy adapter — list/stats/messages/tools at Claude/Codex/Cursor quality.

WorkBuddy session layout: ``~/.workbuddy/projects/<slug>/<session_id>.jsonl``

Buddy-format JSONL record types:
  message / function_call / function_call_result / ai-title /
  file-history-snapshot / summary / reasoning

Tokens + model live in ``providerData.requestModelName`` and
``providerData.usage`` (camelCase: inputTokens / outputTokens).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from echolib._claude import _normalize_timestamp
from echolib._helpers import (
    WORKBUDDY_DIR,
    _extract_content_text,
    _iter_jsonl,
    _match_call_results,
    _strip_system_reminder,
)
from echolib._models import SessionMeta

_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>", flags=re.DOTALL | re.IGNORECASE
)
_EXIT_CODE_RE = re.compile(r"Exit Code:\s*(\d+)", re.IGNORECASE)
# Placeholder model from _empty_stats("workbuddy") — must be overwriteable
_PLACEHOLDER_MODELS = frozenset({"", "workbuddy", "unknown"})


def _workbuddy_user_text(text: str) -> str | None:
    """Strip WorkBuddy wrappers; prefer <user_query> body (Cursor parity)."""
    if not text:
        return None
    matches = _USER_QUERY_RE.findall(text)
    if matches:
        joined = "\n".join(m.strip() for m in matches if m.strip())
        if joined:
            return joined
    cleaned = _strip_system_reminder(text)
    if cleaned is None:
        return None
    stripped = cleaned.strip()
    if stripped.startswith(("<system-reminder", "<user_info", "<environment")):
        return None
    return stripped or None


def _workbuddy_is_error_output(output) -> tuple[bool, str]:
    """Return (is_error, preview) from function_call_result.output."""
    preview = ""
    is_error = False
    texts: list[str] = []

    if isinstance(output, dict):
        if output.get("type") in ("text", "input_text", "output_text") and output.get("text"):
            texts.append(str(output["text"]))
        else:
            # nested / free-form
            for key in ("text", "content", "message", "stderr", "stdout"):
                if isinstance(output.get(key), str) and output[key].strip():
                    texts.append(output[key])
                    break
            if not texts:
                try:
                    texts.append(json.dumps(output, ensure_ascii=False))
                except (TypeError, ValueError):
                    texts.append(str(output))
    elif isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content") or ""
                if t:
                    texts.append(str(t))
            elif isinstance(item, str) and item.strip():
                texts.append(item)
    elif isinstance(output, str):
        texts.append(output)

    preview = " ".join(texts).replace("\n", " ")[:150]
    blob = "\n".join(texts)
    # Prefer explicit exit code (regex was previously double-escaped — never matched)
    m = _EXIT_CODE_RE.search(blob)
    if m and int(m.group(1)) != 0:
        is_error = True
    elif re.search(r"\b(FAILED|ERROR|Traceback)\b", blob) and "Exit Code: 0" not in blob:
        # soft signal only when no successful exit code present
        if "Error:" in blob or "Traceback" in blob:
            is_error = True
    return is_error, preview


def _workbuddy_usage_add(stats, usage: dict) -> None:
    if not isinstance(usage, dict):
        return
    stats["input_tokens"] += int(
        usage.get("inputTokens")
        or usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or 0
    )
    stats["output_tokens"] += int(
        usage.get("outputTokens")
        or usage.get("output_tokens")
        or usage.get("completion_tokens")
        or 0
    )
    # cached tokens may live in details list or flat fields
    details = usage.get("inputTokensDetails") or usage.get("input_tokens_details")
    if isinstance(details, list):
        for d in details:
            if isinstance(d, dict):
                stats["cache_read_tokens"] += int(d.get("cached_tokens") or d.get("cache_read") or 0)
    stats["cache_read_tokens"] += int(
        usage.get("cache_read_input_tokens")
        or usage.get("cacheReadTokens")
        or 0
    )


def _workbuddy_set_model(stats, model: str) -> None:
    if not model:
        return
    if stats.get("model") in _PLACEHOLDER_MODELS:
        stats["model"] = model


def workbuddy_list_sessions(cwd=None, limit=50, keyword=""):
    """List WorkBuddy sessions from ~/.workbuddy/projects/."""
    projects_dir = WORKBUDDY_DIR / "projects"
    if not projects_dir.exists():
        return []
    sessions = []
    keyword_l = keyword.lower() if keyword else ""
    cwd_n = os.path.normpath(cwd) if cwd else ""

    try:
        project_dirs = [d for d in projects_dir.iterdir() if d.is_dir()]
    except OSError:
        return []

    for project_dir in project_dirs:
        project_slug = project_dir.name
        try:
            jsonl_files = list(project_dir.glob("*.jsonl"))
        except OSError:
            continue
        for jsonl_file in jsonl_files:
            sid = jsonl_file.stem
            if sid.startswith("agent-"):
                continue
            try:
                msg_count, first_prompt, ai_title, rec_cwd = _workbuddy_quick_scan(jsonl_file)
                mtime = _normalize_timestamp(jsonl_file.stat().st_mtime)
            except OSError:
                continue

            if cwd_n and rec_cwd and os.path.normpath(rec_cwd) != cwd_n:
                continue

            summary = (ai_title or first_prompt or sid)[:100]
            if keyword_l and keyword_l not in summary.lower() and keyword_l not in first_prompt.lower():
                continue

            sessions.append(SessionMeta(
                session_id=sid,
                full_path=str(jsonl_file),
                created=mtime,
                modified=mtime,
                message_count=msg_count,
                git_branch="",
                summary=summary,
                first_prompt=first_prompt[:200] if first_prompt else "",
                project_path=project_slug,
            ))

    sessions.sort(key=lambda s: str(s.modified or s.created or ""), reverse=True)
    return sessions[:limit]


def _workbuddy_quick_scan(jsonl_path):
    """Quick scan: count user msgs, first prompt, ai-title, cwd."""
    user_count = 0
    first_prompt = ""
    ai_title = ""
    rec_cwd = ""
    for rec in _iter_jsonl(jsonl_path):
        if not rec_cwd and isinstance(rec.get("cwd"), str):
            rec_cwd = rec["cwd"]
        rtype = rec.get("type", "")
        if rtype == "message" and rec.get("role") == "user":
            user_count += 1
            if first_prompt:
                continue
            content = rec.get("content", [])
            if isinstance(content, str):
                cleaned = _workbuddy_user_text(content)
                if cleaned:
                    first_prompt = cleaned[:200]
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") in ("input_text", "text", None) and c.get("text"):
                        cleaned = _workbuddy_user_text(str(c["text"]))
                        if cleaned:
                            first_prompt = cleaned[:200]
                            break
        elif rtype == "ai-title":
            ai_title = rec.get("aiTitle") or rec.get("title") or ""
    return user_count, first_prompt, ai_title, rec_cwd


def workbuddy_session_stats(session_dir):
    """Stats for a WorkBuddy session (file path)."""
    from echolib._adapters import _empty_stats
    path = Path(session_dir)
    if not path.exists():
        return _empty_stats("workbuddy")
    stats = _empty_stats("workbuddy")
    stats["slug"] = path.stem
    ai_title = ""

    for rec in _iter_jsonl(path):
        rtype = rec.get("type", "")
        ts_ms = rec.get("timestamp", 0)
        ts = _normalize_timestamp(ts_ms) if ts_ms else ""
        if ts:
            if not stats["started"] or ts < stats["started"]:
                stats["started"] = ts
            if ts > stats["ended"]:
                stats["ended"] = ts

        if rtype == "message":
            role = rec.get("role", "")
            if role == "user":
                stats["user_messages"] += 1
            elif role == "assistant":
                stats["assistant_messages"] += 1
            pd = rec.get("providerData") if isinstance(rec.get("providerData"), dict) else {}
            model = pd.get("requestModelName") or pd.get("model") or pd.get("requestModelId") or ""
            _workbuddy_set_model(stats, str(model) if model else "")
            # usage may be on message subdict or providerData
            usage = {}
            msg = rec.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                usage = msg["usage"]
            elif isinstance(pd.get("usage"), dict):
                usage = pd["usage"]
            elif isinstance(rec.get("usage"), dict):
                usage = rec["usage"]
            _workbuddy_usage_add(stats, usage)

        elif rtype == "function_call":
            stats["tool_calls"] += 1
            pd = rec.get("providerData") if isinstance(rec.get("providerData"), dict) else {}
            model = pd.get("requestModelName") or pd.get("model") or ""
            _workbuddy_set_model(stats, str(model) if model else "")
            if isinstance(pd.get("usage"), dict):
                _workbuddy_usage_add(stats, pd["usage"])

        elif rtype == "function_call_result":
            status = str(rec.get("status") or "").lower()
            is_error, _ = _workbuddy_is_error_output(rec.get("output"))
            if status in ("failed", "error", "cancelled") or is_error:
                stats["errors"] += 1

        elif rtype == "file-history-snapshot":
            backups = rec.get("snapshot", {}).get("trackedFileBackups", {})
            if isinstance(backups, dict):
                stats["files_edited"] = max(stats.get("files_edited", 0), len(backups))

        elif rtype == "ai-title":
            ai_title = rec.get("aiTitle") or ai_title

        elif rtype == "summary":
            if rec.get("summary"):
                stats["summary"] = str(rec["summary"])[:100]

    if ai_title and not stats["summary"]:
        stats["summary"] = ai_title[:100]
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


def workbuddy_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from a WorkBuddy session.

    - user: content[].input_text, strip system-reminder / user_query wrappers
    - assistant: content[].output_text
    - reasoning records → [THINKING] when thinking_limit allows
    """
    p = Path(session_path)
    if not p.exists() or not p.is_file():
        return

    count = 0
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        ts_ms = rec.get("timestamp", 0)
        ts = _normalize_timestamp(ts_ms) if ts_ms else ""

        if rtype == "reasoning" and role in ("assistant", "both") and thinking_limit != -1:
            content = rec.get("content") or rec.get("rawContent") or ""
            text = _extract_content_text(content) if not isinstance(content, str) else content.strip()
            if text:
                if thinking_limit > 0:
                    text = text[:thinking_limit]
                yield {"role": "ASSISTANT", "timestamp": ts, "text": "[THINKING] " + text[:500]}
                count += 1
                if limit and count >= limit:
                    return
            continue

        if rtype != "message":
            continue

        msg_role = rec.get("role", "")
        content = rec.get("content", [])

        if msg_role == "user" and role in ("user", "both"):
            texts = []
            if isinstance(content, str):
                cleaned = _workbuddy_user_text(content)
                if cleaned:
                    texts.append(cleaned)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") in ("input_text", "text", None):
                        t = (c.get("text") or "").strip()
                        if t:
                            cleaned = _workbuddy_user_text(t)
                            if cleaned:
                                texts.append(cleaned)
            if texts:
                yield {"role": "USER", "timestamp": ts, "text": "\n".join(texts)[:500]}
                count += 1
                if limit and count >= limit:
                    return

        elif msg_role == "assistant" and role in ("assistant", "both"):
            texts = []
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                        t = (c.get("text") or "").strip()
                        if t:
                            texts.append(t)
            if texts:
                yield {"role": "ASSISTANT", "timestamp": ts, "text": "\n".join(texts)[:500]}
                count += 1
                if limit and count >= limit:
                    return


def workbuddy_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls; join results by callId; detect exit-code errors."""
    path = Path(session_dir)
    if not path.exists():
        return
    calls = {}
    results = {}
    for rec in _iter_jsonl(path):
        rtype = rec.get("type", "")
        ts_ms = rec.get("timestamp", 0)
        ts = _normalize_timestamp(ts_ms) if ts_ms else ""
        if rtype == "function_call":
            name = rec.get("name", "")
            if tool_filter and name != tool_filter:
                continue
            call_id = rec.get("callId") or rec.get("call_id") or rec.get("id") or ""
            args = rec.get("arguments", "")
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)
            calls[call_id] = {
                "name": name,
                "ts": ts,
                "input_preview": str(args)[:150] if args else "",
            }
        elif rtype == "function_call_result":
            call_id = rec.get("callId") or rec.get("call_id") or ""
            status = str(rec.get("status") or "").lower()
            is_error, preview = _workbuddy_is_error_output(rec.get("output"))
            if status in ("failed", "error", "cancelled"):
                is_error = True
            results[call_id] = {"preview": preview, "is_error": is_error}
    yield from _match_call_results(calls, results, errors_only, limit)


def workbuddy_session_path(cwd, session_id=None):
    """Find a WorkBuddy session file."""
    projects_dir = WORKBUDDY_DIR / "projects"
    if not projects_dir.exists():
        return None
    if session_id:
        # exact then partial
        for p in projects_dir.rglob(f"{session_id}.jsonl"):
            return str(p)
        for p in projects_dir.rglob("*.jsonl"):
            if session_id in p.stem:
                return str(p)
        return None
    # newest
    newest = None
    newest_m = -1
    try:
        for p in projects_dir.rglob("*.jsonl"):
            if p.name.startswith("agent-"):
                continue
            m = p.stat().st_mtime
            if m > newest_m:
                newest_m = m
                newest = p
    except OSError:
        pass
    return str(newest) if newest else None
