"""Grok Build adapter — family usage, billable tokens, session stats.

Extracted from ``_adapters.py`` (family/aggregate + dedicated stats path).

Public surface re-exported by ``echolib._adapters`` / ``echolib``:
  grok_list_subagents / grok_family_usage_report / grok_aggregate_model_usage

``_empty_stats`` and ``grok_list_sessions`` are deferred-imported from
``echolib._adapters`` to keep package import order stable (same pattern as
``_adapters_zcode`` / ``_adapters_workbuddy``).

``_grok_home`` stays in ``_helpers.py``.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from echolib._claude import _normalize_timestamp
from echolib._helpers import (
    GROK_DIR,
    attach_cache_hit_rates,
    compute_cache_hit_rate,
)


def _grok_resolve_path(path):
    """Resolve Grok session dir to chat_history.jsonl file path."""
    p = Path(path)
    if p.is_dir():
        chat = p / "chat_history.jsonl"
        if chat.exists():
            return str(chat)
    return str(p)


def _grok_extract_messages(path, role="both", limit=0, thinking_limit=0):
    """Dedicated message extraction for Grok sessions.

    Grok's chat_history.jsonl has:
    - type=user: content is a string or list of text blocks. Many are
      system-reminder/system context, not real user messages.
    - type=assistant: content is text, tool_calls may be present.
    - type=reasoning: summary field with thinking content.

    We filter user messages to exclude system-reminder, user_info, and
    system-reminder blocks, keeping only real user queries.
    Timestamps are read from summary.json (session-level, not per-message).
    """
    resolved = _grok_resolve_path(path)

    # Get session-level timestamp from summary.json
    session_ts = ""
    summary_file = Path(resolved).parent / "summary.json"
    if summary_file.exists():
        try:
            with open(summary_file, encoding="utf-8") as f:
                summary = json.load(f)
            created = summary.get("created_at", "")
            if created:
                session_ts = str(_normalize_timestamp(created)) if _normalize_timestamp(created) else ""
        except (json.JSONDecodeError, OSError):
            pass

    count = 0
    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                rtype = rec.get("type", "")
                ts = session_ts  # Grok has no per-message timestamp

                if rtype == "user" and role in ("user", "both"):
                    content = rec.get("content", "")
                    text = ""
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ).strip()

                    if not text:
                        continue

                    # Filter out system context messages
                    if text.startswith("<system-reminder>"):
                        continue
                    if text.startswith("<user_info>"):
                        continue
                    # Extract real user query from <user_query> tags
                    query_match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
                    if query_match:
                        text = query_match.group(1).strip()
                    # Skip if still too short or looks like system noise
                    if len(text) < 2:
                        continue

                    yield {"role": "USER", "timestamp": ts, "text": text[:500]}
                    count += 1
                    if limit and count >= limit:
                        return

                elif rtype == "assistant" and role in ("assistant", "both"):
                    content = rec.get("content", "")
                    text = ""
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ).strip()
                    if text:
                        yield {"role": "ASSISTANT", "timestamp": ts, "text": text[:500]}
                        count += 1
                        if limit and count >= limit:
                            return

                elif rtype == "reasoning" and role in ("assistant", "both") and thinking_limit != -1:
                    summary = rec.get("summary", "")
                    if isinstance(summary, str) and summary.strip():
                        text = summary.strip()
                        if thinking_limit > 0:
                            text = text[:thinking_limit]
                        yield {"role": "ASSISTANT", "timestamp": ts, "text": "[THINKING] " + text}
                        count += 1
                        if limit and count >= limit:
                            return
    except OSError:
        pass

def _grok_as_int(value, default=0):
    """Coerce usage counters; never raise on bad vendor payloads."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _grok_apply_signals(session_dir, stats):
    """Fill activity counters from signals.json (not billable tokens).

    signals.json is O(1) and authoritative for tool/message/error counts and
    primaryModelId. Token *billing* lives in updates.jsonl — see
    ``_grok_apply_usage_from_updates``. Returns True if any counter applied.
    """
    signals_file = Path(session_dir) / "signals.json"
    if not signals_file.is_file():
        return False
    try:
        with open(signals_file, encoding="utf-8") as f:
            sig = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(sig, dict):
        return False

    applied = False
    mapping = (
        ("errors", ("toolFailureCount", "errorCount")),
        ("tool_calls", ("toolCallCount",)),
        ("user_messages", ("userMessageCount",)),
        ("assistant_messages", ("assistantMessageCount",)),
        ("compactions", ("compactionCount",)),
        ("files_edited", ("agentFilesTouched", "totalFilesTouched")),
    )
    for dest, keys in mapping:
        val = None
        for k in keys:
            if k in sig and sig[k] is not None:
                val = _grok_as_int(sig[k], default=None)
                if val is not None:
                    break
        if val is not None:
            stats[dest] = val
            applied = True

    model = sig.get("primaryModelId") or ""
    if model and (not stats.get("model") or stats["model"] == "unknown"):
        stats["model"] = model
        applied = True
    return applied


