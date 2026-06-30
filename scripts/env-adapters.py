"""
env-adapters.py — New environment adapters for session-digger.

Adds support for: Codex (OpenAI), WorkBuddy, Trae CN (ByteDance),
plus a universal fallback adapter for unknown environments.

Usage:
    # Import into echolib.py or run standalone for testing
    from env_adapters import (
        codex_list_sessions, codex_session_stats, codex_extract_messages,
        codex_extract_tools, codex_session_path,
        workbuddy_list_sessions, workbuddy_session_stats,
        workbuddy_extract_messages, workbuddy_extract_tools,
        workbuddy_session_path,
        trae_list_sessions, trae_session_stats, trae_extract_messages,
        trae_extract_tools, trae_session_path,
        universal_list_sessions, universal_list_sessions_parallel,
        universal_session_stats, universal_extract_messages,
        universal_extract_tools, universal_session_path,
        scan_all_environments, scan_all_environments_parallel,
    )

Standalone test:
    python3 env-adapters.py scan          # Scan all environments
    python3 env-adapters.py stats <path>  # Stats for a session
    python3 env-adapters.py messages <path>  # Extract messages
"""

import json
import os
import re
import concurrent.futures
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CODEX_DIR = Path.home() / ".codex"
WORKBUDDY_DIR = Path.home() / ".workbuddy"
TRAE_DIR = Path.home() / ".trae-cn"


# ---------------------------------------------------------------------------
# Helpers (shared with echolib.py)
# ---------------------------------------------------------------------------

def _normalize_timestamp(ts):
    """Normalize various timestamp formats to ISO string."""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        # Epoch seconds or milliseconds
        if ts > 1e12:
            ts = ts / 1000.0
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (OSError, ValueError):
            return str(ts)
    return str(ts)


def _sanitize_tsv(s, max_len=0):
    """Clean a string for TSV output."""
    s = s.replace("\t", " ").replace("\n", " ")
    if max_len and len(s) > max_len:
        s = s[:max_len - 3] + "..."
    return s


class SessionMeta:
    """Lightweight session metadata (matches echolib.py SessionMeta)."""
    __slots__ = (
        "session_id", "full_path", "created", "modified",
        "message_count", "git_branch", "summary", "first_prompt",
        "project_path",
    )

    def __init__(self, **kwargs):
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k, ""))

    def to_tsv(self):
        fields = [
            str(self.session_id), str(self.created), str(self.modified),
            str(self.message_count), str(self.git_branch),
            _sanitize_tsv(str(self.summary), 80),
            _sanitize_tsv(str(self.first_prompt), 100),
            str(self.project_path), str(self.full_path),
        ]
        return "\t".join(fields)


# ═══════════════════════════════════════════════════════════════════════════
# Codex (OpenAI) Adapter
# ═══════════════════════════════════════════════════════════════════════════
#
# Directory layout:
#   ~/.codex/
#     session_index.jsonl          # {id, thread_name, updated_at}
#     sessions/YYYY/MM/DD/
#       rollout-{ISO}-{UUID}.jsonl # Full conversation rollout
#
# Record structure:
#   {timestamp, type, payload}
#   type: session_meta | turn_context | response_item | event_msg
#
# response_item.payload.type:
#   message | reasoning | function_call | function_call_output |
#   custom_tool_call | custom_tool_call_output |
#   tool_search_call | tool_search_output | web_search_call
#
# event_msg.payload.type:
#   task_started | task_complete | user_message | agent_message |
#   agent_reasoning | token_count | mcp_tool_call_end | patch_apply_end


