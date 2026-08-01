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
from pathlib import Path

from echolib._adapters_grok import (
    _grok_extract_messages,
    _grok_session_stats,
    _grok_find_session_dir,
)
from echolib._claude import _normalize_timestamp
from echolib._helpers import compute_cache_hit_rate

# Kimix uses ~/.kimix as its home directory
KIMIX_DIR = Path(os.path.expanduser("~/.kimix"))


def _kimix_home():
    """Return the Kimix home directory."""
    return KIMIX_DIR


# ── Kimix 0.1.16 缓存指标数据源 ─────────────────────────────────────────
# 两个每请求级缓存数据源：
#   1) ~/.kimix/metrics/cache_hit-*.jsonl  — 按日文件，无会话归属
#   2) ~/.kimix/logs/unified.jsonl          — shell.turn.inference_done，带 sid
# 它们补充 updates.jsonl 累积快照（run 级）无法覆盖的请求级精度。

_UNIFIED_CACHE = {"mtime": None, "data": None}


def kimix_unified_cache_index(force=False):
    """Load per-session cache data from ~/.kimix/logs/unified.jsonl.

    Returns {sid: {"requests", "prompt_tokens", "cached_prompt_tokens",
                   "completion_tokens", "reasoning_tokens"}}.
    Module-level mtime cache: re-parses only when the file changed.
    """
    log_file = KIMIX_DIR / "logs" / "unified.jsonl"
    try:
        mtime = log_file.stat().st_mtime
    except OSError:
        return {}
    if not force and _UNIFIED_CACHE["mtime"] == mtime and _UNIFIED_CACHE["data"] is not None:
        return _UNIFIED_CACHE["data"]
    out = {}
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "inference_done" not in line:
                    continue
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if d.get("msg") != "shell.turn.inference_done":
                    continue
                sid = d.get("sid")
                if not sid:
                    continue
                ctx = d.get("ctx") or {}
                bucket = out.setdefault(sid, {
                    "requests": 0, "prompt_tokens": 0,
                    "cached_prompt_tokens": 0, "completion_tokens": 0,
                    "reasoning_tokens": 0,
                })
                bucket["requests"] += 1
                bucket["prompt_tokens"] += ctx.get("prompt_tokens") or 0
                bucket["cached_prompt_tokens"] += ctx.get("cached_prompt_tokens") or 0
                bucket["completion_tokens"] += ctx.get("completion_tokens") or 0
                bucket["reasoning_tokens"] += ctx.get("reasoning_tokens") or 0
    except OSError:
        return {}
    _UNIFIED_CACHE["mtime"] = mtime
    _UNIFIED_CACHE["data"] = out
    return out


def _empty_metrics_agg():
    return {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0,
            "weighted_hit_rate": None}


def kimix_cache_metrics(days=None):
    """Aggregate ~/.kimix/metrics/cache_hit-*.jsonl (per-request cache data).

    Files are named ``cache_hit-YYYY-MM-DD.jsonl``. With *days*, only the
    most recent N calendar days are included. Returns
    ``{"by_date": {date: agg}, "total": agg}`` where each agg has
    requests / prompt_tokens / cached_tokens / weighted_hit_rate.
    """
    metrics_dir = KIMIX_DIR / "metrics"
    if not metrics_dir.is_dir():
        return {"by_date": {}, "total": _empty_metrics_agg()}
    files = sorted(metrics_dir.glob("cache_hit-*.jsonl"))
    if days and days > 0:
        import datetime as _dt
        cutoff = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
        files = [f for f in files
                 if f.name[len("cache_hit-"):-len(".jsonl")] >= cutoff]
    by_date = {}
    totals = _empty_metrics_agg()
    for fp in files:
        date = fp.name[len("cache_hit-"):-len(".jsonl")]
        agg = _empty_metrics_agg()
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"cache_hit"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if rec.get("type") != "cache_hit":
                        continue
                    pt = rec.get("prompt_tokens") or 0
                    ct = rec.get("cached_tokens") or 0
                    agg["requests"] += 1
                    agg["prompt_tokens"] += pt
                    agg["cached_tokens"] += ct
        except OSError:
            continue
        if agg["prompt_tokens"]:
            agg["weighted_hit_rate"] = agg["cached_tokens"] / agg["prompt_tokens"]
        by_date[date] = agg
        for key in ("requests", "prompt_tokens", "cached_tokens"):
            totals[key] += agg[key]
    if totals["prompt_tokens"]:
        totals["weighted_hit_rate"] = totals["cached_tokens"] / totals["prompt_tokens"]
    return {"by_date": by_date, "total": totals}


def kimix_unified_cache_summary(days=None):
    """Aggregate per-session unified.jsonl cache data into env totals.

    Returns ``{"by_sid": {...}, "total": agg, "sessions": n}``; *total* uses
    the same shape as ``kimix_cache_metrics`` totals (prompt/cached tokens).
    """
    index = kimix_unified_cache_index()
    if not index:
        return {"by_sid": {}, "total": _empty_metrics_agg(), "sessions": 0}
    total = _empty_metrics_agg()
    for sid, bucket in index.items():
        if bucket["prompt_tokens"] <= 0:
            continue
        total["requests"] += bucket["requests"]
        total["prompt_tokens"] += bucket["prompt_tokens"]
        total["cached_tokens"] += bucket["cached_prompt_tokens"]
    if total["prompt_tokens"]:
        total["weighted_hit_rate"] = total["cached_tokens"] / total["prompt_tokens"]
    return {"by_sid": index, "total": total, "sessions": len(index)}


def kimix_list_sessions(cwd=None, limit=50, keyword=""):
    """List Kimix sessions.

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
    # Resolve path to session directory
    p = Path(path)

    # Use Grok's session stats function
    stats = _grok_session_stats(path)

    # Override agent name
    stats["agent"] = "kimix"

    # 0.1.16 补充：updates.jsonl 无 usage 时，用 unified.jsonl 的每请求精确
    # 缓存数据填充（带 sid 会话归属），并打标数据来源便于审计。
    stats.setdefault("cache_source", "updates" if stats.get("total_tokens") else "none")
    if not stats.get("total_tokens"):
        # _grok_session_stats 的 slug 是 resolved.stem（chat_history），
        # 会话 uuid 在目录名上。
        sid = p.name if p.is_dir() else p.parent.name
        entry = kimix_unified_cache_index().get(sid)
        if entry and entry["prompt_tokens"]:
            stats["input_tokens"] = entry["prompt_tokens"]
            stats["cache_read_tokens"] = entry["cached_prompt_tokens"]
            stats["output_tokens"] = entry["completion_tokens"]
            stats["total_tokens"] = entry["prompt_tokens"] + entry["completion_tokens"]
            stats["cache_hit_rate"] = compute_cache_hit_rate(
                entry["prompt_tokens"], entry["cached_prompt_tokens"],
                input_includes_cache=True,
            )
            stats["cache_source"] = "unified"
            stats["cache_requests"] = entry["requests"]

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