def _grok_parse_model_usage_map(mu):
    """Parse usage.modelUsage → {model: {input,output,cache_read,reasoning,calls}}."""
    out = {}
    if not isinstance(mu, dict):
        return out
    for name, block in mu.items():
        if not isinstance(block, dict):
            continue
        mid = str(name)
        out[mid] = {
            "input": _grok_as_int(block.get("inputTokens")),
            "output": _grok_as_int(block.get("outputTokens")),
            "cache_read": _grok_as_int(block.get("cachedReadTokens")),
            "reasoning": _grok_as_int(block.get("reasoningTokens")),
            "calls": _grok_as_int(block.get("modelCalls")),
        }
    return out


def _grok_parse_usage_object(usage):
    """Normalize one top-level usage dict → snap for run aggregation."""
    if not isinstance(usage, dict):
        return None
    if "inputTokens" not in usage and "outputTokens" not in usage:
        return None
    by_model = _grok_parse_model_usage_map(usage.get("modelUsage"))
    return {
        "input": _grok_as_int(usage.get("inputTokens")),
        "output": _grok_as_int(usage.get("outputTokens")),
        "total": _grok_as_int(usage.get("totalTokens")),
        "cache_read": _grok_as_int(usage.get("cachedReadTokens")),
        "reasoning": _grok_as_int(usage.get("reasoningTokens")),
        "calls": _grok_as_int(usage.get("modelCalls")),
        "turns": _grok_as_int(usage.get("numTurns")),
        "models": list(by_model.keys()),
        "by_model": by_model,
    }


def _grok_iter_usage_snapshots(session_dir):
    """Yield top-level ``params.update.usage`` snapshots (generator API)."""
    updates_file = Path(session_dir) / "updates.jsonl"
    if not updates_file.is_file():
        return
    try:
        with open(updates_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "inputTokens" not in line and "outputTokens" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                params = obj.get("params")
                if not isinstance(params, dict):
                    continue
                update = params.get("update")
                if not isinstance(update, dict):
                    continue
                snap = _grok_parse_usage_object(update.get("usage"))
                if snap is not None:
                    yield snap
    except OSError:
        return


def _grok_flush_run_last(last, totals, per_model, seen_models):
    """Add one run's last snapshot into aggregate totals (mutates args)."""
    if not last:
        return
    totals["input"] += last["input"]
    totals["output"] += last["output"]
    totals["cache_read"] += last["cache_read"]
    totals["reasoning"] += last["reasoning"]
    totals["calls"] += last["calls"]
    legs = last.get("by_model") or {}
    if legs:
        for mid, leg in legs.items():
            if mid not in seen_models:
                seen_models.append(mid)
            bucket = per_model.setdefault(
                mid,
                {"input": 0, "output": 0, "cache_read": 0, "reasoning": 0, "calls": 0},
            )
            bucket["input"] += leg.get("input", 0)
            bucket["output"] += leg.get("output", 0)
            bucket["cache_read"] += leg.get("cache_read", 0)
            bucket["reasoning"] += leg.get("reasoning", 0)
            bucket["calls"] += leg.get("calls", 0)
    else:
        mid = (last.get("models") or ["unknown"])[0]
        if mid not in seen_models:
            seen_models.append(mid)
        bucket = per_model.setdefault(
            mid,
            {"input": 0, "output": 0, "cache_read": 0, "reasoning": 0, "calls": 0},
        )
        bucket["input"] += last["input"]
        bucket["output"] += last["output"]
        bucket["cache_read"] += last["cache_read"]
        bucket["reasoning"] += last["reasoning"]
        bucket["calls"] += last["calls"]


def _grok_aggregate_billable_usage(snapshots):
    """Aggregate run-cumulative usage snapshots into session + per-model totals.

    Observed Grok ACP semantics (live sessions):
      * Each ``params.update.usage`` is cumulative **within a run**
      * A **drop** in ``modelCalls`` marks a new run
    Rule: split on ``modelCalls`` decreases; take the **last** snapshot of
    each run; sum those. Returns None if no snapshots.
    """
    totals = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "reasoning": 0,
        "calls": 0,
        "models": [],
        "by_model": {},
    }
    seen_models = []
    per_model = {}
    run_last = None
    n = 0
    for snap in snapshots:
        n += 1
        if run_last is not None and snap["calls"] < run_last["calls"]:
            _grok_flush_run_last(run_last, totals, per_model, seen_models)
        run_last = snap
    if n == 0:
        return None
    _grok_flush_run_last(run_last, totals, per_model, seen_models)
    totals["models"] = seen_models
    totals["by_model"] = {}
    for mid, v in per_model.items():
        inp, out, cache = v["input"], v["output"], v["cache_read"]
        totals["by_model"][mid] = {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache,
            "total_tokens": inp + out,
            "model_calls": v["calls"],
            "cache_hit_rate": compute_cache_hit_rate(
                inp, cache, input_includes_cache=True
            ),
        }
    return totals


