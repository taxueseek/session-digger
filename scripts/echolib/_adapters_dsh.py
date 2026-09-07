"""DSH (DeepSeek) adapter — zstd-compressed event-stream sessions.

Storage layout (all under ``~/.dsh/sessions/``)::

    <project-slug>/session-<uuid>/session.jsonl.zstd      # v0 store
    <project-slug>/session-<uuid>/session.v2.jsonl.zstd   # v2 store (resumed/newer)

Event stream facts (verified against live stores):
- ``session`` header carries id / createdAt (ms epoch) / cwd / agentPreset.
- ``user/message`` → data.content[] text blocks, data.source.kind == "user".
- ``assistant/message`` → data.message.content[] (text / reasoning / tool-call),
  data.message.source.model, data.usage.{inputTokens,outputTokens,...}.
- ``tool/call`` → data.{callId,name,arguments}; ``tool/result`` carries
  data.message.content[].tool-result with an ``isError`` flag.
- ``session/title`` → data.title; the ``source.kind == "provider"`` variant is
  the LLM-generated title and beats the fallback first-prompt title.
- Streaming chunks (``assistant/chunk``, ``reasoning-chunks``, ...) are replay
  artefacts and are skipped — final events carry the same content.

When both v0 and v2 files exist in one session dir, v2 is the live store
(seeded copy + continuation); v0 is then stale and must NOT be double-counted.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from echolib._helpers import DSH_DIR, _iter_jsonl

# Files inside a session dir that hold the conversation store, in preference
# order (v2 first). Anything else (.bak.*, .lock) is not a store.
_DSH_STORE_FILES = ("session.v2.jsonl.zstd", "session.jsonl.zstd")


def _dsh_ms_to_iso(ms):
    """Millisecond Unix epoch → ISO-8601 Z string ("" when absent/invalid)."""
    if ms in (None, "", 0):
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def _dsh_store_file(session_dir):
    """Pick the live store file for a session dir (v2 preferred, None if absent)."""
    for name in _DSH_STORE_FILES:
        candidate = session_dir / name
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def _dsh_iter_session_dirs(root=None):
    """Yield (session_dir, project_slug) for every DSH session directory."""
    root = Path(root) if root else DSH_DIR
    if not root.is_dir():
        return
    try:
        project_dirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return
    for project_dir in project_dirs:
        try:
            session_dirs = sorted(d for d in project_dir.iterdir() if d.is_dir() and d.name.startswith("session-"))
        except OSError:
            continue
        for session_dir in session_dirs:
            yield session_dir, project_dir.name


def _dsh_text_from_blocks(blocks):
    """Concatenate text blocks ``[{"type":"text","text":...}, ...]``."""
    parts = []
    if isinstance(blocks, str):
        return blocks.strip()
    for block in blocks or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


def _dsh_user_text(data):
    """User text from a user/message event's data payload."""
    return _dsh_text_from_blocks(data.get("content"))


def _dsh_assistant_parts(message):
    """Split an assistant/message data.message into (text, reasoning) parts."""
    text_parts, reasoning_parts = [], []
    content = message.get("content") if isinstance(message, dict) else None
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            text_parts.append(str(block["text"]))
        elif btype == "reasoning" and block.get("text"):
            reasoning_parts.append(str(block["text"]))
    return "\n".join(text_parts).strip(), "\n".join(reasoning_parts).strip()


def dsh_list_sessions(cwd=None, limit=50, keyword=""):
    """List DSH sessions. Pure filesystem walk — no decompression on this path.

    The builder recomputes real stats for changed files; keeping discovery
    decompress-free is what makes incremental builds stay fast.
    """
    sessions = []
    keyword_l = keyword.lower() if keyword else ""
    for session_dir, project_slug in _dsh_iter_session_dirs():
        store = _dsh_store_file(session_dir)
        if store is None:
            continue
        sid = session_dir.name.replace("session-", "", 1) or session_dir.name
        try:
            mtime = _dsh_ms_to_iso(int(session_dir.stat().st_mtime * 1000))
        except OSError:
            mtime = ""
        title = f"DSH {project_slug}/{sid[:8]}"
        if keyword_l and keyword_l not in title.lower():
            continue
        sessions.append({
            "id": sid,
            "title": title,
            "created": mtime,
            "modified": mtime,
            "message_count": 0,
            "path": str(store),
            "agent": "dsh",
            "model": "",
        })
    sessions.sort(key=lambda s: str(s.get("modified") or s.get("created") or ""), reverse=True)
    return sessions[:limit] if limit else sessions


