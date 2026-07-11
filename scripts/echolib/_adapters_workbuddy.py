"""WorkBuddy adapter implementation — extracted from _adapters.py.

WorkBuddy session layout: ~/.workbuddy/projects/<slug>/<session_id>.jsonl
Buddy-format JSONL record types:  message / function_call / function_call_result /
ai-title / file-history-snapshot / summary. Tokens + model name live in
providerData.requestModelName / providerData.usage / message.usage.
"""
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


def workbuddy_list_sessions(cwd=None, limit=50, keyword=""):
    """List WorkBuddy sessions from ~/.workbuddy/projects/."""
    projects_dir = WORKBUDDY_DIR / "projects"
    if not projects_dir.exists():
        return []
    sessions = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        project_slug = project_dir.name
        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            sid = jsonl_file.stem
            if sid.startswith("agent-"):
                continue
            msg_count, first_prompt, ai_title = _workbuddy_quick_scan(jsonl_file)
            mtime = _normalize_timestamp(jsonl_file.stat().st_mtime)
            if keyword:
                searchable = (ai_title + " " + first_prompt).lower()
                if keyword.lower() not in searchable:
                    continue
            sessions.append(SessionMeta(
                session_id=sid, full_path=str(jsonl_file),
                created=mtime, modified=mtime,
                message_count=msg_count, git_branch="",
                summary=ai_title[:100] if ai_title else "",
                first_prompt=first_prompt[:200] if first_prompt else "",
                project_path=project_slug,
            ))
    sessions.sort(key=lambda s: str(s.created or ""), reverse=True)
    return sessions[:limit]


def _workbuddy_quick_scan(jsonl_path):
    """Quick scan: count messages, extract first prompt and title."""
    user_count = 0
    first_prompt = ""
    ai_title = ""
    for rec in _iter_jsonl(jsonl_path):
        rtype = rec.get("type", "")
        if rtype == "message" and rec.get("role") == "user":
            user_count += 1
            if not first_prompt:
                content = rec.get("content", [])
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "input_text":
                        text = c.get("text", "")
                        match = re.search(r'<system-reminder[^>]*>(.*?)</system-reminder>', text, re.DOTALL)
                        if match:
                            after = text[match.end():].strip()
                            first_prompt = after[:200] if after else text[:200]
                        else:
                            first_prompt = text[:200]
                        break
        elif rtype == "ai-title":
            ai_title = rec.get("aiTitle", "")
    return user_count, first_prompt, ai_title


def workbuddy_session_stats(session_dir):
    """Get stats for a WorkBuddy session."""
    from echolib._adapters import _empty_stats
    path = Path(session_dir)
    if not path.exists():
        return _empty_stats("workbuddy")
    stats = _empty_stats("workbuddy")
    stats["slug"] = path.stem
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
                pd = rec.get("providerData", {})
                model = pd.get("requestModelName", "") or pd.get("model", "")
                if model and not stats["model"]:
                    stats["model"] = model
                usage = rec.get("message", {}).get("usage", {})
                if usage:
                    stats["input_tokens"] += usage.get("input_tokens", 0)
                    stats["output_tokens"] += usage.get("output_tokens", 0)
                    stats["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
        elif rtype == "function_call":
            stats["tool_calls"] += 1
            pd = rec.get("providerData", {})
            usage = pd.get("usage", {})
            if usage:
                stats["input_tokens"] += usage.get("inputTokens", 0)
                stats["output_tokens"] += usage.get("outputTokens", 0)
        elif rtype == "function_call_result":
            output = rec.get("output", "")
            if isinstance(output, dict) and output.get("type") == "text":
                text = output.get("text", "")
                code_match = re.search(r'Exit Code:\\s*(\\d+)', text)
                if code_match and int(code_match.group(1)) != 0:
                    stats["errors"] += 1
        elif rtype == "file-history-snapshot":
            backups = rec.get("snapshot", {}).get("trackedFileBackups", {})
            fc = len(backups) if isinstance(backups, dict) else 0
            if fc > stats.get("files_edited", 0):
                stats["files_edited"] = fc
        elif rtype == "summary":
            stats["summary"] = rec.get("summary", "")[:100]
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


def workbuddy_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from a WorkBuddy session.

    WorkBuddy format:
      - message with role=user → content[].type=input_text, content[].text
      - message with role=assistant → content[].type=output_text, content[].text
    """
    p = Path(session_path)
    if not p.exists() or not p.is_file():
        return

    count = 0
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        ts_ms = rec.get("timestamp", 0)
        ts = _normalize_timestamp(ts_ms) if ts_ms else ""

        if rtype != "message":
            continue

        msg_role = rec.get("role", "")
        content = rec.get("content", [])

        if msg_role == "user" and role in ("user", "both"):
            if isinstance(content, list):
                texts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "input_text":
                        t = c.get("text", "").strip()
                        if t:
                            cleaned = _strip_system_reminder(t)
                            if cleaned:
                                texts.append(cleaned)
                if texts:
                    yield {"role": "USER", "timestamp": ts, "text": "\n".join(texts)[:500]}
                    count += 1
                    if limit and count >= limit:
                        return

        elif msg_role == "assistant" and role in ("assistant", "both"):
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
                    yield {"role": "ASSISTANT", "timestamp": ts, "text": "\n".join(texts)[:500]}
                    count += 1
                    if limit and count >= limit:
                        return


def workbuddy_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from a WorkBuddy session."""
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
            call_id = rec.get("callId", "")
            args = rec.get("arguments", "")
            calls[call_id] = {"name": name, "ts": ts, "input_preview": str(args)[:150] if args else ""}
        elif rtype == "function_call_result":
            call_id = rec.get("callId", "")
            output = rec.get("output", "")
            is_error = False
            if isinstance(output, dict) and output.get("type") == "text":
                text = output.get("text", "")
                code_match = re.search(r'Exit Code:\\s*(\\d+)', text)
                if code_match and int(code_match.group(1)) != 0:
                    is_error = True
                results[call_id] = {"preview": text[:150].replace("\\n", " ") if text else "", "is_error": is_error}
            elif isinstance(output, list):
                texts = [c.get("text", "") for c in output if isinstance(c, dict)]
                results[call_id] = {"preview": " ".join(texts)[:150].replace("\\n", " "), "is_error": False}
    yield from _match_call_results(calls, results, errors_only, limit)


def workbuddy_session_path(cwd, session_id=None):
    """Find a WorkBuddy session file."""
    projects_dir = WORKBUDDY_DIR / "projects"
    if not projects_dir.exists():
        return None
    if session_id:
        for p in projects_dir.rglob(f"{session_id}.jsonl"):
            return p
    return None