def _grok_read_billable_usage(session_dir):
    """Single-pass stream of updates.jsonl → billable agg (no intermediate list).

    Preferred hot path for family reports and cross-session aggregates.
    """
    return _grok_aggregate_billable_usage(_grok_iter_usage_snapshots(session_dir))


# mtime-aware micro-cache: family/aggregate often re-read the same child dirs
_GROK_USAGE_CACHE = {}  # path -> (mtime_ns, size, agg|None)
_GROK_USAGE_CACHE_MAX = 256


def _grok_read_billable_usage_cached(session_dir):
    """Cached billable usage; invalidates on updates.jsonl mtime/size change."""
    updates_file = Path(session_dir) / "updates.jsonl"
    key = str(updates_file)
    try:
        st = updates_file.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    hit = _GROK_USAGE_CACHE.get(key)
    if hit and hit[0] == sig[0] and hit[1] == sig[1]:
        return hit[2]
    agg = _grok_read_billable_usage(session_dir)
    if len(_GROK_USAGE_CACHE) >= _GROK_USAGE_CACHE_MAX:
        # Drop an arbitrary oldest half to bound memory (FIFO-ish)
        for i, k in enumerate(list(_GROK_USAGE_CACHE.keys())):
            if i >= _GROK_USAGE_CACHE_MAX // 2:
                break
            _GROK_USAGE_CACHE.pop(k, None)
    _GROK_USAGE_CACHE[key] = (sig[0], sig[1], agg)
    return agg


def _grok_apply_usage_agg(stats, agg):
    """Write billable agg into a stats dict. Returns True if agg is non-empty."""
    if not agg:
        return False
    stats["input_tokens"] = agg["input"]
    stats["output_tokens"] = agg["output"]
    stats["cache_read_tokens"] = agg["cache_read"]
    stats["total_tokens"] = agg["input"] + agg["output"]
    if agg.get("by_model"):
        stats["model_usage"] = agg["by_model"]
    if agg.get("models") and (not stats.get("model") or stats["model"] == "unknown"):
        stats["model"] = agg["models"][0]
    # Grok billable inputTokens already include cachedReadTokens.
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return True


def _grok_apply_usage_from_updates(session_dir, stats):
    """Fill billable token fields from updates.jsonl. Returns True if applied."""
    return _grok_apply_usage_agg(stats, _grok_read_billable_usage_cached(session_dir))


