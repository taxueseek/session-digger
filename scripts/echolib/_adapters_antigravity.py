"""Antigravity (Google / Gemini) adapter — transcripts, token accounting, cache hits.

Storage layout:
    ~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript.jsonl
    ~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript_full.jsonl
    ~/.gemini/antigravity-cli/conversation_summaries.db
Also supports ~/.gemini/antigravity/brain and ~/.gemini/antigravity-ide/brain.

Antigravity transcript format:
- Lines are JSON records with step_index, source, type, status, created_at, content.
- USER_INPUT: source=USER_EXPLICIT / USER, content contains <USER_REQUEST>...</USER_REQUEST>
  and <USER_SETTINGS_CHANGE> tags.
- PLANNER_RESPONSE: source=MODEL, contains input_tokens, cache_read_tokens,
  output_tokens, content, thinking, tool_calls.
- GENERIC (tool outputs): source=MODEL, status=DONE/ERROR, tool execution results.

Token Policy:
- input_includes_cache = False (input_tokens is non-cached prompt tokens).
- cache_read_tokens is prompt cache hit tokens.
- cache_hit_rate = cache_read_tokens / (input_tokens + cache_read_tokens).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from echolib._helpers import (
    ANTIGRAVITY_DIR,
    _iter_jsonl,
    attach_cache_hit_rates,
    cap,
    discovery_only_active,
)
from echolib._models import normalize_model_name

_MODEL_CHANGE_RE = re.compile(
    r"setting\s+[`'\"]?Model Selection[`'\"]?\s+from\s+.*?to\s+[`'\"]?([^<\n]+?)(?:\.\s*<|\.\s*$|[`'\"]?\s*<|[`'\"]?\s*$)",
    re.IGNORECASE,
)
_USER_TAG_RE = re.compile(r"<\/?(?:USER_REQUEST|USER_SETTINGS_CHANGE|ADDITIONAL_METADATA)[^>]*>", re.IGNORECASE)


def _antigravity_brain_dirs(root: str | Path | None = None) -> list[Path]:
    """Candidate brain directories in priority order."""
    if root:
        p = Path(root).expanduser()
        return [p] if p.is_dir() else []
    dirs = [
        ANTIGRAVITY_DIR,
        Path.home() / ".gemini" / "antigravity" / "brain",
        Path.home() / ".gemini" / "antigravity-ide" / "brain",
    ]
    return [d for d in dirs if d.is_dir()]


_SUMMARIES_CACHE: dict[str, tuple[float, dict[str, dict]]] = {}


def _get_conversation_summaries() -> dict[str, dict]:
    """Read conversation_summaries.db across ~/.gemini/* if available."""
    db_candidates = [
        Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db",
        Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db",
        Path.home() / ".gemini" / "antigravity-ide" / "conversation_summaries.db",
    ]
    out: dict[str, dict] = {}
    for db_path in db_candidates:
        if not db_path.is_file():
            continue
        try:
            mtime = db_path.stat().st_mtime
        except OSError:
            continue
        cached = _SUMMARIES_CACHE.get(str(db_path))
        if cached and cached[0] == mtime:
            out.update(cached[1])
            continue
        local_map: dict[str, dict] = {}
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                for row in conn.execute(
                    "SELECT conversation_id, title, preview, parent_conversation_id, "
                    "nesting_depth, workspace_uris, last_modified_time FROM conversation_summaries"
                ):
                    local_map[str(row[0])] = {
                        "title": row[1] or "",
                        "preview": row[2] or "",
                        "parent_id": row[3] or "",
                        "nesting_depth": int(row[4] or 0),
                        "workspaces": row[5] or "",
                        "modified": row[6] or "",
                    }
            finally:
                conn.close()
            _SUMMARIES_CACHE[str(db_path)] = (mtime, local_map)
            out.update(local_map)
        except sqlite3.Error:
            pass
    return out


def _get_default_model() -> str:
    """Read configured default model from settings.json."""
    settings_file = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    if settings_file.is_file():
        try:
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("model"):
                return str(data["model"]).strip()
        except Exception:
            pass
    return "Gemini 3.8 Flash (Medium)"


def antigravity_session_path(session_id: str, root: str | Path | None = None) -> Path | None:
    """Locate transcript.jsonl for a session ID."""
    sid = str(session_id).replace("antigravity:", "", 1)
    for bdir in _antigravity_brain_dirs(root):
        cand = bdir / sid / ".system_generated" / "logs" / "transcript.jsonl"
        if cand.is_file():
            return cand
    return None


def antigravity_list_sessions(
    cwd: str | None = None,
    limit: int | None = None,
    keyword: str = "",
    root: str | Path | None = None,
    project: str | None = None,
    **kwargs,
) -> list[dict]:
    """List Antigravity sessions across brain directories."""
    summaries = _get_conversation_summaries()
    sessions = []
    seen_ids: set[str] = set()

    is_discovery = discovery_only_active()
    kw_lower = (keyword or "").lower()

    for bdir in _antigravity_brain_dirs(root):
        try:
            entries = list(bdir.iterdir())
        except OSError:
            continue
        for sdir in entries:
            if not sdir.is_dir():
                continue
            tpath = sdir / ".system_generated" / "logs" / "transcript.jsonl"
            if not tpath.is_file():
                continue
            sid = sdir.name
            if sid in seen_ids:
                continue
            seen_ids.add(sid)

            db_info = summaries.get(sid, {})
            title = db_info.get("title") or db_info.get("preview") or ""
            role = "subagent" if db_info.get("parent_id") or db_info.get("nesting_depth", 0) > 0 else "main"

            # Project derivation
            workspaces = db_info.get("workspaces") or ""
            proj = ""
            if workspaces:
                try:
                    uris = json.loads(workspaces)
                    if uris and isinstance(uris, list):
                        p_str = uris[0].replace("file://", "").rstrip("/")
                        proj = os.path.basename(p_str)
                except Exception:
                    pass

            if cwd and proj and cwd.lower() not in proj.lower():
                continue
            if project and proj and project.lower() not in proj.lower():
                continue

            try:
                mtime_ts = tpath.stat().st_mtime
                mod_iso = datetime.fromtimestamp(mtime_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            except OSError:
                mod_iso = ""

            model = ""
            first_prompt = ""
            msgs_count = 0

            if not is_discovery:
                # Fast head scan for first prompt & model
                try:
                    with open(tpath, encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f):
                            if i > 25 and first_prompt and model:
                                break
                            try:
                                d = json.loads(line)
                            except Exception:
                                continue
                            t = d.get("type")
                            src = d.get("source")
                            if t == "USER_INPUT" or src in ("USER_EXPLICIT", "USER"):
                                msgs_count += 1
                                if not first_prompt:
                                    cnt = d.get("content") or ""
                                    clean = _USER_TAG_RE.sub("", cnt).strip()
                                    first_prompt = clean[:200]
                            elif t == "PLANNER_RESPONSE" or src == "MODEL":
                                msgs_count += 1
                            if not model:
                                cnt = d.get("content") or ""
                                m = _MODEL_CHANGE_RE.search(cnt)
                                if m:
                                    model = m.group(1).strip()
                except OSError:
                    pass

            if not model:
                model = _get_default_model()

            haystack = f"{title} {first_prompt} {sid}".lower()
            if kw_lower and kw_lower not in haystack:
                continue

            sessions.append({
                "id": sid,
                "session_id": sid,
                "title": title or first_prompt or sid[:8],
                "summary": title or first_prompt,
                "first_prompt": first_prompt,
                "created": db_info.get("modified") or mod_iso,
                "modified": mod_iso,
                "message_count": msgs_count or db_info.get("step_count", 0),
                "path": str(tpath),
                "full_path": str(tpath),
                "agent": "antigravity",
                "model": normalize_model_name(model),
                "project": proj,
                "role": role,
            })

    # Sort newest first
    sessions.sort(key=lambda s: s.get("modified") or "", reverse=True)
    return cap(sessions, limit)


def antigravity_session_stats(session_path: str | Path) -> dict:
    """Extract standard SessionStats from an Antigravity transcript."""
    from echolib._adapters import _empty_stats

    p = Path(session_path)
    stats = _empty_stats("antigravity")
    sid = p.parent.parent.parent.name if ".system_generated" in str(p) else p.parent.name
    stats["slug"] = sid
    if not p.is_file():
        return stats

    summaries = _get_conversation_summaries()
    db_info = summaries.get(sid, {})

    title = db_info.get("title") or db_info.get("preview") or ""
    workspaces = db_info.get("workspaces") or ""
    if workspaces:
        try:
            uris = json.loads(workspaces)
            if uris and isinstance(uris, list):
                stats["project"] = os.path.basename(uris[0].replace("file://", "").rstrip("/"))
        except Exception:
            pass

    saw_usage = False
    detected_model = ""
    first_prompt = ""

    for d in _iter_jsonl(p):
        if not isinstance(d, dict):
            continue
        t = d.get("type", "")
        src = d.get("source", "")
        ts = d.get("created_at") or ""
        if ts:
            if not stats["started"] or ts < stats["started"]:
                stats["started"] = ts
            if not stats["ended"] or ts > stats["ended"]:
                stats["ended"] = ts

        content = d.get("content") or ""
        if not detected_model:
            m = _MODEL_CHANGE_RE.search(content)
            if m:
                detected_model = m.group(1).strip()

        if t == "USER_INPUT" or (src in ("USER_EXPLICIT", "USER") and t != "GENERIC"):
            clean = _USER_TAG_RE.sub("", content).strip()
            if clean:
                stats["user_messages"] += 1
                if not first_prompt:
                    first_prompt = clean

        elif t == "PLANNER_RESPONSE":
            stats["assistant_messages"] += 1
            if "input_tokens" in d or "output_tokens" in d:
                saw_usage = True
                stats["input_tokens"] += int(d.get("input_tokens") or 0)
                stats["output_tokens"] += int(d.get("output_tokens") or 0)
                stats["cache_read_tokens"] += int(d.get("cache_read_tokens") or 0)

            tc = d.get("tool_calls")
            if tc and isinstance(tc, list):
                stats["tool_calls"] += len(tc)
                for call in tc:
                    if isinstance(call, dict):
                        name = call.get("name") or call.get("tool_name") or ""
                        if name in ("edit_file", "write_file"):
                            stats["files_edited"] += 1

        elif t == "GENERIC":
            # Tool result check for errors
            status = d.get("status", "")
            if status == "ERROR":
                stats["errors"] += 1
            elif "Command exited with code " in content:
                m_code = re.search(r"Command exited with code (\d+)", content)
                if m_code and m_code.group(1) != "0":
                    stats["errors"] += 1

    stats["model"] = normalize_model_name(detected_model or _get_default_model())
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"] + stats["cache_read_tokens"]
    if not saw_usage:
        stats["total_tokens"] = 0

    # Antigravity prompt caching policy: input_includes_cache = False
    attach_cache_hit_rates(stats, input_includes_cache=False)
    stats["summary"] = (title or first_prompt)[:200]
    stats["first_prompt"] = first_prompt[:300]
    return stats


def antigravity_extract_messages(
    session_path: str | Path,
    role: str = "both",
    limit: int = 0,
    thinking_limit: int = 0,
) -> Generator[dict, None, None]:
    """Extract messages from Antigravity transcript."""
    p = Path(session_path)
    if not p.is_file():
        return

    count = 0
    for d in _iter_jsonl(p):
        if not isinstance(d, dict):
            continue
        t = d.get("type", "")
        src = d.get("source", "")
        ts = d.get("created_at") or ""

        if (t == "USER_INPUT" or (src in ("USER_EXPLICIT", "USER") and t != "GENERIC")) and role in ("user", "both"):
            cnt = d.get("content") or ""
            clean = _USER_TAG_RE.sub("", cnt).strip()
            if clean:
                yield {"role": "USER", "timestamp": ts, "text": clean}
                count += 1
                if limit and count >= limit:
                    return

        elif t == "PLANNER_RESPONSE" and role in ("assistant", "both"):
            cnt = d.get("content") or ""
            thinking = d.get("thinking") or ""
            if not cnt and not thinking:
                continue
            text = cnt
            if thinking_limit != -1 and thinking:
                if thinking_limit > 0:
                    thinking = thinking[:thinking_limit]
                text = f"[THINKING] {thinking}\n{text}".strip()
            if text:
                yield {"role": "ASSISTANT", "timestamp": ts, "text": text}
                count += 1
                if limit and count >= limit:
                    return


def antigravity_extract_tools(
    session_path: str | Path,
    tool_filter: str = "",
    errors_only: bool = False,
    limit: int = 0,
) -> Generator[dict, None, None]:
    """Extract tool calls and outcomes from Antigravity transcript."""
    p = Path(session_path)
    if not p.is_file():
        return

    count = 0
    records = list(_iter_jsonl(p))
    n = len(records)

    for i, d in enumerate(records):
        if not isinstance(d, dict):
            continue
        if d.get("type") != "PLANNER_RESPONSE" and d.get("source") != "MODEL":
            continue
        tc_list = d.get("tool_calls")
        if not tc_list or not isinstance(tc_list, list):
            continue
        ts = d.get("created_at") or ""

        for j, call in enumerate(tc_list):
            if not isinstance(call, dict):
                continue
            name = call.get("name") or call.get("tool_name") or ""
            if tool_filter and tool_filter.lower() not in name.lower():
                continue

            args = call.get("args") or {}
            key_input = ""
            if isinstance(args, dict):
                key_input = args.get("CommandLine") or args.get("Path") or args.get("query") or str(args)[:120]
            else:
                key_input = str(args)[:120]

            # Correlate with result if next record is GENERIC tool output
            result_preview = ""
            status = "success"
            if i + 1 + j < n:
                res_rec = records[i + 1 + j]
                if isinstance(res_rec, dict) and res_rec.get("type") == "GENERIC":
                    cnt = res_rec.get("content") or ""
                    result_preview = cnt[:200]
                    if res_rec.get("status") == "ERROR" or "Command exited with code " in cnt:
                        m_c = re.search(r"Command exited with code (\d+)", cnt)
                        if m_c and m_c.group(1) != "0":
                            status = "error"
                    if "error" in cnt.lower() and status != "error":
                        status = "error"

            if errors_only and status != "error":
                continue

            yield {
                "name": name,
                "timestamp": ts,
                "key_input": str(key_input),
                "status": status,
                "result_preview": result_preview,
                "duration": 0.0,
            }
            count += 1
            if limit and count >= limit:
                return
