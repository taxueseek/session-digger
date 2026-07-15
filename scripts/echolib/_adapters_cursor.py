"""Cursor adapter — agent-transcripts JSONL + Desktop state.vscdb.

Borrowing discovery/parse patterns from Grok Build's resume-session
``session_reader.py``, trimmed to session-digger's 5-method contract:

  list_sessions / session_stats / extract_messages / extract_tools / session_path

Sources (in list order, de-duplicated by session_id):
  1. ~/.cursor/projects/*/agent-transcripts/<uuid>/<uuid>.jsonl  (CLI/agent)
  2. Desktop state.vscdb composerHeaders                         (IDE composers)
  3. ~/.cursor/chats/<workspace-hash>/<uuid>/store.db|meta.json   (CLI store)

All SQLite opens are read-only. Binary/protobuf blobs are skipped, never
fabricated — matching resume-session's safety stance for analysis use.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from echolib._claude import _normalize_timestamp
from echolib._helpers import CURSOR_DIR, _extract_content_text, _iter_jsonl
from echolib._models import SessionMeta

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>", flags=re.DOTALL
)
_SKIP_ROLES = frozenset({
    "system", "developer", "instruction", "instructions", "preamble",
})


def _cursor_desktop_paths():
    home = Path.home()
    candidates = [
        home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
        home / ".config/Cursor/User/globalStorage/state.vscdb",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Cursor/User/globalStorage/state.vscdb")
    out = []
    for p in candidates:
        if p not in out:
            out.append(p)
    return out


def _open_sqlite_ro(path: Path):
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _cursor_user_text(text: str) -> str | None:
    """Strip Cursor wrappers; prefer <user_query> body (resume-session parity)."""
    if not text:
        return None
    matches = _USER_QUERY_RE.findall(text)
    if matches:
        joined = "\n".join(m.strip() for m in matches if m.strip())
        return joined or None
    stripped = text.lstrip()
    if stripped.startswith((
        "<environment_context",
        "<user_instructions",
        "<system_reminder",
        "<manually_attached_skills",
        "<timestamp",
    )):
        return None
    return text.strip() or None


def _content_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, dict):
        return [content]
    return []


def _message_content(rec: dict):
    msg = rec.get("message")
    if isinstance(msg, dict) and "content" in msg:
        return msg.get("content")
    return rec.get("content")


def _iter_cursor_transcript_files():
    projects = CURSOR_DIR / "projects"
    if not projects.is_dir():
        return
    try:
        project_dirs = list(projects.iterdir())
    except OSError:
        return
    for project_dir in project_dirs:
        if not project_dir.is_dir() or project_dir.is_symlink():
            continue
        transcripts = project_dir / "agent-transcripts"
        if not transcripts.is_dir():
            continue
        try:
            for sid_dir in transcripts.iterdir():
                if not sid_dir.is_dir() or not _UUID_RE.fullmatch(sid_dir.name):
                    continue
                candidate = sid_dir / f"{sid_dir.name}.jsonl"
                if candidate.is_file() and not candidate.is_symlink():
                    yield candidate, sid_dir.name, project_dir.name
        except OSError:
            continue


def _quick_scan_transcript(path: Path):
    user_count = 0
    assistant_count = 0
    tool_count = 0
    first_prompt = ""
    for rec in _iter_jsonl(path):
        if not isinstance(rec, dict):
            continue
        role = (rec.get("role") or "").lower()
        content = _message_content(rec)
        if role == "user":
            text = _extract_content_text(content)
            cleaned = _cursor_user_text(text) if text else None
            if cleaned:
                user_count += 1
                if not first_prompt:
                    first_prompt = cleaned[:200]
        elif role == "assistant":
            text = _extract_content_text(content)
            has_tool = False
            for block in _content_blocks(content):
                if block.get("type") in ("tool_use", "tool_call"):
                    tool_count += 1
                    has_tool = True
            if text or has_tool:
                assistant_count += 1
        elif role == "tool":
            tool_count += 1
    return user_count, assistant_count, tool_count, first_prompt


def cursor_list_sessions(cwd=None, limit=50, keyword=""):
    """List Cursor sessions from agent-transcripts + desktop composers."""
    sessions = []
    seen = set()
    keyword_l = keyword.lower() if keyword else ""

    # 1) Agent transcripts (highest fidelity for digger analysis)
    for path, sid, project in _iter_cursor_transcript_files():
        if sid in seen:
            continue
        try:
            mtime = _normalize_timestamp(path.stat().st_mtime)
        except OSError:
            mtime = ""
        user_n, asst_n, _tool_n, first = _quick_scan_transcript(path)
        summary = first or path.stem
        if keyword_l and keyword_l not in summary.lower() and keyword_l not in sid.lower():
            continue
        seen.add(sid)
        sessions.append(SessionMeta(
            session_id=sid,
            full_path=str(path),
            created=mtime,
            modified=mtime,
            message_count=user_n + asst_n,
            git_branch="",
            summary=summary[:100],
            first_prompt=first[:200] if first else "",
            project_path=project,
        ))

    # 2) Desktop composers not already covered by transcripts
    for db_path in _cursor_desktop_paths():
        if not db_path.is_file() or db_path.is_symlink():
            continue
        try:
            with _open_sqlite_ro(db_path) as db:
                cols = _table_columns(db, "composerHeaders")
                required = {"composerId", "lastUpdatedAt", "value"}
                if not required.issubset(cols):
                    continue
                order = "recency" if "recency" in cols else "lastUpdatedAt"
                archive_filter = (
                    "WHERE COALESCE(isArchived, 0) = 0 AND COALESCE(isSubagent, 0) = 0 "
                    if {"isArchived", "isSubagent"}.issubset(cols) else ""
                )
                rows = db.execute(
                    f"SELECT composerId, lastUpdatedAt, value FROM composerHeaders "
                    f"{archive_filter}ORDER BY {order} DESC, composerId ASC"
                )
                for sid, raw_updated, raw_value in rows:
                    if not isinstance(sid, str) or sid in seen:
                        continue
                    if not (_UUID_RE.fullmatch(sid) or sid):
                        continue
                    title = ""
                    if isinstance(raw_value, (bytes, memoryview)):
                        try:
                            raw_value = bytes(raw_value).decode("utf-8", errors="replace")
                        except Exception:
                            raw_value = ""
                    if isinstance(raw_value, str) and raw_value.strip():
                        try:
                            meta = json.loads(raw_value)
                            if isinstance(meta, dict):
                                title = meta.get("name") or meta.get("title") or ""
                        except (json.JSONDecodeError, ValueError):
                            pass
                    if keyword_l and keyword_l not in (title or "").lower() and keyword_l not in sid.lower():
                        continue
                    transcript = _find_cursor_transcript(sid)
                    full_path = str(transcript) if transcript else f"{db_path}#composer:{sid}"
                    mtime = _normalize_timestamp(raw_updated) if raw_updated else ""
                    seen.add(sid)
                    sessions.append(SessionMeta(
                        session_id=sid,
                        full_path=full_path,
                        created=mtime,
                        modified=mtime,
                        message_count=0,
                        git_branch="",
                        summary=(title or sid)[:100],
                        first_prompt=(title or "")[:200],
                        project_path="cursor-desktop",
                    ))
        except (OSError, sqlite3.Error):
            continue

    sessions.sort(key=lambda s: str(s.modified or s.created or ""), reverse=True)
    return sessions[:limit]


def _find_cursor_transcript(session_id: str):
    if not session_id:
        return None
    projects = CURSOR_DIR / "projects"
    if not projects.is_dir():
        return None
    try:
        candidates = sorted(
            projects.glob(f"*/agent-transcripts/{session_id}/{session_id}.jsonl"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
    except OSError:
        return None
    for p in candidates:
        if p.is_file() and not p.is_symlink():
            return p
    return None


def cursor_session_path(cwd, session_id=None):
    """Resolve a Cursor session to its best readable path."""
    if session_id:
        t = _find_cursor_transcript(session_id)
        if t:
            return str(t)
        for db in _cursor_desktop_paths():
            if db.is_file():
                return f"{db}#composer:{session_id}"
    # newest transcript
    latest = None
    latest_mtime = -1
    for path, _sid, _proj in _iter_cursor_transcript_files():
        try:
            m = path.stat().st_mtime
        except OSError:
            continue
        if m > latest_mtime:
            latest_mtime = m
            latest = path
    return str(latest) if latest else None


def _resolve_cursor_path(session_path):
    """Return (kind, path_or_db, session_id). kind in {jsonl, desktop, none}."""
    raw = str(session_path or "")
    if "#composer:" in raw:
        db_s, sid = raw.split("#composer:", 1)
        return "desktop", Path(db_s), sid
    p = Path(raw)
    if p.is_file() and p.suffix == ".jsonl":
        return "jsonl", p, p.stem
    if p.is_file() and p.name in ("store.db", "state.vscdb"):
        return "desktop", p, p.parent.name if p.name == "store.db" else ""
    # bare uuid
    if _UUID_RE.fullmatch(raw):
        t = _find_cursor_transcript(raw)
        if t:
            return "jsonl", t, raw
    return "none", p, ""


def cursor_session_stats(session_path):
    from echolib._adapters import _empty_stats
    stats = _empty_stats("cursor")
    kind, path, sid = _resolve_cursor_path(session_path)
    stats["slug"] = sid or path.stem
    stats["model"] = "cursor"
    if kind == "jsonl" and path.is_file():
        user_n, asst_n, tool_n, first = _quick_scan_transcript(path)
        stats["user_messages"] = user_n
        stats["assistant_messages"] = asst_n
        stats["tool_calls"] = tool_n
        stats["summary"] = (first or "")[:200]
        try:
            mtime = _normalize_timestamp(path.stat().st_mtime)
            stats["started"] = mtime
            stats["ended"] = mtime
        except OSError:
            pass
        return stats
    if kind == "desktop" and path.is_file() and sid:
        # Lightweight: presence only; bubble decode is best-effort in extract_*
        stats["summary"] = f"cursor-desktop:{sid[:8]}"
        return stats
    return stats


def cursor_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    kind, path, sid = _resolve_cursor_path(session_path)
    count = 0
    if kind == "jsonl" and path.is_file():
        for rec in _iter_jsonl(path):
            if not isinstance(rec, dict):
                continue
            r = (rec.get("role") or "").lower()
            if r in _SKIP_ROLES or not r:
                continue
            if role == "user" and r != "user":
                continue
            if role == "assistant" and r not in ("assistant", "tool"):
                continue
            content = _message_content(rec)
            text = _extract_content_text(content)
            if r == "user":
                text = _cursor_user_text(text) or ""
            if not text:
                # tool-only assistant turns still surface as empty text — skip
                if r == "assistant":
                    tools = [
                        b.get("name") or "tool"
                        for b in _content_blocks(content)
                        if b.get("type") in ("tool_use", "tool_call")
                    ]
                    if tools:
                        text = f"[tools: {', '.join(tools[:5])}]"
                    else:
                        continue
                else:
                    continue
            out_role = "USER" if r == "user" else "ASSISTANT"
            yield {"role": out_role, "timestamp": "", "text": text[:500]}
            count += 1
            if limit and count >= limit:
                return
        return

    if kind == "desktop" and path.is_file() and sid:
        for text, r in _iter_desktop_bubbles(path, sid):
            if role == "user" and r != "user":
                continue
            if role == "assistant" and r != "assistant":
                continue
            yield {"role": "USER" if r == "user" else "ASSISTANT", "timestamp": "", "text": text[:500]}
            count += 1
            if limit and count >= limit:
                return


def cursor_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    kind, path, sid = _resolve_cursor_path(session_path)
    count = 0
    if kind != "jsonl" or not path.is_file():
        return
    for rec in _iter_jsonl(path):
        if not isinstance(rec, dict):
            continue
        content = _message_content(rec)
        for block in _content_blocks(content):
            btype = block.get("type")
            if btype not in ("tool_use", "tool_call"):
                continue
            name = block.get("name") or "unknown"
            if tool_filter and name != tool_filter:
                continue
            if errors_only:
                # transcript tool_use rarely carries is_error; skip when filtering errors
                continue
            yield {
                "timestamp": "",
                "name": name,
                "status": "ok",
                "key_input": str(block.get("input") or block.get("arguments") or "")[:150],
                "result_preview": "",
            }
            count += 1
            if limit and count >= limit:
                return


def _decode_jsonish(raw):
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(raw, str):
        text = raw
    else:
        return raw if isinstance(raw, (dict, list)) else None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _iter_desktop_bubbles(db_path: Path, session_id: str):
    """Yield (text, role) from cursorDiskKV bubbles when available."""
    try:
        with _open_sqlite_ro(db_path) as db:
            cols = _table_columns(db, "cursorDiskKV")
            if not {"key", "value"}.issubset(cols):
                return
            try:
                rows = db.execute(
                    "SELECT key, value FROM cursorDiskKV "
                    "WHERE key = ? OR key LIKE ? ORDER BY key",
                    (f"composerData:{session_id}", f"bubbleId:{session_id}:%"),
                )
            except sqlite3.Error:
                return
            for _key, raw in rows:
                value = _decode_jsonish(raw)
                if value is None:
                    continue
                yield from _walk_role_texts(value)
    except (OSError, sqlite3.Error):
        return


def _walk_role_texts(value, depth=0):
    if depth > 8 or value is None:
        return
    if isinstance(value, dict):
        role = value.get("role")
        if isinstance(role, str):
            r = role.lower()
            if r not in _SKIP_ROLES and r in ("user", "assistant", "tool"):
                content = value.get("content")
                if isinstance(value.get("message"), dict):
                    content = value["message"].get("content", content)
                text = _extract_content_text(content)
                if r == "user":
                    text = _cursor_user_text(text or "") or ""
                if text:
                    yield text, ("assistant" if r == "tool" else r)
        for nested in value.values():
            yield from _walk_role_texts(nested, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_role_texts(item, depth + 1)