def _grok_session_token_profile(session_dir):
    """Lightweight profile: model name + billable tokens only (no chat scan).

    Used by family/aggregate paths to avoid N× full ``_grok_session_stats``.
    """
    from echolib._helpers import _empty_stats

    session_dir = Path(session_dir)
    stats = _empty_stats("grok")
    stats["slug"] = session_dir.name
    summary_file = session_dir / "summary.json"
    if summary_file.is_file():
        try:
            with open(summary_file, encoding="utf-8") as f:
                summary = json.load(f)
            model = summary.get("current_model_id") or ""
            if model:
                stats["model"] = model
            stats["summary"] = (
                summary.get("session_summary")
                or summary.get("generated_title")
                or ""
            )[:100]
        except (json.JSONDecodeError, OSError):
            pass
    # Prefer signals primary model only if summary missing
    if not stats.get("model") or stats["model"] == "unknown":
        _grok_apply_signals(session_dir, stats)
    _grok_apply_usage_from_updates(session_dir, stats)
    if not stats.get("total_tokens"):
        stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return stats


def _grok_token_bucket(stats_or_leg=None):
    """Normalize a stats/leg dict into a small token bucket."""
    s = stats_or_leg or {}
    inp = int(s.get("input_tokens") or 0)
    cache = int(s.get("cache_read_tokens") or 0)
    return {
        "input_tokens": inp,
        "output_tokens": int(s.get("output_tokens") or 0),
        "cache_read_tokens": cache,
        "total_tokens": int(
            s.get("total_tokens")
            or (inp + int(s.get("output_tokens") or 0))
        ),
        "model_calls": int(s.get("model_calls") or 0),
        "cache_hit_rate": compute_cache_hit_rate(inp, cache),
    }


def _grok_add_buckets(dst, src):
    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "total_tokens", "model_calls"):
        dst[k] = int(dst.get(k) or 0) + int(src.get(k) or 0)
    dst["cache_hit_rate"] = compute_cache_hit_rate(
        dst.get("input_tokens"), dst.get("cache_read_tokens")
    )
    return dst


def _grok_sub_buckets(a, b):
    """Non-negative a - b for token fields."""
    out = {}
    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "total_tokens", "model_calls"):
        out[k] = max(0, int(a.get(k) or 0) - int(b.get(k) or 0))
    out["cache_hit_rate"] = compute_cache_hit_rate(
        out.get("input_tokens"), out.get("cache_read_tokens")
    )
    return out


def _grok_find_session_dir(session_id, hint_parent_dir=None):
    """Locate a Grok session directory by id (prefer same cwd group as parent)."""
    if not session_id:
        return None
    if hint_parent_dir is not None:
        parent = Path(hint_parent_dir)
        # Child sessions are siblings under the same encoded-cwd group
        sibling = parent.parent / session_id
        if sibling.is_dir() and (sibling / "summary.json").is_file():
            return sibling
        # Rare: nested under parent
        nested = parent / session_id
        if nested.is_dir() and (nested / "summary.json").is_file():
            return nested
    if not GROK_DIR.is_dir():
        return None
    for group in GROK_DIR.iterdir():
        if not group.is_dir():
            continue
        cand = group / session_id
        if cand.is_dir() and (cand / "summary.json").is_file():
            return cand
    return None


def grok_list_subagents(session_dir):
    """List child subagents declared under ``session_dir/subagents/*/meta.json``.

    Each item: subagent_id, child_session_id, parent_session_id, subagent_type,
    description, child_path (resolved session dir or None).
    """
    session_dir = Path(session_dir)
    sub_root = session_dir / "subagents"
    if not sub_root.is_dir():
        return []
    out = []
    for entry in sorted(sub_root.iterdir()):
        meta_file = entry / "meta.json" if entry.is_dir() else None
        if not meta_file or not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(meta, dict):
            continue
        child_id = meta.get("child_session_id") or meta.get("subagent_id") or entry.name
        child_path = _grok_find_session_dir(child_id, hint_parent_dir=session_dir)
        out.append({
            "subagent_id": meta.get("subagent_id") or entry.name,
            "child_session_id": child_id,
            "parent_session_id": meta.get("parent_session_id") or session_dir.name,
            "subagent_type": meta.get("subagent_type") or "",
            "description": meta.get("description") or "",
            "child_path": str(child_path) if child_path else None,
        })
    return out


