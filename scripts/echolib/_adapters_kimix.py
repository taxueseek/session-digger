"""Kimix CLI adapter — family usage, billable tokens, session stats.

Kimix is an unofficial Kimi Code CLI community build based on Grok Build.
The session format is nearly identical to Grok's, so this adapter
delegates to the Grok adapter functions with minimal overrides.

Public surface re-exported by ``echolib._adapters`` / ``echolib``:
  kimix_list_sessions / kimix_session_stats / kimix_extract_messages /
  kimix_extract_tools / kimix_session_path
"""
from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

from echolib._adapters_grok import (
    _grok_resolve_path,
    _grok_extract_messages,
    _grok_session_stats,
    _grok_apply_signals,
    _grok_apply_usage_from_updates,
    _grok_find_session_dir,
    _grok_session_token_profile,
    _grok_aggregate_billable_usage,
    _grok_iter_usage_snapshots,
)
from echolib._claude import _normalize_timestamp
from echolib._helpers import _empty_stats, attach_cache_hit_rates

# Kigi uses ~/.kigi as its home directory
KIMIX_DIR = Path(os.path.expanduser("~/.kigi"))


def _kimix_home():
    """Return the Kimix home directory."""
    return KIMIX_DIR


def kimix_list_sessions(cwd=None, limit=50, keyword=""):
    """List Kigi sessions.

    Returns list of SessionMeta (reusing the existing class).
    """
    if not KIMIX_DIR.exists():
        return []

    sessions_dir = KIMIX_DIR / "sessions"
    if not sessions_dir.exists():
        return []

    from echolib._models import SessionMeta

    entries = []
    # Scan all group directories
    for group_dir in sessions_dir.iterdir():
        if not group_dir.is_dir() or group_dir.name.startswith("."):
            continue

        # Each session is a subdirectory under the group
        for session_dir in group_dir.iterdir():
            if not session_dir.is_dir():
                continue

            summary_file = session_dir / "summary.json"
            if not summary_file.exists():
                continue

            try:
                with open(summary_file, encoding="utf-8") as f:
                    summary = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            info = summary.get("info", {}) if isinstance(summary.get("info"), dict) else {}
            session_cwd = info.get("cwd", "")
            session_id = info.get("id", session_dir.name)

            # Filter by cwd if specified
            if cwd and session_cwd != cwd:
                continue

            # Keyword filter
            if keyword:
                title = summary.get("session_summary", "") or summary.get("generated_title", "")
                if keyword.lower() not in title.lower() and keyword.lower() not in session_id.lower():
                    continue

            created = summary.get("created_at", "")
            updated = summary.get("updated_at") or summary.get("last_active_at") or ""
            model = summary.get("current_model_id", "")

            meta = SessionMeta(
                session_id=session_id,
                full_path=str(session_dir),
                created=_normalize_timestamp(created) if created else "",
                modified=_normalize_timestamp(updated) if updated else "",
                message_count=0,
                git_branch="",
                summary=(summary.get("session_summary") or summary.get("generated_title") or "")[:100],
                first_prompt="",
                project_path=session_cwd,
            )
            entries.append(meta)

    # Sort by modified descending
    entries.sort(key=lambda e: e.modified or "", reverse=True)
    return entries[:limit]


def kimix_session_stats(path):
    """Dedicated stats for Kimix sessions.

    Reuses the Grok adapter since the format is nearly identical.
    The only difference is the home directory path.
    """
    from echolib._helpers import _empty_stats

    # Resolve path to session directory
    p = Path(path)
    if p.is_dir():
        session_dir = p
    else:
        session_dir = p.parent

    # Use Grok's session stats function
    stats = _grok_session_stats(path)

    # Override agent name
    stats["agent"] = "kimix"

    return stats


def kimix_extract_messages(path, role="both", limit=0, thinking_limit=0):
    """Extract messages from Kimix sessions.

    Reuses the Grok adapter since chat_history.jsonl format is identical.
    """
    return _grok_extract_messages(path, role=role, limit=limit, thinking_limit=thinking_limit)


def kimix_extract_tools(path):
    """Extract tool usage from Kimix sessions.

    Reuses the Grok adapter since events.jsonl format is identical.
    """
    from echolib._claude import extract_tools
    # Delegate to Grok's tool extraction
    return extract_tools(path, agent="kimix")


def kimix_session_path(session_id):
    """Resolve a session_id to its full path."""
    return _grok_find_session_dir(session_id)
