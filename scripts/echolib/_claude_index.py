"""Index-side helpers for the Clode Code adapter — discovery + scanning + indexing.

Extracted from ``_claude.py`` so that the session parser (``_claude.py``) and the
project/index discovery logic live in their own modules.  This module owns:
* Project / session discovery (``all_project_dirs``, ``find_project_dir``, ``list_sessions``)
* Index file I/O (``load_index``, ``build_fallback_index``)
* Claude-specific normalisations (``_encode_project_path``, ``_sanitize_tsv``,
  ``_fast_find_jsonl``, ``detect_agent_type``)

Importing this module must NOT create import cycles: it depends only on
``_helpers`` + ``_models`` (already leaf-level packages).
"""
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

from echolib._helpers import (
    CLAUDE_DIR,
    _iter_jsonl,
    _strip_system_reminder,
)
from echolib._models import SessionMeta


def _sanitize_tsv(s, max_len=0):
    """Sanitise a string for safe embedding in TSV output."""
    if not s:
        return ""
    s = s.replace("\t", " ").replace("\n", " ").replace("\r", " ")
    if max_len and len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _fast_find_jsonl(directory, max_depth=4, max_files=5000):
    """Fast recursive .jsonl finder via os.scandir.

    Returns a **list** (not a generator) so callers can safely use ``len()``.
    Caps depth/count to keep env scans responsive on large trees.

    One-line class of bugs this kills: every scan path that did
    ``len(_fast_find_jsonl(...))`` after the generator refactor.
    """
    results = []
    # stack of (path, depth)
    stack = [(str(directory), 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                            results.append(entry)
                            if len(results) >= max_files:
                                return results
                        elif (
                            entry.is_dir(follow_symlinks=False)
                            and depth < max_depth
                            and not entry.name.startswith(".")
                            and entry.name not in ("node_modules", "__pycache__", ".git")
                        ):
                            stack.append((entry.path, depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return results


def _encode_project_path(project_path):
    """Encode a Claude-style project path (``-Users-name-...``) → Path."""
    if not project_path:
        return None
    s = project_path
    s = s.replace("-", "/", 1) if s.startswith("-") else s
    return Path(s)


def find_project_dir(target):
    """Resolve a Claude session's project directory from a CWD or target hint."""
    if not target:
        return None
    p = _encode_project_path(target)
    if p and p.exists():
        return str(p)
    # Fall back to reverse-find in CLAUDE_DIR
    if not CLAUDE_DIR.exists():
        return None
    encoded = target.replace("/", "-")
    exact = CLAUDE_DIR / encoded
    if exact.exists():
        return str(exact)
    encoded2 = "-" + encoded if not encoded.startswith("-") else encoded
    exact2 = CLAUDE_DIR / encoded2
    if exact2.exists():
        return str(exact2)
    # Search for matching project directory by walking CLAUDE_DIR
    for index_path in CLAUDE_DIR.glob("*/sessions-index.json"):
        if encoded in index_path.parent.name or encoded2 in index_path.parent.name:
            return str(index_path.parent)
    for d in CLAUDE_DIR.iterdir():
        if d.is_dir() and (encoded in d.name or encoded2 in d.name):
            return str(d)
    return None


def resolve_project_root(project_dir):
    """From a project's .claude dir, resolve to the project root.

    Returns None when the target does not exist on disk (so callers can
    distinguish "known nothing" from "valid root").
    """
    if not project_dir:
        return None
    p = Path(project_dir)
    if not p.exists():
        return None
    if (p / ".claude").exists():
        return str(p)
    if p.name == ".claude" and p.parent.exists():
        return str(p.parent)
    return str(p)


def all_project_dirs():
    """List all Claude project directories under ~/.claude/projects/."""
    if not CLAUDE_DIR.exists():
        return []
    return [str(d) for d in CLAUDE_DIR.iterdir() if d.is_dir()]


def load_index(index_path):
    """Load sessions from a sessions-index.json file. Returns list of SessionMeta."""
    if not Path(index_path).exists():
        return []
    data = json.loads(Path(index_path).read_text(encoding="utf-8"))
    result = []
    for e in data:
        if isinstance(e, dict):
            result.append(SessionMeta(**e))
    return result


def _scan_project_dir(project_dir, since="", grep_pat=""):
    """Yield (session_id, jsonl_path, mtime) for every session JSONL in a project dir."""
    p = Path(project_dir)
    if not p.exists():
        return
    for jf in p.glob("*.jsonl"):
        sid = jf.stem
        try:
            mtime = jf.stat().st_mtime
        except OSError:
            continue
        if since and mtime < since:
            continue
        if grep_pat:
            try:
                head = jf.read_text(encoding="utf-8", errors="replace")[:8192]
                if grep_pat.lower() not in head.lower():
                    continue
            except OSError:
                continue
        yield sid, str(jf), mtime


def list_sessions(scope="current", target=None, limit=50, since="", grep_pat=""):
    """Main entry: list Claude sessions in echolib format (SessionMeta list)."""
    if scope == "current":
        project_dir = find_project_dir(target)
        if not project_dir:
            return []
        # Look for sessions-index.json first
        index_path = Path(project_dir) / "sessions-index.json"
        if index_path.exists():
            all_sessions = load_index(index_path)
        else:
            all_sessions = []
            for sid, jf, mtime in _scan_project_dir(project_dir):
                all_sessions.append(SessionMeta(
                    session_id=sid,
                    created=mtime,
                    modified=mtime,
                    full_path=jf,
                    message_count=0,
                    summary="",
                ))
        if grep_pat:
            all_sessions = [s for s in all_sessions
                            if grep_pat.lower() in (s.full_path or "").lower()]
        if since:
            try:
                since_ts = float(since)
                all_sessions = [s for s in all_sessions
                                if float(s.modified or 0) >= since_ts]
            except (ValueError, TypeError):
                pass
        all_sessions.sort(key=lambda s: float(s.modified or 0), reverse=True)
        return all_sessions[:limit]

    # scope = "all" — walk every project
    if scope == "all":
        results = []
        for project_dir in all_project_dirs():
            for sid, jf, mtime in _scan_project_dir(project_dir, since=since):
                sm = SessionMeta(
                    session_id=sid, created=mtime, modified=mtime,
                    full_path=jf, message_count=0, summary="",
                )
                if grep_pat and grep_pat.lower() not in jf.lower():
                    continue
                results.append(sm)
        results.sort(key=lambda s: float(s.modified or 0), reverse=True)
        return results[:limit]

    # scope = "session" — single target file
    if scope == "session":
        if not target:
            return []
        p = Path(target)
        if not p.exists():
            return []
        mtime = p.stat().st_mtime if p.exists() else 0
        return [SessionMeta(
            session_id=p.stem, created=mtime, modified=mtime,
            full_path=str(p), message_count=0, summary="",
        )]

    return []


def broad_list_claude_sessions(limit=50, keyword=""):
    """Broad listing across all Claude projects.  Keyword matches title/summary."""
    all_session = list_sessions(scope="all", limit=limit * 10, grep_pat=keyword)
    return all_session[:limit]


def build_fallback_index(project_dir):
    """Build a sessions-index.json from raw JSONL files (fallback when native index missing)."""
    import time as _time_module
    project_path = Path(project_dir)
    if not project_path.exists() or not project_path.is_dir():
        return None
    entries = []
    seen_sids = set()
    for jf in project_path.glob("*.jsonl"):
        sid = jf.stem
        if sid in seen_sids or sid.startswith("agent-"):
            continue
        seen_sids.add(sid)
        message_count = 0
        user_count = 0
        assistant_count = 0
        first_prompt = ""
        last_ts = 0
        first_ts = 0
        model = ""
        try:
            for rec in _iter_jsonl(jf):
                ts = rec.get("timestamp", "")
                if ts:
                    try:
                        fts = float(ts)
                    except (ValueError, TypeError):
                        fts = 0
                    if not first_ts:
                        first_ts = fts
                    last_ts = max(last_ts, fts)
                rtype = rec.get("type", "")
                if rtype == "summary":
                    continue
                if rtype in ("user", "human"):
                    user_count += 1
                    message_count += 1
                    if not first_prompt and rec.get("content"):
                        first_prompt = (rec.get("content") or "")[:200]
                elif rtype in ("assistant", "model"):
                    assistant_count += 1
                    message_count += 1
                    if not model:
                        msg = rec.get("message", {})
                        model = (msg.get("model", "") or "") if isinstance(msg, dict) else ""
        except OSError:
            continue
        if not message_count:
            continue
        entries.append({
            "session_id": sid,
            "project_path": str(project_path),
            "created": first_ts,
            "modified": last_ts,
            "message_count": message_count,
            "user_messages": user_count,
            "assistant_messages": assistant_count,
            "model": model,
            "full_path": str(jf),
            "first_prompt": first_prompt,
            "summary": "",
            "project_name": project_path.name,
        })
    if not entries:
        return None
    entries.sort(key=lambda e: float(e.get("modified", 0) or 0), reverse=True)
    index_path = project_path / "sessions-index.json"
    try:
        index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError:
        return None
    return str(index_path)


# Path markers ordered most-specific first. One table → path detection for
# every known environment; adding a new env is a single tuple, not a new if.
# Values are callables so CODEX_HOME / install moves stay live at call time.
def _env_path_markers():
    from echolib._helpers import (
        CODEX_DIR,
        CURSOR_DIR,
        DIM_DIR,
        GROK_DIR,
        KIMI_CODE_DIR,
        REASONIX_DIR,
        TRAE_DIR,
        WORKBUDDY_DIR,
        ZCODE_DIR,
        _codex_homes,
    )
    markers = [
        (str(GROK_DIR), "grok"),
        (str(KIMI_CODE_DIR), "kimi_code"),
        (str(WORKBUDDY_DIR), "workbuddy"),
        (str(TRAE_DIR), "trae_cn"),
        (str(ZCODE_DIR), "zcode"),
        (str(DIM_DIR), "dim"),
        (str(REASONIX_DIR), "reasonix"),
        (str(CURSOR_DIR), "cursor"),
        # Claude: projects subtree only (not any file under ~/.claude)
        (str(Path.home() / ".claude" / "projects"), "claude"),
    ]
    for home in _codex_homes():
        markers.insert(2, (str(home), "codex"))
    # Also match default ~/.codex even when CODEX_HOME differs and is missing
    if str(CODEX_DIR) not in {m[0] for m in markers}:
        markers.insert(2, (str(CODEX_DIR), "codex"))
    return markers


def detect_agent_type(path=None):
    """Detect which agent produced a session path.

    Resolution order (high confidence → low):
    1. Path markers (directory the transcript lives under)
    2. Filename cues (Codex rollout-*, Cursor agent-transcripts)
    3. **Structural** content cues only — NEVER substring-match model names
       in free text (user saying "sonnet"/"claude" must not force Claude adapter)

    Returns a registered adapter name or ``"unknown"`` (→ universal).
    """
    if not path:
        return "unknown"
    try:
        p = Path(path).expanduser().resolve()
    except OSError:
        p = Path(path).expanduser()
    ps = str(p)

    # 1) Path-prefix table (O(envs), no file I/O) — highest confidence
    for marker, agent in _env_path_markers():
        if marker and (ps == marker or ps.startswith(marker.rstrip("/") + "/")):
            return agent

    # 2) Filename / structural path cues for relocated transcripts
    name = p.name
    if name.startswith("rollout-") and ".jsonl" in name:
        return "codex"
    if "agent-transcripts" in p.parts:
        return "cursor"
    if name in ("store.db", "state.vscdb", "meta.json") and ".cursor" in ps:
        return "cursor"
    # Kimi Code wire layout
    if name == "wire.jsonl" and "agents" in p.parts:
        return "kimi_code"
    # ZCode transcript layout
    if name == "transcript.jsonl" and any(
        part.startswith("agent_") or part.startswith("sess_") for part in p.parts
    ):
        return "zcode"

    # 3) Content is intentionally NOT used here for model-name substrings.
    # Structural content routing lives in dispatch_resolve_agent →
    # _detect_format_from_content (JSON signatures, not free-text "sonnet").
    return "unknown"