def _grok_sum_model_maps(model_maps):
    """Merge many model_usage maps by summing counters per model."""
    merged = {}
    for mu in model_maps:
        if not isinstance(mu, dict):
            continue
        for mid, leg in mu.items():
            bucket = merged.setdefault(mid, _grok_token_bucket())
            _grok_add_buckets(bucket, _grok_token_bucket(leg))
    return merged


def grok_family_usage_report(session_dir):
    """主会话 vs 子代理 token 分账报告（账单级）。

    Grok 把子代理落成独立 session（sessions 树中的 sibling），父目录
    ``subagents/<id>/meta.json`` 只存元数据。父会话 ``updates.jsonl`` 的
    ``modelUsage`` 在部分版本会 **汇总进子代理用量**（rollup），部分版本则
    **父子各自独立**（separate）。

    判定：
      * 若存在子模型 m 使得 parent[m] < sum(children[m]) → separate
      * 若所有子模型均 parent[m] >= sum(children[m]) 且至少有一个子代理
        → rollup（主会话自身 = parent − children）
      * 无子代理 → standalone

    Returns dict with:
      accounting, parent, children[], main_only, subagents_total,
      by_model_main, by_model_subagents, by_model_family, family_total
    """
    session_dir = Path(session_dir)
    # Token-only profile: skip chat/events full scan on parent + children
    parent_stats = _grok_session_token_profile(str(session_dir))
    parent_mu = dict(parent_stats.get("model_usage") or {})
    parent_bucket = _grok_token_bucket(parent_stats)

    children = []
    child_mus = []
    sub_bucket = _grok_token_bucket()
    for meta in grok_list_subagents(session_dir):
        child = {
            "subagent_id": meta["subagent_id"],
            "child_session_id": meta["child_session_id"],
            "subagent_type": meta["subagent_type"],
            "description": meta["description"],
            "child_path": meta["child_path"],
            "stats": None,
            "model_usage": {},
        }
        if meta["child_path"]:
            st = _grok_session_token_profile(meta["child_path"])
            child["stats"] = {
                "model": st.get("model"),
                **_grok_token_bucket(st),
            }
            child["model_usage"] = dict(st.get("model_usage") or {})
            _grok_add_buckets(sub_bucket, _grok_token_bucket(st))
            child_mus.append(child["model_usage"])
        children.append(child)

    child_mu_sum = _grok_sum_model_maps(child_mus)

    if not children:
        accounting = "standalone"
    else:
        accounting = "rollup"
        for mid, leg in child_mu_sum.items():
            p_in = (parent_mu.get(mid) or {}).get("input_tokens") or 0
            c_in = leg.get("input_tokens") or 0
            if p_in < c_in:
                accounting = "separate"
                break
        # If parent has no model_usage but has children with tokens → separate
        if not parent_mu and sub_bucket["input_tokens"] > 0:
            accounting = "separate"

    if accounting == "rollup" and children:
        by_model_main = {}
        all_models = set(parent_mu) | set(child_mu_sum)
        for mid in all_models:
            p = _grok_token_bucket(parent_mu.get(mid))
            c = _grok_token_bucket(child_mu_sum.get(mid))
            main_leg = _grok_sub_buckets(p, c)
            if any(main_leg[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens")):
                by_model_main[mid] = main_leg
        main_only = _grok_sub_buckets(parent_bucket, sub_bucket)
        # Prefer sum of main legs when model map is richer
        if by_model_main:
            summed = _grok_token_bucket()
            for leg in by_model_main.values():
                _grok_add_buckets(summed, leg)
            # Keep main_only totals aligned with per-model sum when close
            main_only = summed
        by_model_subagents = child_mu_sum
        by_model_family = parent_mu  # already family-wide under rollup
        family_total = parent_bucket
    else:
        # separate or standalone: parent is main; family = parent + children
        by_model_main = parent_mu
        main_only = parent_bucket
        by_model_subagents = child_mu_sum
        by_model_family = _grok_sum_model_maps([parent_mu, child_mu_sum])
        family_total = _grok_token_bucket(parent_bucket)
        _grok_add_buckets(family_total, sub_bucket)

    return {
        "session_dir": str(session_dir),
        "session_id": session_dir.name,
        "accounting": accounting,  # rollup | separate | standalone
        "parent": {
            "model": parent_stats.get("model"),
            "summary": parent_stats.get("summary"),
            **parent_bucket,
            "model_usage": parent_mu,
        },
        "children": children,
        "main_only": main_only,
        "subagents_total": sub_bucket,
        "family_total": family_total,
        "by_model_main": by_model_main,
        "by_model_subagents": by_model_subagents,
        "by_model_family": by_model_family,
        "subagent_count": len(children),
        "subagent_resolved": sum(1 for c in children if c.get("child_path")),
    }


def grok_aggregate_model_usage(session_dirs=None, limit=50, mode="family",
                               dedupe_family=None):
    """跨会话汇总各模型账单级 token（去重、不漏计）。

    *mode*:
      - ``family``（默认，推荐计费）:
          有子代理的父会话只计入 **家族一次**（``by_model_family``）：
            rollup → 父 usage（已含子）；separate → 父+子之和。
          纯子会话永不单独计入。无家族的会话按自身 usage 计一次。
      - ``session``: 每个会话目录各计一次，但跳过所有子会话 id（可能对
          separate 家族 **漏计** 子代理 — 仅兼容旧行为）。
      - ``raw``: 每个目录都计，允许父子双计（调试用）。

    *dedupe_family*: 已弃用。True→session，False→raw；请改用 mode=。

    Returns: {model_id: {input_tokens, output_tokens, cache_read_tokens,
                         total_tokens, model_calls, sessions}}
    """
    if dedupe_family is not None:
        mode = "session" if dedupe_family else "raw"
    if mode not in ("family", "session", "raw"):
        mode = "family"

    if session_dirs is None:
        from echolib._adapters import grok_list_sessions

        metas = grok_list_sessions(limit=limit or 50)
        session_dirs = []
        for m in metas:
            p = Path(m.full_path)
            session_dirs.append(p if p.is_dir() else p.parent)
    else:
        session_dirs = [Path(p) for p in session_dirs]

    # Index parent ↔ children for family/session modes
    child_to_parent = {}  # child_id -> parent_id
    parents_with_kids = set()
    for sd in session_dirs:
        kids = grok_list_subagents(sd)
        if not kids:
            continue
        parents_with_kids.add(sd.name)
        for meta in kids:
            cid = meta.get("child_session_id")
            if cid:
                child_to_parent[cid] = sd.name

    dir_ids = {sd.name for sd in session_dirs}
    totals = {}

    def _add_mu(mu, sessions_inc=1):
        if not mu:
            return
        for mid, leg in mu.items():
            bucket = totals.setdefault(
                mid,
                {**_grok_token_bucket(), "sessions": 0},
            )
            _grok_add_buckets(bucket, _grok_token_bucket(leg))
            bucket["sessions"] = int(bucket.get("sessions") or 0) + sessions_inc
            bucket["cache_hit_rate"] = compute_cache_hit_rate(
                bucket.get("input_tokens"), bucket.get("cache_read_tokens")
            )

    def _profile_mu(sd):
        st = _grok_session_token_profile(str(sd))
        mu = st.get("model_usage") or {}
        if not mu and (st.get("input_tokens") or st.get("output_tokens")):
            mu = {st.get("model") or "unknown": _grok_token_bucket(st)}
        return mu

    for sd in session_dirs:
        sid = sd.name
        if mode == "raw":
            _add_mu(_profile_mu(sd))
            continue

        # Skip child only when its parent is also in this aggregation set
        parent_id = child_to_parent.get(sid)
        if parent_id and parent_id in dir_ids:
            if mode in ("session", "family"):
                continue

        if mode == "session":
            _add_mu(_profile_mu(sd))
            continue

        # mode == family
        if sid in parents_with_kids:
            rep = grok_family_usage_report(str(sd))
            mu = rep.get("by_model_family") or {}
            if not mu:
                mid = (rep.get("parent") or {}).get("model") or "unknown"
                mu = {mid: rep.get("family_total") or _grok_token_bucket()}
            _add_mu(mu, sessions_inc=1)
            continue
        # standalone (or orphan child whose parent not in set)
        _add_mu(_profile_mu(sd))

    return totals


def _grok_session_stats(path):
    """Dedicated stats for Grok sessions.

    Priority:
      1. summary.json — timestamps / title / model
      2. signals.json — activity counters (tools/messages/errors)
      3. updates.jsonl — **billable** token usage (ACP usage snapshots)
      4. events.jsonl + chat_history.jsonl — activity fallback when no signals
    """
    from echolib._helpers import _empty_stats

    resolved = _grok_resolve_path(path)
    stats = _empty_stats("grok")
    stats["slug"] = Path(resolved).stem

    # Timestamps, model, and summary from summary.json
    session_dir = Path(resolved).parent
    if Path(path).is_dir():
        session_dir = Path(path)
    summary_file = session_dir / "summary.json"
    if summary_file.exists():
        try:
            with open(summary_file, encoding="utf-8") as f:
                summary = json.load(f)
            info = summary.get("info", {}) if isinstance(summary.get("info"), dict) else {}
            created = summary.get("created_at") or info.get("created_at") or ""
            updated = (
                summary.get("updated_at")
                or summary.get("last_active_at")
                or info.get("updated_at")
                or ""
            )
            if created:
                stats["started"] = _normalize_timestamp(created)
            if updated:
                stats["ended"] = _normalize_timestamp(updated)
            stats["summary"] = (
                summary.get("session_summary")
                or summary.get("generated_title")
                or ""
            )[:100]
            model = summary.get("current_model_id", "")
            if model:
                stats["model"] = model
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: use file mtime if no timestamps from summary
    if not stats["started"] or not stats["ended"]:
        try:
            mtime = os.path.getmtime(resolved)
            nt = _normalize_timestamp(mtime)
            if nt:
                if not stats["started"]:
                    stats["started"] = nt
                if not stats["ended"]:
                    stats["ended"] = nt
        except OSError:
            pass

    # Activity counters (fast path) + billable tokens (always attempt)
    had_signals = _grok_apply_signals(session_dir, stats)
    _grok_apply_usage_from_updates(session_dir, stats)

    if had_signals:
        # tokens already filled when updates.jsonl exists; keep 0s otherwise
        if not stats["total_tokens"]:
            stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
        return stats

    # Count errors from events.jsonl (outcome is "error" or "failure")
    events_file = session_dir / "events.jsonl"
    if events_file.exists():
        try:
            with open(events_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if event.get("type") == "tool_completed" and event.get("outcome") in (
                        "error", "failure",
                    ):
                        stats["errors"] += 1
        except OSError:
            pass

    # Count from chat_history.jsonl
    try:
        with open(resolved, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                rtype = rec.get("type", "")
                if rtype == "user":
                    # Align with _grok_extract_messages: system-reminder /
                    # user_info injections are not real user turns.
                    content = rec.get("content", "")
                    text = ""
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ).strip()
                    if not text:
                        continue
                    if text.startswith("<system-reminder>") or text.startswith("<user_info>"):
                        continue
                    stats["user_messages"] += 1
                elif rtype == "assistant":
                    stats["assistant_messages"] += 1
                    # Tool calls are embedded in assistant messages
                    tool_calls = rec.get("tool_calls", [])
                    if isinstance(tool_calls, list):
                        stats["tool_calls"] += len(tool_calls)
                    # Model from assistant message
                    if not stats["model"]:
                        model_id = rec.get("model_id", "")
                        if model_id:
                            stats["model"] = model_id
                elif rtype == "tool_result":
                    # Detect errors in tool results
                    content = rec.get("content", "")
                    if isinstance(content, str):
                        if "Exit Code:" in content and "Exit Code: 0" not in content:
                            stats["errors"] += 1
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                text = block.get("text", "")
                                if isinstance(text, str) and "Exit Code:" in text and "Exit Code: 0" not in text:
                                    stats["errors"] += 1
                                    break
    except OSError:
        pass
    if not stats["total_tokens"]:
        stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    # Grok chat_history fallback (usage usually already applied from updates.jsonl).
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return stats