def codex_list_sessions(cwd=None, limit=50, keyword=""):
    """List Codex sessions from ~/.codex/sessions/ using session_index.jsonl."""
    index_path = CODEX_DIR / "session_index.jsonl"
    if not index_path.exists():
        # Fallback: scan rollout files directly
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

            # Find the rollout file for this session
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
        # Extract session ID from filename: rollout-{ts}-{uuid}.jsonl
        name = rollout.stem
        parts = name.split("-")
        sid = parts[-1] if len(parts) >= 2 else name

        msg_count, first_prompt = _codex_quick_scan(rollout)
        mtime = _normalize_timestamp(rollout.stat().st_mtime)

        if keyword and keyword.lower() not in first_prompt.lower():
            continue

        sessions.append(SessionMeta(
            session_id=sid,
            full_path=str(rollout),
            created=mtime,
            modified=mtime,
            message_count=msg_count,
            git_branch="",
            summary="",
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
    try:
        with open(rollout_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                rtype = rec.get("type", "")
                payload = rec.get("payload", {})
                if rtype == "event_msg" and payload.get("type") == "user_message":
                    user_count += 1
                    if not first_prompt:
                        first_prompt = payload.get("message", "")[:200]
    except OSError:
        pass
    return user_count, first_prompt


def codex_session_stats(session_dir):
    """Get stats for a Codex rollout session."""
    path = Path(session_dir)
    if not path.exists():
        return _empty_stats("codex")

    stats = _empty_stats("codex")
    stats["slug"] = path.stem

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

                rtype = rec.get("type", "")
                ts = rec.get("timestamp", "")
                payload = rec.get("payload", {})

                if ts:
                    if not stats["started"] or ts < stats["started"]:
                        stats["started"] = ts
                    if ts > stats["ended"]:
                        stats["ended"] = ts

                if rtype == "session_meta":
                    mp = payload.get("model_provider", "")
                    model = payload.get("model", "") or mp
                    if model:
                        stats["model"] = model

                elif rtype == "turn_context":
                    m = payload.get("model", "")
                    if m and not stats["model"]:
                        stats["model"] = m

                elif rtype == "response_item":
                    ptype = payload.get("type", "")
                    if ptype == "message":
                        role = payload.get("role", "")
                        if role == "user":
                            stats["user_messages"] += 1
                        elif role == "assistant":
                            stats["assistant_messages"] += 1
                    elif ptype == "function_call":
                        stats["tool_calls"] += 1
                    elif ptype == "custom_tool_call":
                        stats["tool_calls"] += 1
                    elif ptype == "function_call_output":
                        output = payload.get("output", "")
                        if isinstance(output, str) and "exit code" in output.lower():
                            if "exit code: 0" not in output.lower():
                                stats["errors"] += 1

                elif rtype == "event_msg":
                    etype = payload.get("type", "")
                    if etype == "token_count":
                        info = payload.get("info") or {}
                        total = info.get("total_token_usage") or {}
                        stats["input_tokens"] += total.get("input_tokens", 0)
                        stats["output_tokens"] += total.get("output_tokens", 0)

    except OSError:
        pass

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


def codex_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """Extract messages from a Codex rollout session."""
    path = Path(session_dir)
    if not path.exists():
        return

    count = 0
    thinking_count = 0
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

                rtype = rec.get("type", "")
                payload = rec.get("payload", {})
                ts = rec.get("timestamp", "")

                if rtype == "response_item":
                    ptype = payload.get("type", "")
                    if ptype == "message":
                        role_name = payload.get("role", "")
                        if role_name == "user" and role in ("user", "both"):
                            parts = payload.get("content", [])
                            texts = [p.get("text", "") for p in parts
                                     if isinstance(p, dict) and p.get("type") == "input_text"]
                            if texts:
                                yield {"role": "USER", "timestamp": ts,
                                       "text": "\n".join(texts)}
                                count += 1
                        elif role_name == "assistant" and role in ("assistant", "both"):
                            parts = payload.get("content", [])
                            texts = [p.get("text", "") for p in parts
                                     if isinstance(p, dict) and p.get("type") == "output_text"]
                            if texts:
                                yield {"role": "ASSISTANT", "timestamp": ts,
                                       "text": "\n".join(texts)}
                                count += 1
                        elif role_name == "developer" and role in ("assistant", "both"):
                            parts = payload.get("content", [])
                            texts = [p.get("text", "") for p in parts
                                     if isinstance(p, dict) and p.get("type") == "input_text"]
                            if texts:
                                yield {"role": "SYSTEM", "timestamp": ts,
                                       "text": "\n".join(texts)}
                                count += 1

                    elif ptype == "reasoning" and role in ("assistant", "both"):
                        if thinking_limit and thinking_count >= thinking_limit:
                            continue
                        content = payload.get("content", [])
                        texts = [p.get("text", "") for p in (content or [])
                                 if isinstance(p, dict) and p.get("type") == "reasoning_text"]
                        if texts:
                            yield {"role": "THINKING", "timestamp": ts,
                                   "text": "\n".join(texts)}
                            thinking_count += 1

                elif rtype == "event_msg":
                    etype = payload.get("type", "")
                    if etype == "user_message" and role in ("user", "both"):
                        msg = payload.get("message", "")
                        if msg and not count:  # Only if not already captured
                            yield {"role": "USER", "timestamp": ts, "text": msg}
                            count += 1
                    elif etype == "agent_message" and role in ("assistant", "both"):
                        msg = payload.get("message", "")
                        if msg:
                            yield {"role": "ASSISTANT", "timestamp": ts, "text": msg}
                            count += 1

                if limit and count >= limit:
                    return

    except OSError:
        pass


def codex_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from a Codex rollout session."""
    path = Path(session_dir)
    if not path.exists():
        return

    # Collect calls and outputs
    calls = {}  # call_id -> {name, ts, input_preview}
    outputs = {}  # call_id -> {output_preview, is_error}

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

                rtype = rec.get("type", "")
                if rtype != "response_item":
                    continue
                payload = rec.get("payload", {})
                ptype = payload.get("type", "")
                ts = rec.get("timestamp", "")

                if ptype == "function_call":
                    name = payload.get("name", "")
                    if tool_filter and name != tool_filter:
                        continue
                    call_id = payload.get("call_id", "")
                    args = payload.get("arguments", "")
                    calls[call_id] = {
                        "name": name, "ts": ts,
                        "input_preview": str(args)[:150] if args else "",
                    }

                elif ptype == "function_call_output":
                    call_id = payload.get("call_id", "")
                    output = payload.get("output", "")
                    is_error = False
                    if isinstance(output, str):
                        is_error = "exit code: 0" not in output.lower() and "exit code" in output.lower()
                    outputs[call_id] = {
                        "preview": str(output)[:150].replace("\n", " ") if output else "",
                        "is_error": is_error,
                    }

                elif ptype == "custom_tool_call":
                    name = payload.get("name", "")
                    if tool_filter and name != tool_filter:
                        continue
                    call_id = payload.get("call_id", "")
                    inp = payload.get("input", "")
                    calls[call_id] = {
                        "name": name, "ts": ts,
                        "input_preview": str(inp)[:150] if inp else "",
                    }

                elif ptype == "custom_tool_call_output":
                    call_id = payload.get("call_id", "")
                    output = payload.get("output", "")
                    is_error = False
                    if isinstance(output, str) and "Failed" in output:
                        is_error = True
                    outputs[call_id] = {
                        "preview": str(output)[:150].replace("\n", " ") if output else "",
                        "is_error": is_error,
                    }

    except OSError:
        pass

    count = 0
    for call_id, info in sorted(calls.items(), key=lambda x: x[1]["ts"]):
        result_info = outputs.get(call_id, {"preview": "(no result)", "is_error": False})
        status = "error" if result_info["is_error"] else "ok"

        if errors_only and not result_info["is_error"]:
            continue

        if limit and count >= limit:
            return

        yield {
            "timestamp": info["ts"],
            "name": info["name"],
            "status": status,
            "key_input": info["input_preview"],
            "result_preview": result_info["preview"],
        }
        count += 1


def codex_session_path(cwd, session_id=None):
    """Find a Codex session file."""
    sessions_dir = CODEX_DIR / "sessions"
    if not sessions_dir.exists():
        return None
    if session_id:
        for p in sessions_dir.rglob(f"*{session_id}*.jsonl"):
            return p
    return None


# ═══════════════════════════════════════════════════════════════════════════
# WorkBuddy Adapter
# ═══════════════════════════════════════════════════════════════════════════
#
# Directory layout:
#   ~/.workbuddy/
#     workspace-state.json        # {version, sessions: {slug: {id, path}}}
#     projects/<slug>/
#       <session-uuid>.jsonl      # Main session JSONL
#       <session-uuid>/
#         subagents/              # Sub-agent JSONL files
#
# Record structure (flat, tree-linked via parentId):
#   {id, timestamp(int ms), type, sessionId, cwd, parentId?, role?, ...}
#
# type enum:
#   message | reasoning | function_call | function_call_result |
#   file-history-snapshot | ai-title | summary
#
# Tool calls: function_call.callId -> function_call_result.callId


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
            # Skip sub-agent files
            if sid.startswith("agent-"):
                continue

            msg_count, first_prompt, ai_title = _workbuddy_quick_scan(jsonl_file)
            mtime = _normalize_timestamp(jsonl_file.stat().st_mtime)

            if keyword:
                searchable = (ai_title + " " + first_prompt).lower()
                if keyword.lower() not in searchable:
                    continue

            sessions.append(SessionMeta(
                session_id=sid,
                full_path=str(jsonl_file),
                created=mtime,
                modified=mtime,
                message_count=msg_count,
                git_branch="",
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
                rtype = rec.get("type", "")
                if rtype == "message" and rec.get("role") == "user":
                    user_count += 1
                    if not first_prompt:
                        content = rec.get("content", [])
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "input_text":
                                text = c.get("text", "")
                                # Strip system-reminder tags
                                match = re.search(r'<system-reminder[^>]*>(.*?)</system-reminder>',
                                                  text, re.DOTALL)
                                if match:
                                    # Extract the actual user message after the reminder
                                    after = text[match.end():].strip()
                                    first_prompt = after[:200] if after else text[:200]
                                else:
                                    first_prompt = text[:200]
                                break
                elif rtype == "ai-title":
                    ai_title = rec.get("aiTitle", "")
    except OSError:
        pass
    return user_count, first_prompt, ai_title


def workbuddy_session_stats(session_dir):
    """Get stats for a WorkBuddy session."""
    path = Path(session_dir)
    if not path.exists():
        return _empty_stats("workbuddy")

    stats = _empty_stats("workbuddy")
    stats["slug"] = path.stem

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
                        # Extract model from providerData
                        pd = rec.get("providerData", {})
                        model = pd.get("requestModelName", "") or pd.get("model", "")
                        if model and not stats["model"]:
                            stats["model"] = model
                        # Extract token usage
                        usage = rec.get("message", {}).get("usage", {})
                        if usage:
                            stats["input_tokens"] += usage.get("input_tokens", 0)
                            stats["output_tokens"] += usage.get("output_tokens", 0)
                            stats["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)

                elif rtype == "function_call":
                    stats["tool_calls"] += 1
                    # Token usage from providerData
                    pd = rec.get("providerData", {})
                    usage = pd.get("usage", {})
                    if usage:
                        stats["input_tokens"] += usage.get("inputTokens", 0)
                        stats["output_tokens"] += usage.get("outputTokens", 0)

                elif rtype == "function_call_result":
                    output = rec.get("output", "")
                    if isinstance(output, dict) and output.get("type") == "text":
                        text = output.get("text", "")
                        if "Exit Code:" in text:
                            code_match = re.search(r'Exit Code:\s*(\d+)', text)
                            if code_match and int(code_match.group(1)) != 0:
                                stats["errors"] += 1

                elif rtype == "file-history-snapshot":
                    backups = rec.get("snapshot", {}).get("trackedFileBackups", {})
                    fc = len(backups) if isinstance(backups, dict) else 0
                    if fc > stats.get("files_edited", 0):
                        stats["files_edited"] = fc

                elif rtype == "summary":
                    stats["summary"] = rec.get("summary", "")[:100]

    except OSError:
        pass

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


def workbuddy_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """Extract messages from a WorkBuddy session."""
    path = Path(session_dir)
    if not path.exists():
        return

    count = 0
    thinking_count = 0
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

                rtype = rec.get("type", "")
                ts_ms = rec.get("timestamp", 0)
                ts = _normalize_timestamp(ts_ms) if ts_ms else ""

                if rtype == "message":
                    role_name = rec.get("role", "")
                    content = rec.get("content", [])

                    if role_name == "user" and role in ("user", "both"):
                        texts = []
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "input_text":
                                texts.append(c.get("text", ""))
                        if texts:
                            yield {"role": "USER", "timestamp": ts,
                                   "text": "\n".join(texts)}
                            count += 1

                    elif role_name == "assistant" and role in ("assistant", "both"):
                        texts = []
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                texts.append(c.get("text", ""))
                        if texts:
                            yield {"role": "ASSISTANT", "timestamp": ts,
                                   "text": "\n".join(texts)}
                            count += 1

                elif rtype == "reasoning" and role in ("assistant", "both"):
                    if thinking_limit and thinking_count >= thinking_limit:
                        continue
                    raw = rec.get("rawContent", [])
                    texts = [c.get("text", "") for c in raw
                             if isinstance(c, dict) and c.get("type") == "reasoning_text"]
                    if texts:
                        yield {"role": "THINKING", "timestamp": ts,
                               "text": "\n".join(texts)}
                        thinking_count += 1

                if limit and count >= limit:
                    return

    except OSError:
        pass


def workbuddy_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from a WorkBuddy session."""
    path = Path(session_dir)
    if not path.exists():
        return

    # Collect calls and results (linked by callId)
    calls = {}  # callId -> {name, ts, input_preview}
    results = {}  # callId -> {preview, is_error}

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

                rtype = rec.get("type", "")
                ts_ms = rec.get("timestamp", 0)
                ts = _normalize_timestamp(ts_ms) if ts_ms else ""

                if rtype == "function_call":
                    name = rec.get("name", "")
                    if tool_filter and name != tool_filter:
                        continue
                    call_id = rec.get("callId", "")
                    args = rec.get("arguments", "")
                    calls[call_id] = {
                        "name": name, "ts": ts,
                        "input_preview": str(args)[:150] if args else "",
                    }

                elif rtype == "function_call_result":
                    call_id = rec.get("callId", "")
                    output = rec.get("output", "")
                    is_error = False
                    if isinstance(output, dict) and output.get("type") == "text":
                        text = output.get("text", "")
                        if "Exit Code:" in text:
                            code_match = re.search(r'Exit Code:\s*(\d+)', text)
                            if code_match and int(code_match.group(1)) != 0:
                                is_error = True
                        results[call_id] = {
                            "preview": text[:150].replace("\n", " ") if text else "",
                            "is_error": is_error,
                        }
                    elif isinstance(output, list):
                        texts = [c.get("text", "") for c in output
                                 if isinstance(c, dict)]
                        combined = " ".join(texts)[:150]
                        results[call_id] = {
                            "preview": combined.replace("\n", " "),
                            "is_error": False,
                        }

    except OSError:
        pass

    count = 0
    for call_id, info in sorted(calls.items(), key=lambda x: x[1]["ts"]):
        result_info = results.get(call_id, {"preview": "(no result)", "is_error": False})
        status = "error" if result_info["is_error"] else "ok"

        if errors_only and not result_info["is_error"]:
            continue

        if limit and count >= limit:
            return

        yield {
            "timestamp": info["ts"],
            "name": info["name"],
            "status": status,
            "key_input": info["input_preview"],
            "result_preview": result_info["preview"],
        }
        count += 1


def workbuddy_session_path(cwd, session_id=None):
    """Find a WorkBuddy session file."""
    projects_dir = WORKBUDDY_DIR / "projects"
    if not projects_dir.exists():
        return None
    if session_id:
        for p in projects_dir.rglob(f"{session_id}.jsonl"):
            return p
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Trae CN (ByteDance) Adapter
# ═══════════════════════════════════════════════════════════════════════════
#
# IMPORTANT: Trae CN stores LLM-generated SUMMARIES, not raw conversations.
# Each record is a post-hoc summary of one user-assistant exchange:
#   {intent, actions, outcome, learned, message_summary_time, message_id}
#
# Directory layout:
#   ~/.trae-cn/memory/projects/<project-path>/
#     project_memory.md           # Hard constraints + lessons
#     YYYYMMDD/
#       topics.md                 # Topic summaries
#       session_memory_*.jsonl    # Per-session memory summaries
#
# This adapter extracts summary-level information only.


def trae_list_sessions(cwd=None, limit=50, keyword=""):
    """List Trae CN sessions from ~/.trae-cn/memory/projects/."""
    memory_dir = TRAE_DIR / "memory" / "projects"
    if not memory_dir.exists():
        return []

    sessions = {}
    for project_dir in sorted(memory_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        project_path = project_dir.name

        for date_dir in sorted(project_dir.iterdir()):
            if not date_dir.is_dir() or not date_dir.name.isdigit():
                continue

            for jsonl_file in sorted(date_dir.glob("session_memory_*.jsonl")):
                sid = jsonl_file.stem.replace("session_memory_", "")

                # Accumulate across date dirs
                if sid not in sessions:
                    sessions[sid] = {
                        "path": str(jsonl_file),
                        "project": project_path,
                        "intents": [],
                        "date": date_dir.name,
                        "mtime": _normalize_timestamp(jsonl_file.stat().st_mtime),
                    }
                sessions[sid]["intents"].extend(
                    _trae_extract_intents(jsonl_file)
                )

    result = []
    for sid, info in sessions.items():
        first_intent = info["intents"][0] if info["intents"] else ""
        summary = " | ".join(info["intents"][:3])  # First 3 intents as summary

        if keyword and keyword.lower() not in summary.lower():
            continue

        result.append(SessionMeta(
            session_id=sid,
            full_path=info["path"],
            created=info["mtime"],
            modified=info["mtime"],
            message_count=len(info["intents"]),
            git_branch="",
            summary=summary[:100],
            first_prompt=first_intent[:200],
            project_path=info["project"],
        ))

    result.sort(key=lambda s: str(s.created or ""), reverse=True)
    return result[:limit]


def _trae_extract_intents(jsonl_path):
    """Extract intent strings from a Trae CN memory JSONL."""
    intents = []
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
                intent = rec.get("intent", "")
                if intent:
                    intents.append(intent)
    except OSError:
        pass
    return intents


def trae_session_stats(session_dir):
    """Get stats for a Trae CN session (summary-level only)."""
    path = Path(session_dir)
    if not path.exists():
        return _empty_stats("trae-cn")

    stats = _empty_stats("trae-cn")
    stats["model"] = "trae-cn (summary only)"
    stats["slug"] = path.stem

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

    except OSError:
        pass

    stats["summary"] = f"Trae CN summary: {stats['user_messages']} turns"
    return stats


def trae_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """
    Extract summarized messages from a Trae CN session.

    NOTE: These are LLM-generated summaries, not raw messages.
    User intent -> actions taken -> outcome -> lessons learned.
    """
    path = Path(session_dir)
    if not path.exists():
        return

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

                ts = rec.get("message_summary_time", "")

                if role in ("user", "both"):
                    intent = rec.get("intent", "")
                    if intent:
                        yield {"role": "USER", "timestamp": ts,
                               "text": f"[意图] {intent}"}
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
                        yield {"role": "ASSISTANT", "timestamp": ts,
                               "text": "\n".join(parts)}
                        count += 1

                if limit and count >= limit:
                    return

    except OSError:
        pass


def trae_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """
    Extract action summaries from a Trae CN session.

    NOTE: Trae CN doesn't store raw tool calls, only action descriptions.
    Returns action strings as pseudo-tool-calls.
    """
    path = Path(session_dir)
    if not path.exists():
        return

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

                ts = rec.get("message_summary_time", "")
                actions = rec.get("actions", [])
                if not isinstance(actions, list):
                    continue

                for action in actions:
                    if tool_filter and tool_filter.lower() not in action.lower():
                        continue
                    if limit and count >= limit:
                        return

                    yield {
                        "timestamp": ts,
                        "name": f"[trae-action] {action[:50]}",
                        "status": "ok",
                        "key_input": action[:150],
                        "result_preview": "",
                    }
                    count += 1

    except OSError:
        pass


def trae_session_path(cwd, session_id=None):
    """Find a Trae CN session file."""
    memory_dir = TRAE_DIR / "memory" / "projects"
    if not memory_dir.exists():
        return None
    if session_id:
        for p in memory_dir.rglob(f"session_memory_{session_id}.jsonl"):
            return p
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Universal Fallback Adapter
# ═══════════════════════════════════════════════════════════════════════════
#
# For unknown environments: auto-detect JSONL structure and extract
# whatever information is available.


def universal_list_sessions(home_dir=None, env_name="unknown", limit=50, keyword=""):
    """
    Universal session discovery: find JSONL files in any environment directory.
    Works with ANY environment by scanning for .jsonl files.
    """
    if home_dir is None:
        home_dir = Path.home()

    # Look for common session patterns
    search_dirs = []
    for pattern in ["sessions", "projects", "memory", "data"]:
        candidate = home_dir / pattern
        if candidate.exists():
            search_dirs.append(candidate)

    # Also scan root for .jsonl files
    jsonl_files = []
    for search_dir in search_dirs:
        jsonl_files.extend(search_dir.rglob("*.jsonl"))

    # Fallback: scan the home dir itself
    if not jsonl_files:
        jsonl_files = list(home_dir.rglob("*.jsonl"))

    sessions = []
    for jf in sorted(jsonl_files, key=lambda p: p.stat().st_mtime, reverse=True):
        # Skip very small files (likely config, not sessions)
        if jf.stat().st_size < 100:
            continue

        sid = jf.stem
        mtime = _normalize_timestamp(jf.stat().st_mtime)

        # Quick scan for content
        first_prompt = _universal_quick_scan(jf)

        if keyword and keyword.lower() not in first_prompt.lower():
            continue

        sessions.append(SessionMeta(
            session_id=sid,
            full_path=str(jf),
            created=mtime,
            modified=mtime,
            message_count=0,  # Unknown without full parse
            git_branch="",
            summary=f"[{env_name}] {jf.parent.name}",
            first_prompt=first_prompt[:200],
            project_path=str(jf.parent),
        ))

    return sessions[:limit]


def _universal_quick_scan(jsonl_path):
    """Try to extract any text content from the first few records."""
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 20:  # Only check first 20 lines
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Try common message patterns
                # Pattern 1: {type: "user", message: {content: ...}}
                msg = rec.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        return content[:200]
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                return block.get("text", "")[:200]

                # Pattern 2: {type: "human", content: [...]}
                if rec.get("type") in ("human", "user"):
                    content = rec.get("content", "")
                    if isinstance(content, str) and content.strip():
                        return content[:200]
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                return block.get("text", "")[:200]

                # Pattern 3: {role: "user", content: ...}
                if rec.get("role") in ("user", "human"):
                    content = rec.get("content", "")
                    if isinstance(content, str) and content.strip():
                        return content[:200]

                # Pattern 4: {text: ...} or {input: ...}
                for key in ("text", "input", "prompt", "query"):
                    val = rec.get(key, "")
                    if isinstance(val, str) and val.strip():
                        return val[:200]

    except OSError:
        pass
    return ""


def universal_session_stats(session_path):
    """
    Universal stats: parse any JSONL and extract whatever metrics are available.
    Uses heuristics to detect message types.
    """
    path = Path(session_path)
    stats = _empty_stats("unknown")
    stats["slug"] = path.stem

    if not path.exists():
        return stats

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

                # Timestamp
                ts = ""
                for key in ("timestamp", "time", "created_at", "date"):
                    val = rec.get(key, "")
                    if val:
                        ts = _normalize_timestamp(val)
                        break
                if ts:
                    if not stats["started"] or ts < stats["started"]:
                        stats["started"] = ts
                    if ts > stats["ended"]:
                        stats["ended"] = ts

                # Model
                if not stats["model"]:
                    for key in ("model", "model_name", "engine"):
                        val = rec.get(key, "")
                        if val:
                            stats["model"] = str(val)
                            break
                    msg = rec.get("message", {})
                    if isinstance(msg, dict) and not stats["model"]:
                        stats["model"] = msg.get("model", "")

                # Message type heuristics
                rtype = rec.get("type", "")
                role = rec.get("role", "")

                is_user = (
                    rtype in ("user", "human", "turn.prompt")
                    or role in ("user", "human")
                )
                is_assistant = (
                    rtype in ("assistant", "ai", "bot", "text", "agent")
                    or role in ("assistant", "ai", "bot")
                )
                is_tool_call = (
                    rtype in ("tool_call", "function_call", "tool.call")
                    or "function_call" in rtype
                )
                is_tool_result = (
                    rtype in ("tool_result", "function_call_result", "tool.result")
                    or "function_call_output" in rtype
                )

                if is_user:
                    stats["user_messages"] += 1
                elif is_assistant:
                    stats["assistant_messages"] += 1
                elif is_tool_call:
                    stats["tool_calls"] += 1
                elif is_tool_result:
                    # Check for errors
                    output = rec.get("output", rec.get("result", ""))
                    if isinstance(output, str) and "error" in output.lower():
                        stats["errors"] += 1

    except OSError:
        pass

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


def universal_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """
    Universal message extraction: try common patterns to find messages.
    """
    path = Path(session_path)
    if not path.exists():
        return

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

                ts = ""
                for key in ("timestamp", "time", "created_at"):
                    val = rec.get(key, "")
                    if val:
                        ts = _normalize_timestamp(val)
                        break

                rtype = rec.get("type", "")
                role_name = rec.get("role", "")

                # Try to extract user messages
                if role in ("user", "both"):
                    text = _universal_extract_text(rec, "user")
                    if text:
                        yield {"role": "USER", "timestamp": ts, "text": text}
                        count += 1
                        continue

                # Try to extract assistant messages
                if role in ("assistant", "both"):
                    text = _universal_extract_text(rec, "assistant")
                    if text:
                        yield {"role": "ASSISTANT", "timestamp": ts, "text": text}
                        count += 1
                        continue

                if limit and count >= limit:
                    return

    except OSError:
        pass


def _universal_extract_text(rec, target_role):
    """Try to extract text from a record using common patterns."""
    rtype = rec.get("type", "")
    role = rec.get("role", "")

    # Determine if this record matches the target role
    if target_role == "user":
        is_match = (
            rtype in ("user", "human", "turn.prompt")
            or role in ("user", "human")
        )
    else:
        is_match = (
            rtype in ("assistant", "ai", "bot", "text", "agent")
            or role in ("assistant", "ai", "bot")
        )

    if not is_match:
        return ""

    # Try to extract text from various formats
    # Format 1: message.content
    msg = rec.get("message", {})
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            return content[:500]
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                    elif "text" in block:
                        texts.append(block["text"])
            if texts:
                return "\n".join(texts)[:500]

    # Format 2: content field directly
    content = rec.get("content", "")
    if isinstance(content, str) and content.strip():
        return content[:500]
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif "text" in block:
                    texts.append(block["text"])
        if texts:
            return "\n".join(texts)[:500]

    # Format 3: text/input/prompt fields
    for key in ("text", "input", "prompt", "query", "message_text"):
        val = rec.get(key, "")
        if isinstance(val, str) and val.strip():
            return val[:500]

    return ""


def universal_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """
    Universal tool extraction: try to find tool calls in any JSONL format.
    """
    path = Path(session_path)
    if not path.exists():
        return

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

                rtype = rec.get("type", "")

                # Detect tool calls
                is_tool_call = (
                    rtype in ("tool_call", "function_call", "tool.call")
                    or "function_call" in rtype
                )
                is_tool_result = (
                    rtype in ("tool_result", "function_call_result", "tool.result")
                    or "function_call_output" in rtype
                )

                if not (is_tool_call or is_tool_result):
                    continue

                ts = ""
                for key in ("timestamp", "time", "created_at"):
                    val = rec.get(key, "")
                    if val:
                        ts = _normalize_timestamp(val)
                        break

                name = rec.get("name", rec.get("tool_name", ""))
                if tool_filter and name and name != tool_filter:
                    continue

                if is_tool_call:
                    args = rec.get("arguments", rec.get("input", rec.get("args", "")))
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)

                    status = "ok"
                    if errors_only:
                        continue  # Need result to check errors

                    if limit and count >= limit:
                        return

                    yield {
                        "timestamp": ts,
                        "name": name or f"[{rtype}]",
                        "status": status,
                        "key_input": str(args)[:150] if args else "",
                        "result_preview": "",
                    }
                    count += 1

    except OSError:
        pass


def universal_session_path(cwd, session_id=None):
    """Universal path finder: search for any JSONL file matching session_id."""
    home = Path.home()
    if session_id:
        for p in home.rglob(f"*{session_id}*.jsonl"):
            return p
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Environment Registry
# ═══════════════════════════════════════════════════════════════════════════

ENV_REGISTRY = {
    "claude": {
        "name": "Claude Code",
        "root": "~/.claude/projects/",
        "format": "jsonl",
        "adapter": "claude",  # Uses existing echolib.py adapter
    },
    "grok": {
        "name": "Grok Build",
        "root": "~/.grok/sessions/",
        "format": "jsonl",
        "adapter": "grok",
    },
    "kimi_code": {
        "name": "Kimi Code",
        "root": "~/.kimi-code/sessions/",
        "format": "jsonl",
        "adapter": "kimi_code",
    },
    "codex": {
        "name": "Codex (OpenAI)",
        "root": "~/.codex/sessions/",
        "format": "jsonl",
        "adapter": "codex",
    },
    "workbuddy": {
        "name": "WorkBuddy",
        "root": "~/.workbuddy/projects/",
        "format": "jsonl",
        "adapter": "workbuddy",
    },
    "trae_cn": {
        "name": "Trae CN (ByteDance)",
        "root": "~/.trae-cn/memory/projects/",
        "format": "jsonl-summary",  # Not raw conversation
        "adapter": "trae_cn",
    },
}

# Environments with known directories but no adapter yet
KNOWN_UNADAPTED = {
    "mimo": {"name": "MiMo", "root": "~/.mimo/projects/"},
    "qwen": {"name": "Qwen Code", "root": "~/.qwen/projects/"},
    "qoder": {"name": "Qoder", "root": "~/.qoder/cache/projects/"},
    "reasonix": {"name": "Reasonix", "root": "~/.reasonix/sessions/"},
    "openclaw-autoclaw": {"name": "OpenClaw AutoClaw", "root": "~/.openclaw-autoclaw/agents/"},
    "gstack": {"name": "GStack", "root": "~/.gstack/sessions/"},
    "codebuddy": {"name": "CodeBuddy", "root": "~/.codebuddy/sessions/"},
    "cc-switch": {"name": "CC-Switch", "root": "~/.cc-switch/"},
    "dimcode": {"name": "DimCode", "root": "~/.dimcode/v2/data/sessions/"},
}


def scan_all_environments():
    """
    Scan all known and unknown environments.
    Returns a list of {name, path, has_data, session_count, format_type}.
    """
    results = []

    # Check registered adapters
    for env_id, env_info in ENV_REGISTRY.items():
        root = Path(os.path.expanduser(env_info["root"]))
        exists = root.exists()
        session_count = 0
        if exists:
            session_count = len(list(root.rglob("*.jsonl")))
        results.append({
            "name": env_info["name"],
            "env_id": env_id,
            "path": str(root),
            "exists": exists,
            "session_count": session_count,
            "format": env_info["format"],
            "adapter": env_info["adapter"],
            "status": "adapted",
        })

    # Check known unadapted
    for env_id, env_info in KNOWN_UNADAPTED.items():
        root = Path(os.path.expanduser(env_info["root"]))
        exists = root.exists()
        session_count = 0
        if exists:
            session_count = len(list(root.rglob("*.jsonl")))
        results.append({
            "name": env_info["name"],
            "env_id": env_id,
            "path": str(root),
            "exists": exists,
            "session_count": session_count,
            "format": "unknown",
            "adapter": "universal",
            "status": "unadapted" if exists else "missing",
        })

    # Scan home for unknown environments
    home = Path.home()
    known_dirs = {e["root"].replace("~", str(home)).split("/")[0]
                  for e in list(ENV_REGISTRY.values()) + list(KNOWN_UNADAPTED.values())}
    known_dirs.add(str(home / ".claude"))
    known_dirs.add(str(home / ".zcode"))
    known_dirs.add(str(home / ".agents"))
    known_dirs.add(str(home / ".config"))
    known_dirs.add(str(home / ".cache"))
    known_dirs.add(str(home / ".npm"))
    known_dirs.add(str(home / ".cargo"))
    known_dirs.add(str(home / ".ssh"))
    known_dirs.add(str(home / ".local"))

    for dotdir in sorted(home.iterdir()):
        if not dotdir.is_dir() or not dotdir.name.startswith("."):
            continue
        if str(dotdir) in known_dirs:
            continue

        # Check for session data
        jsonl_count = len(list(dotdir.rglob("*.jsonl")))
        if jsonl_count > 0:
            results.append({
                "name": dotdir.name,
                "env_id": dotdir.name.lstrip("."),
                "path": str(dotdir),
                "exists": True,
                "session_count": jsonl_count,
                "format": "unknown",
                "adapter": "universal",
                "status": "discovered",
            })

    return results


def _fast_find_jsonl(directory):
    """快速查找JSONL文件，使用os.scandir()比Path.rglob()快2-3倍。"""
    results = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                    results.append(Path(entry.path))
                elif entry.is_dir(follow_symlinks=False):
                    results.extend(_fast_find_jsonl(entry.path))
    except OSError:
        pass
    return results


def _scan_single_file(jf, env_name, keyword):
    """扫描单个JSONL文件，返回SessionMeta或None。用于并行处理。"""
    try:
        # 跳过非常小的文件（可能是配置文件，不是会话）
        if jf.stat().st_size < 100:
            return None
        
        sid = jf.stem
        mtime = _normalize_timestamp(jf.stat().st_mtime)
        
        # 快速扫描内容
        first_prompt = _universal_quick_scan(jf)
        
        if keyword and keyword.lower() not in first_prompt.lower():
            return None
        
        return SessionMeta(
            session_id=sid,
            full_path=str(jf),
            created=mtime,
            modified=mtime,
            message_count=0,  # 未知，需要完整解析
            git_branch="",
            summary=f"[{env_name}] {jf.parent.name}",
            first_prompt=first_prompt[:200],
            project_path=str(jf.parent),
        )
    except OSError:
        return None


def universal_list_sessions_parallel(home_dir=None, env_name="unknown", limit=50, keyword=""):
    """
    并行版本的通用会话发现：在任何环境目录中查找JSONL文件。
    使用线程池加速I/O密集型文件扫描任务。
    """
    if home_dir is None:
        home_dir = Path.home()
    
    # 查找常见的会话模式
    search_dirs = []
    for pattern in ["sessions", "projects", "memory", "data"]:
        candidate = home_dir / pattern
        if candidate.exists():
            search_dirs.append(candidate)
    
    # 使用快速扫描查找JSONL文件
    jsonl_files = []
    for search_dir in search_dirs:
        jsonl_files.extend(_fast_find_jsonl(search_dir))
    
    # 如果没有找到，回退到扫描主目录
    if not jsonl_files:
        jsonl_files = _fast_find_jsonl(home_dir)
    
    # 按修改时间排序（最新的在前）
    try:
        jsonl_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        # 如果stat()失败，保持原顺序
        pass
    
    # 并行处理所有文件
    sessions = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        # 提交所有文件的扫描任务
        futures = {}
        for jf in jsonl_files:
            future = executor.submit(_scan_single_file, jf, env_name, keyword)
            futures[future] = jf
        
        # 收集结果
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    sessions.append(result)
            except Exception:
                # 静默处理异常，继续处理其他文件
                pass
    
    # 按创建时间排序
    sessions.sort(key=lambda s: str(s.created or ""), reverse=True)
    return sessions[:limit]


def scan_all_environments_parallel():
    """
    并行版本的环境扫描：同时扫描所有已知和未知环境。
    使用线程池加速目录遍历和文件计数。
    """
    results = []
    
    def scan_env(env_id, env_info, is_registered=True):
        """扫描单个环境，返回结果字典。"""
        root = Path(os.path.expanduser(env_info["root"]))
        exists = root.exists()
        session_count = 0
        if exists:
            # 使用快速扫描查找JSONL文件
            jsonl_files = _fast_find_jsonl(root)
            session_count = len(jsonl_files)
        
        return {
            "name": env_info["name"],
            "env_id": env_id,
            "path": str(root),
            "exists": exists,
            "session_count": session_count,
            "format": env_info.get("format", "unknown"),
            "adapter": env_info.get("adapter", "universal"),
            "status": "adapted" if is_registered else ("unadapted" if exists else "missing"),
        }
    
    # 收集所有需要扫描的环境
    env_tasks = []
    
    # 注册的适配器
    for env_id, env_info in ENV_REGISTRY.items():
        env_tasks.append((env_id, env_info, True))
    
    # 已知但未适配的环境
    for env_id, env_info in KNOWN_UNADAPTED.items():
        env_tasks.append((env_id, env_info, False))
    
    # 并行扫描所有注册和已知的环境
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for env_id, env_info, is_registered in env_tasks:
            future = executor.submit(scan_env, env_id, env_info, is_registered)
            futures[future] = env_id
        
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception:
                pass
    
    # 扫描主目录中的未知环境（串行，因为需要遍历目录）
    home = Path.home()
    known_dirs = {e["root"].replace("~", str(home)).split("/")[0]
                  for e in list(ENV_REGISTRY.values()) + list(KNOWN_UNADAPTED.values())}
    known_dirs.add(str(home / ".claude"))
    known_dirs.add(str(home / ".zcode"))
    known_dirs.add(str(home / ".agents"))
    known_dirs.add(str(home / ".config"))
    known_dirs.add(str(home / ".cache"))
    known_dirs.add(str(home / ".npm"))
    known_dirs.add(str(home / ".cargo"))
    known_dirs.add(str(home / ".ssh"))
    known_dirs.add(str(home / ".local"))
    
    # 收集所有需要检查的目录
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
    
    # 并行扫描未知目录
    def scan_unknown_dir(dotdir):
        """扫描未知目录中的JSONL文件。"""
        try:
            jsonl_files = _fast_find_jsonl(dotdir)
            jsonl_count = len(jsonl_files)
            if jsonl_count > 0:
                return {
                    "name": dotdir.name,
                    "env_id": dotdir.name.lstrip("."),
                    "path": str(dotdir),
                    "exists": True,
                    "session_count": jsonl_count,
                    "format": "unknown",
                    "adapter": "universal",
                    "status": "discovered",
                }
        except OSError:
            pass
        return None
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(scan_unknown_dir, d) for d in dotdirs]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception:
                pass
    
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _empty_stats(agent_name):
    """Return the standard stats dict with empty values."""
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


# ═══════════════════════════════════════════════════════════════════════════
# CLI (standalone testing)
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 env-adapters.py <command> [args]")
        print("Commands:")
        print("  scan              Scan all environments")
        print("  stats <path>      Show session stats")
        print("  messages <path>   Extract messages")
        print("  tools <path>      Extract tool calls")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "scan":
        results = scan_all_environments()
        print(f"\n{'Environment':<25} {'Status':<12} {'Sessions':<10} {'Path'}")
        print("-" * 80)
        for r in results:
            status_icon = {"adapted": "✅", "unadapted": "⚠️", "missing": "❌",
                           "discovered": "🔍"}.get(r["status"], "?")
            print(f"{r['name']:<25} {status_icon} {r['session_count']:<10} {r['path']}")

    elif cmd == "stats" and len(sys.argv) > 2:
        path = sys.argv[2]
        stats = universal_session_stats(path)
        for k, v in stats.items():
            print(f"  {k}: {v}")

    elif cmd == "messages" and len(sys.argv) > 2:
        path = sys.argv[2]
        for msg in universal_extract_messages(path, limit=10):
            print(f"\n[{msg['role']}] {msg['timestamp']}")
            print(f"  {msg['text'][:200]}")

    elif cmd == "tools" and len(sys.argv) > 2:
        path = sys.argv[2]
        for tool in universal_extract_tools(path, limit=20):
            print(f"\n  [{tool['status']}] {tool['name']}")
            print(f"    input: {tool['key_input'][:100]}")
            print(f"    output: {tool['result_preview'][:100]}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