def dsh_session_stats(session_path):
    """Stats for a DSH zstd event store (one decompression pass)."""
    from echolib._adapters import _empty_stats

    p = Path(session_path)
    stats = _empty_stats("dsh")
    stats["slug"] = p.parent.name.replace("session-", "", 1) or p.parent.name
    if not p.exists() or not p.is_file():
        return stats

    cwd = ""
    title_provider = ""
    title_fallback = ""
    first_user = ""
    saw_usage = False
    for rec in _iter_jsonl(p):
        if not isinstance(rec, dict):
            continue
        etype = rec.get("type", "")
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}

        nts = _dsh_ms_to_iso(rec.get("time"))
        if nts:
            if not stats["started"] or nts < stats["started"]:
                stats["started"] = nts
            if not stats["ended"] or nts > stats["ended"]:
                stats["ended"] = nts

        if etype == "session":
            # 头事件字段在事件顶层（无 data 包装）。
            cwd = rec.get("cwd") or data.get("cwd") or cwd
            header_ts = _dsh_ms_to_iso(rec.get("createdAt") or data.get("createdAt"))
            if header_ts and (not stats["started"] or header_ts < stats["started"]):
                stats["started"] = header_ts

        elif etype == "user/message":
            text = _dsh_user_text(data)
            if text:
                stats["user_messages"] += 1
                if not first_user:
                    first_user = text

        elif etype == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            text, _reasoning = _dsh_assistant_parts(message)
            if text or _reasoning:
                stats["assistant_messages"] += 1
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            if usage:
                saw_usage = True
                stats["input_tokens"] += int(usage.get("inputTokens") or 0)
                stats["output_tokens"] += int(usage.get("outputTokens") or 0)
                stats["cache_read_tokens"] += int(usage.get("cacheReadTokens") or 0)
                stats["cache_create_tokens"] += int(usage.get("cacheWriteTokens") or 0)
            if not stats["model"] or stats["model"] == "dsh":
                model = (message.get("source") or {}).get("model") if isinstance(message.get("source"), dict) else ""
                if model:
                    stats["model"] = str(model)

        elif etype == "tool/call":
            stats["tool_calls"] += 1

        elif etype == "tool/result":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            for block in content:
                if isinstance(block, dict) and block.get("isError"):
                    stats["errors"] += 1
                    break

        elif etype == "session/title":
            title = str(data.get("title") or "").strip()
            if title:
                if (data.get("source") or {}).get("kind") == "provider":
                    title_provider = title
                else:
                    title_fallback = title_fallback or title

    if cwd:
        stats["project"] = os.path.basename(cwd.rstrip("/")) or cwd
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    if not saw_usage:
        stats["total_tokens"] = 0
    stats["summary"] = (title_provider or title_fallback or first_user or "")[:200]
    return stats


def dsh_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract user/assistant messages from a DSH event store.

    Assistant text comes from ``assistant/message`` final events (not stream
    chunks). Reasoning blocks are prefixed ``[THINKING]`` (matching the ZCode /
    Codex adapters); ``thinking_limit=-1`` drops them entirely.
    """
    p = Path(session_path)
    if not p.exists() or not p.is_file():
        return
    count = 0
    for rec in _iter_jsonl(p):
        if not isinstance(rec, dict):
            continue
        etype = rec.get("type", "")
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        nts = _dsh_ms_to_iso(rec.get("time"))

        if etype == "user/message" and role in ("user", "both"):
            text = _dsh_user_text(data)
            if text:
                yield {"role": "USER", "timestamp": nts, "text": text}
                count += 1
                if limit and count >= limit:
                    return

        elif etype == "assistant/message" and role in ("assistant", "both"):
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            text, reasoning = _dsh_assistant_parts(message)
            if not text and not reasoning:
                continue
            if thinking_limit != -1 and reasoning:
                if thinking_limit > 0:
                    reasoning = reasoning[:thinking_limit]
                text = f"[THINKING] {reasoning}\n{text}".strip()
            if text:
                yield {"role": "ASSISTANT", "timestamp": nts, "text": text}
                count += 1
                if limit and count >= limit:
                    return


def dsh_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from a DSH event store, joining tool/call ↔ tool/result."""
    p = Path(session_path)
    if not p.exists() or not p.is_file():
        return

    results = {}  # callId → is_error
    for rec in _iter_jsonl(p):
        if not isinstance(rec, dict) or rec.get("type") != "tool/result":
            continue
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        source = message.get("source") if isinstance(message.get("source"), dict) else {}
        call_id = source.get("callId") or ""
        content = message.get("content") if isinstance(message.get("content"), list) else []
        is_error = any(
            isinstance(block, dict) and block.get("isError")
            for block in content
        )
        if call_id:
            results[call_id] = is_error

    count = 0
    for rec in _iter_jsonl(p):
        if not isinstance(rec, dict) or rec.get("type") != "tool/call":
            continue
        data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        name = str(data.get("name") or "")
        if tool_filter and name != tool_filter:
            continue
        call_id = str(data.get("callId") or "")
        is_error = results.get(call_id, False)
        if errors_only and not is_error:
            continue
        args = data.get("arguments", "")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        nts = _dsh_ms_to_iso(rec.get("time"))
        yield {
            "timestamp": nts,
            "name": name or "[tool]",
            "status": "error" if is_error else "ok",
            "key_input": str(args)[:150] if args else "",
            "result_preview": "(error)" if is_error else "",
        }
        count += 1
        if limit and count >= limit:
            return


def dsh_session_path(cwd, session_id=None):
    """Resolve a DSH session store path by uuid (or newest session overall)."""
    if session_id:
        wanted = str(session_id).replace("dsh:", "", 1).replace("session-", "", 1)
        for session_dir, _slug in _dsh_iter_session_dirs():
            if wanted and wanted in session_dir.name:
                store = _dsh_store_file(session_dir)
                if store:
                    return str(store)
    newest = None
    newest_m = -1.0
    for session_dir, _slug in _dsh_iter_session_dirs():
        store = _dsh_store_file(session_dir)
        if not store:
            continue
        try:
            m = store.stat().st_mtime
        except OSError:
            continue
        if m > newest_m:
            newest_m = m
            newest = store
    return str(newest) if newest else str(DSH_DIR)
