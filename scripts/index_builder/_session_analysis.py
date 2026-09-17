"""Per-session analysis: one session file in, derived index fields out.

Two strategies, same output contract (stats, tools, messages, identity):

``analyze_single_pass``      reads the JSONL once and computes everything.
``_enrich_identity_fields``  fills gaps after adapter dispatch, for formats
                             whose billable data lives outside the transcript.

The single pass exists because the adapter-dispatch path reads the same file
three to four times per session (stats, tools, messages, identity scan).
Measured on real large sessions: 205 ms vs 75 ms, 64 ms vs 22 ms, 49 ms vs
18 ms — 2.7x, with stats/tokens/model/cache_hit_rate/tool_usage identical.
``single_pass_unsupported`` gates it: formats that keep billable tokens outside
the transcript, use cumulative snapshots, or nest usage per provider must go
through the adapter, where the token semantics are known.
"""

import json
import math
import os
from collections import Counter
from datetime import datetime

import echolib

from echolib._contracts import SessionStats
from echolib._helpers import _extract_content_text, _iter_jsonl
from echolib._registry_data import environment_model_tokens, single_pass_adapters

import logging as _logging
_log = _logging.getLogger("index_builder")


# ---------------------------------------------------------------------------
# Single-pass analysis — read JSONL once, compute everything in-memory.
# ---------------------------------------------------------------------------

def _has_foreign_token_source(path_str: str) -> bool:
    """True when this file's billable data is not fully inside the transcript.

    Single-pass is optimised for Claude-like JSONL (inline usage + messages).
    Formats that store billable tokens outside the transcript, use cumulative
    snapshots, or keep usage in provider-specific nests must not be approximated
    here — one wrong pass would silently zero cache fields or invent totals.
    """
    p = path_str.replace("\\", "/")
    name = os.path.basename(p)
    # ZCode: tokens live in cli/db SQLite, not transcript.jsonl
    if name == "transcript.jsonl" and "sess_" in p and "agent_" in p:
        return True
    # Grok: billable usage is in sibling updates.jsonl
    if name == "chat_history.jsonl":
        return True
    # Codex: token_count events are cumulative snapshots (need max, not sum)
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        return True
    # Kimi / Kimi Code wire formats (StatusUpdate / usage.record)
    if name == "wire.jsonl":
        return True
    # WorkBuddy: usage lives in providerData.usage (cached_tokens details).
    # Single-pass only sees a vague total and drops cache_hit_rate + model.
    if "/.workbuddy/" in p or "/workbuddy/" in p:
        return True
    return False


def _should_skip_single_pass(path_str: str, agent: str) -> bool:
    """Return True when adapter dispatch is the ground-truth path.

    Fail-closed on two independent conditions:

    1. The adapter must declare a layout the reader can mirror (allow-list, see
       ``single_pass_adapters``). A deny-list is not enough here: the reader
       returns ZERO for text it cannot parse rather than raising, so any
       unlisted layout silently loses the whole session. Measured with the
       deny-list alone — ``dsh`` (zstd event stores) and ``universal``
       (SchemaProbe, e.g. commandcode) both came back empty: 610 messages
       reported as 0.
    2. No path rule may veto it (see ``_has_foreign_token_source``).
    """
    if agent not in single_pass_adapters():
        return True
    return _has_foreign_token_source(path_str)


# Record types the reader recognises. Used as a self-check: a file that parses
# to records but matches none of these is a foreign layout the allow-list has
# not caught yet, and must go back to the adapter instead of yielding zeros.
_KNOWN_RECORD_TYPES = frozenset({
    "user", "assistant", "system", "summary", "progress",
    "queue-operation", "file-history-snapshot", "pr-link",
})


def _as_int(value):
    """Coerce a counter field from third-party JSONL to an int, never raising.

    Token counters arrive from a dozen agent formats: an int in one, a float in
    another, and ``null`` or a quoted string in a hand-edited or version-skewed
    transcript. The accumulation sites below used to do arithmetic straight on
    the raw value, so ``x + None`` or ``"12" + 0`` raised — and those sums run
    inside ``_compute_session`` *outside* its per-session error handling, so a
    single bad field aborted the whole build instead of skipping one session.
    Measured: one transcript with ``"input_tokens": null`` raised TypeError out
    of ``_compute_session`` and through ``pool.map``.

    Missing, null, bool and unparseable values count as 0 — the same "absent
    means zero" reading the call sites already used with ``or 0``.
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, float):
        # json.loads accepts the bare literals Infinity / -Infinity / NaN, so a
        # transcript can carry them and int(inf) raises OverflowError.
        return int(value) if math.isfinite(value) else 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, OverflowError):
            return 0
    return 0


def _single_pass_analyze(path, agent):
    """Single-pass analysis: read JSONL once, produce stats+tools+messages+identity.

    Eliminates the 3-4 redundant disk reads per session that the adapter path
    performs (stats, tools, messages, identity scan). Gated by
    ``_should_skip_single_pass`` so the caller cannot bypass the safety check.

    Returns:
        (stats, tools, messages, identity) tuple, or None when this file must
        go through adapter dispatch.
    """
    path_str = str(path)
    if path_str.startswith("dimcode://") or path_str.startswith("dimcode:"):
        return None
    if "://" in path_str and not path_str.startswith("file:"):
        return None
    if not os.path.isfile(path_str):
        return None
    if _should_skip_single_pass(path_str, agent):
        return None

    try:
        records = list(_iter_jsonl(path))
    except Exception as exc:
        _log.debug("single-pass read failed for %s: %s", path_str, exc)
        return None
    if records and not any(
        isinstance(r, dict) and r.get("type") in _KNOWN_RECORD_TYPES for r in records
    ):
        # Unrecognised transcript shape: fail closed rather than emit zeros.
        _log.warning("single-pass: unrecognized record shape, using adapter for %s",
                     path_str)
        return None

    # -- stats accumulators (mirrors Claude session_stats + generic scan) --
    stats = {
        "slug": "", "model": "", "branch": "",
        "started": "", "ended": "",
        "user_messages": 0, "assistant_messages": 0,
        "tool_calls": 0, "files_edited": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "compactions": 0, "summary": "",
    }

    # -- tool extraction accumulators (mirrors Claude extract_tools) --
    tool_calls_map = {}          # tid -> (ts, name, key)
    tool_results = {}            # tid -> (status, preview)

    # -- messages + identity accumulators --
    messages = []
    model_votes = Counter()
    token_sum = 0
    token_max = 0  # codex cumulative snapshots → take max
    first_prompt = ""

    for d in records:
        rtype = d.get("type", "")

        # --- error detection (top-level + nested tool_result) ---
        if d.get("isError") or d.get("is_error"):
            stats["errors"] += 1
        elif rtype == "user":
            _msg_c = d.get("message", {})
            if isinstance(_msg_c, dict):
                _content = _msg_c.get("content", [])
                if isinstance(_content, list):
                    for _b in _content:
                        if isinstance(_b, dict) and _b.get("is_error"):
                            stats["errors"] += 1
                            break

        # --- timestamps / branch / slug ---
        ts = d.get("timestamp", "")
        if ts and not isinstance(ts, str):
            ts = str(ts)
        if ts:
            if not stats["started"] or str(ts) < str(stats["started"]):
                stats["started"] = ts
            if str(ts) > str(stats["ended"]):
                stats["ended"] = ts
        if not stats["branch"]:
            stats["branch"] = d.get("gitBranch", "")
        if not stats["slug"]:
            stats["slug"] = d.get("slug", "")

        # --- model votes + token scan (mirrors _scan_file_for_model_tokens) ---
        for key in ("model", "model_id", "modelName", "modelAlias", "model_name"):
            v = d.get(key)
            if isinstance(v, str) and _is_useful_model(v):
                model_votes[_clean_model_name(v)] += 1
        msg = d.get("message")
        if isinstance(msg, dict):
            v = msg.get("model")
            if isinstance(v, str) and _is_useful_model(v):
                model_votes[_clean_model_name(v)] += 1
            usage = msg.get("usage")
            if isinstance(usage, dict):
                tok = _as_int(usage.get("total_tokens")) or (
                    _as_int(usage.get("input_tokens")) + _as_int(usage.get("output_tokens"))
                )
                if isinstance(tok, (int, float)) and tok > 0:
                    token_sum += int(tok)
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if payload:
            for key in ("model", "model_id", "requestModelName", "model_provider"):
                v = payload.get(key)
                if isinstance(v, str) and _is_useful_model(v):
                    model_votes[_clean_model_name(v)] += 1
            usage = payload.get("usage")
            if isinstance(usage, dict):
                tok = _as_int(usage.get("totalTokens")) or _as_int(usage.get("total_tokens"))
                if not tok:
                    tok = _as_int(usage.get("inputTokens")) + _as_int(usage.get("outputTokens"))
                if isinstance(tok, (int, float)) and tok > 0:
                    token_sum += int(tok)
                m = payload.get("model") or payload.get("modelName")
                if isinstance(m, str) and _is_useful_model(m):
                    model_votes[_clean_model_name(m)] += 1
            if payload.get("type") == "token_count" or d.get("type") == "event_msg":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                total_u = info.get("total_token_usage") if isinstance(info, dict) else None
                if isinstance(total_u, dict):
                    tok = _as_int(total_u.get("total_tokens")) or (
                        _as_int(total_u.get("input_tokens")) + _as_int(total_u.get("output_tokens"))
                    )
                    if isinstance(tok, (int, float)) and tok > token_max:
                        token_max = int(tok)
            if "model" in payload and isinstance(payload.get("model"), str):
                if _is_useful_model(payload["model"]):
                    model_votes[_clean_model_name(payload["model"])] += 3

        if d.get("type") == "usage.record":
            u = d.get("usage") or d.get("data") or payload
            if isinstance(u, dict):
                out_t = _as_int(u.get("output")) or _as_int(u.get("outputTokens")) or _as_int(u.get("output_tokens"))
                in_t = (_as_int(u.get("input")) or _as_int(u.get("inputTokens"))
                        or _as_int(u.get("input_tokens")) or _as_int(u.get("inputOther")))
                cache = _as_int(u.get("inputCacheRead")) or _as_int(u.get("cache_read_input_tokens"))
                tok = in_t + out_t + cache
                if isinstance(tok, (int, float)) and tok > 0:
                    token_sum += int(tok)

        # --- user messages + compaction ---
        if rtype == "user":
            umsg = d.get("message", {})
            if isinstance(umsg, dict) and not d.get("isMeta") and not d.get("isCompactSummary"):
                content = umsg.get("content", "")
                if not content and umsg.get("parts"):
                    content = umsg.get("parts")
                # Fallback: some agents (grok, etc.) put content at top level
                if not content:
                    content = d.get("content", "")
                if isinstance(content, list):
                    has_tr = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                    if not has_tr:
                        has_text = any(isinstance(b, dict) and b.get("type") == "text" for b in content)
                        has_bare = any(isinstance(b, dict) and b.get("text") for b in content)
                        if has_text or has_bare:
                            stats["user_messages"] += 1
                            # Emit USER message inline (preserves file order for FTS)
                            txt = _text_from_message_blob(content)
                            if txt and not txt.startswith("<system-reminder>") and not txt.startswith("[Request interrupted"):
                                messages.append({
                                    "role": "USER",
                                    "timestamp": d.get("timestamp", ""),
                                    "text": txt,
                                })
                            if not first_prompt:
                                if txt and not txt.startswith("<"):
                                    first_prompt = txt[:200]
                elif isinstance(content, str) and content.strip():
                    txt = content.strip()
                    stats["user_messages"] += 1
                    if not first_prompt and not txt.startswith("<"):
                        first_prompt = txt[:200]
                    messages.append({
                        "role": "USER",
                        "timestamp": d.get("timestamp", ""),
                        "text": txt,
                    })

                # Tool results (for tool status resolution)
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tid = block.get("tool_use_id", "")
                            is_error = block.get("is_error", False)
                            rc = block.get("content", "")
                            if isinstance(rc, list):
                                preview = " ".join(
                                    b.get("text", "")[:100] for b in rc if isinstance(b, dict)
                                )
                            elif isinstance(rc, str):
                                preview = rc[:150].replace("\n", " ").replace("\t", " ")
                            else:
                                preview = ""
                            tool_results[tid] = ("error" if is_error else "ok", preview)

        # --- assistant messages + tool calls ---
        elif rtype == "assistant":
            amsg = d.get("message", {})
            if isinstance(amsg, dict) and amsg.get("model") != "<synthetic>":
                stats["assistant_messages"] += 1
                amodel = amsg.get("model", "")
                if not stats["model"] and amodel:
                    stats["model"] = amodel

                usage = amsg.get("usage", {})
                if isinstance(usage, dict):
                    stats["input_tokens"] += _as_int(usage.get("input_tokens"))
                    stats["output_tokens"] += _as_int(usage.get("output_tokens"))
                    stats["cache_read_tokens"] += _as_int(usage.get("cache_read_input_tokens"))
                    stats["cache_create_tokens"] += _as_int(usage.get("cache_creation_input_tokens"))

                content = amsg.get("content", [])
                # Fallback: some agents (grok, etc.) put content at top level
                if not content:
                    content = d.get("content", [])
                if isinstance(content, list):
                    # Mirror echolib extract_messages: text + thinking + tool_use
                    # summaries all flow into the FTS index. A message is emitted
                    # when any part is present (even tool_use-only turns).
                    text_parts = []
                    has_part = False
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype == "text":
                            t = block.get("text", "").strip()
                            if t:
                                text_parts.append(t)
                                has_part = True
                        elif btype == "thinking":
                            t = block.get("thinking", "").strip()
                            if t:
                                text_parts.append("[THINKING] " + t)
                                has_part = True
                        elif btype == "tool_use":
                            stats["tool_calls"] += 1
                            tid = block.get("id", "")
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            if not isinstance(inp, dict):
                                inp = {}
                            key = _tool_key(name, inp)
                            tool_calls_map[tid] = (ts, name, key)
                            if key:
                                text_parts.append("[TOOL: {}] {}".format(name, key))
                            else:
                                text_parts.append("[TOOL: {}]".format(name))
                            has_part = True
                    if has_part:
                        messages.append({
                            "role": "ASSISTANT",
                            "timestamp": ts,
                            "text": "\n".join(text_parts),
                        })

        elif rtype == "summary":
            stats["summary"] = d.get("summary", "")

        elif rtype == "file-history-snapshot":
            backups = d.get("snapshot", {}).get("trackedFileBackups", {})
            fc = len(backups) if isinstance(backups, dict) else 0
            if fc > stats["files_edited"]:
                stats["files_edited"] = fc

        elif rtype == "system":
            st = d.get("subtype", "")
            if st in ("compact_boundary", "microcompact_boundary"):
                stats["compactions"] += 1

        # first_prompt fallback from payload.user_message / generic user line
        if not first_prompt:
            if payload.get("type") == "user_message":
                m = payload.get("message")
                if isinstance(m, str) and len(m.strip()) > 2:
                    first_prompt = m.strip()[:200]
        if not first_prompt:
            rtype_fallback = d.get("type") or d.get("role")
            if rtype_fallback in ("user", "human"):
                _txt = _text_from_message_blob(d.get("message") or d)
                if _txt and len(_txt) > 2 and not _txt.startswith("<"):
                    first_prompt = _txt[:200]

    # --- assemble tools list (joined calls + results) ---
    tools = []
    for tid, (t_ts, name, key) in sorted(tool_calls_map.items(), key=lambda x: x[1][0]):
        status, preview = tool_results.get(tid, ("ok", "(no result captured)"))
        tools.append({
            "timestamp": t_ts[:19] if t_ts else "",
            "name": name,
            "status": status,
            "key_input": key,
            "result_preview": preview,
        })

    # messages 已经在单次遍历中按文件顺序交错产出，直接使用
    all_msgs = messages

    # --- identity (mirrors _enrich_identity_fields + _scan_file_for_model_tokens) ---
    total = max(token_sum, token_max)
    # Same precedence as _enrich_identity_fields: the adapter-style first
    # assistant model wins, and only a generic/absent value falls back to the
    # plurality vote. Voting first looked equivalent but is not — a session that
    # switched models reported a different model here than through the adapter
    # path (measured: 53 msgs on LongCat-2.0 then 178 on deepseek-v4-flash).
    identity_model = _clean_model_name(stats.get("model") or "")
    if not _is_useful_model(identity_model) and model_votes:
        identity_model = model_votes.most_common(1)[0][0]
    if not _is_useful_model(identity_model):
        identity_model = ""
    identity_tokens = int(total) if total > 0 else (stats["input_tokens"] + stats["output_tokens"])
    # first_prompt 优先使用 _first_user_prompt_from_messages（与旧路径完全一致）；
    # 若为空则回退到内存中的 records 扫描（零额外 I/O），
    # 复现旧路径 _scan_file_for_model_tokens 的行为。
    identity_first = _first_user_prompt_from_messages(all_msgs)
    if not identity_first:
        identity_first = _first_prompt_from_records(records)
    identity_summary = stats["summary"] or (identity_first[:160] if identity_first else "")

    identity = {
        "model": identity_model,
        "total_tokens": identity_tokens,
        "summary": identity_summary[:500],
        "first_prompt": identity_first[:300] if identity_first else "",
    }

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    # Claude-like JSONL: input_tokens is the non-cached leg.
    echolib.attach_cache_hit_rates(stats, input_includes_cache=False)

    return stats, tools, all_msgs, identity


def _tool_key(name, inp):
    """Extract the most informative field from a tool_use input.
    Mirrors ``_tool_key`` in ``echolib._claude``.
    """
    if name in ("Read", "Write", "Edit", "MultiEdit"):
        return inp.get("file_path", "")
    elif name == "Bash":
        return inp.get("command", "")[:80]
    elif name in ("Grep", "Glob"):
        return inp.get("pattern", "")
    elif name == "Task":
        return inp.get("description", "")
    elif name == "WebSearch":
        return inp.get("query", "")
    elif name == "WebFetch":
        return inp.get("url", "")
    return ""


_GENERIC_MODELS = frozenset({
    "", "unknown", "<synthetic>", "openai-custom",
    "trae-cn (summary only)", "trae-cn",
}) | environment_model_tokens()


# 单一真源：模型名归一
from echolib._models import MODEL_ALIASES as _MODEL_ALIASES


def _clean_model_name(name: str) -> str:
    if not name:
        return ""
    s = str(name).strip()
    # strip path-like prefixes: uuid/LongCat-2.0 → LongCat-2.0
    if "/" in s and not s.startswith("http"):
        s = s.split("/")[-1]
    # drop bracket suffixes like deepseek-v4-flash[1M]
    if "[" in s:
        s = s.split("[", 1)[0]
    s = s.strip()
    key = s.lower()
    if key in _MODEL_ALIASES:
        return _MODEL_ALIASES[key]
    # soft: deepseek-flash* → deepseek-v4-flash
    if key.startswith("deepseek-flash") and "v4" not in key:
        return "deepseek-v4-flash"
    if key.startswith("longcat") and "preview" in key:
        return "LongCat-2.0-Preview"
    if key.startswith("longcat"):
        return "LongCat-2.0"
    return s


def _is_useful_model(name: str) -> bool:
    n = _clean_model_name(name).lower()
    if not n or n in _GENERIC_MODELS:
        return False
    if len(n) < 3:
        return False
    return True


def _text_from_message_blob(msg) -> str:
    """Best-effort plain text from heterogeneous message shapes.

    Formerly a standalone function — now a thin wrapper around the shared
    helper in ``echolib._helpers`` so that content-shape handling lives in
    one place. Supports a wider key set (``content``, ``prompt``) than the
    default helper for the indexer-specific paths.
    """
    if msg is None:
        return ""
    if isinstance(msg, str):
        return msg.strip()
    if isinstance(msg, list):
        return _extract_content_text(msg, keys=("text", "message", "content", "prompt"))
    if not isinstance(msg, dict):
        return ""
    return _extract_content_text(msg, keys=("text", "message", "content", "prompt"))


def _first_user_prompt_from_messages(messages) -> str:
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").lower()
        if role and role not in ("user", "human"):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if text.startswith(("<", "[Request interrupted", "System:", "[System]")):
            continue
        if text.lower() in ("ok", "test", "hi", "hey"):
            continue
        return text[:200]
    return ""


def _first_prompt_from_records(records) -> str:
    """Fallback first_prompt scan over raw JSONL records (in-memory).

    Mirrors the user-branch of ``_scan_file_for_model_tokens``: finds the
    first ``type=user`` record whose extracted text is non-trivial and does
    not start with ``<``. Used only when ``_first_user_prompt_from_messages``
    yields nothing, matching the old multi-pass fallback behaviour.
    """
    for d in records or []:
        rtype = d.get("type") or d.get("role")
        if rtype not in ("user", "human"):
            continue
        text = _text_from_message_blob(d.get("message") or d)
        if text and len(text) > 2 and not text.startswith("<"):
            return text[:200]
    return ""


def _scan_file_for_model_tokens(path: str, max_lines: int = 8000) -> dict:
    """Lightweight pass over raw JSONL for model + token totals.

    Handles Claude / Grok / Codex / Kimi wire / ZCode transcript shapes.
    Returns {model, total_tokens, first_prompt}.
    """
    path_str = str(path)
    out = {"model": "", "total_tokens": 0, "first_prompt": ""}
    if path_str.startswith("dimcode://") or path_str.startswith("dimcode:"):
        return out
    if "://" in path_str and not path_str.startswith("file:"):
        return out
    if path_str.endswith((".zst", ".zstd")):
        # 压缩流对文本扫描不可读；字段由适配器 stats 直接提供。
        return out
    if not os.path.isfile(path_str):
        return out

    model_votes: Counter = Counter()
    token_sum = 0
    token_max = 0  # codex cumulative snapshots → take max
    first_prompt = ""
    lines = 0
    try:
        with open(path_str, encoding="utf-8", errors="replace") as f:
            for line in f:
                lines += 1
                if lines > max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue

                # --- model candidates ---
                for key in ("model", "model_id", "modelName", "modelAlias", "model_name"):
                    v = d.get(key)
                    if isinstance(v, str) and _is_useful_model(v):
                        model_votes[_clean_model_name(v)] += 1
                msg = d.get("message")
                if isinstance(msg, dict):
                    v = msg.get("model")
                    if isinstance(v, str) and _is_useful_model(v):
                        model_votes[_clean_model_name(v)] += 1
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        tok = _as_int(usage.get("total_tokens")) or (
                            _as_int(usage.get("input_tokens"))
                            + _as_int(usage.get("output_tokens"))
                        )
                        if isinstance(tok, (int, float)) and tok > 0:
                            token_sum += int(tok)
                payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
                if payload:
                    for key in ("model", "model_id", "requestModelName", "model_provider"):
                        v = payload.get(key)
                        if isinstance(v, str) and _is_useful_model(v):
                            model_votes[_clean_model_name(v)] += 1
                    # zcode model_complete
                    usage = payload.get("usage")
                    if isinstance(usage, dict):
                        tok = usage.get("totalTokens") or usage.get("total_tokens")
                        if not tok:
                            tok = _as_int(usage.get("inputTokens")) + _as_int(usage.get("outputTokens"))
                        if isinstance(tok, (int, float)) and tok > 0:
                            token_sum += int(tok)
                        m = payload.get("model") or payload.get("modelName")
                        if isinstance(m, str) and _is_useful_model(m):
                            model_votes[_clean_model_name(m)] += 1
                    # codex token_count event
                    if payload.get("type") == "token_count" or d.get("type") == "event_msg":
                        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                        total_u = info.get("total_token_usage") if isinstance(info, dict) else None
                        if isinstance(total_u, dict):
                            tok = _as_int(total_u.get("total_tokens")) or (
                                _as_int(total_u.get("input_tokens"))
                                + _as_int(total_u.get("output_tokens"))
                            )
                            if isinstance(tok, (int, float)) and tok > token_max:
                                token_max = int(tok)
                    if payload.get("type") == "user_message" and not first_prompt:
                        m = payload.get("message")
                        if isinstance(m, str) and len(m.strip()) > 2:
                            first_prompt = m.strip()[:200]
                    # turn_context model
                    if "model" in payload and isinstance(payload.get("model"), str):
                        if _is_useful_model(payload["model"]):
                            model_votes[_clean_model_name(payload["model"])] += 3

                # kimi usage.record
                if d.get("type") == "usage.record":
                    u = d.get("usage") or d.get("data") or payload
                    if isinstance(u, dict):
                        # shapes: input/output or inputOther/output
                        out_t = u.get("output") or u.get("outputTokens") or u.get("output_tokens") or 0
                        in_t = (
                            u.get("input")
                            or u.get("inputTokens")
                            or u.get("input_tokens")
                            or u.get("inputOther")
                            or 0
                        )
                        cache = (
                            u.get("inputCacheRead")
                            or u.get("cache_read_input_tokens")
                            or 0
                        )
                        tok = _as_int(in_t) + _as_int(out_t) + _as_int(cache)
                        if isinstance(tok, (int, float)) and tok > 0:
                            token_sum += int(tok)

                # first user-ish line for claude / generic
                if not first_prompt:
                    rtype = d.get("type") or d.get("role")
                    if rtype in ("user", "human"):
                        text = _text_from_message_blob(d.get("message") or d)
                        if text and len(text) > 2 and not text.startswith("<"):
                            first_prompt = text[:200]
                    if rtype == "summary" and d.get("summary"):
                        # keep for caller via model path only
                        pass
    except OSError:
        return out

    # prefer max cumulative (codex) when present and larger
    total = max(token_sum, token_max)
    if model_votes:
        out["model"] = model_votes.most_common(1)[0][0]
    out["total_tokens"] = int(total) if total > 0 else 0
    out["first_prompt"] = first_prompt
    return out


def _compute_rich_stats(path: str, base_stats: SessionStats) -> dict:
    """Compute per-tool usage, errors, flags, and duration from a session."""
    tool_usage: Counter = Counter()
    tool_errors: Counter = Counter()
    try:
        for t in echolib.dispatch_extract_tools(path, limit=0):
            name = t.get("name", "unknown")
            tool_usage[name] += 1
            if t.get("status") == "error":
                tool_errors[name] += 1
    except Exception:
        pass

    return _build_rich_stats(tool_usage, tool_errors, base_stats, path)


def _rich_stats_from_tools(tools: list[dict], base_stats: SessionStats, path: str) -> dict:
    """Build rich stats from an in-memory tools list (single-pass path).

    Avoids re-reading the JSONL file the way ``_compute_rich_stats`` does.
    Delegates the final assembly to ``_build_rich_stats``.
    """
    tool_usage: Counter = Counter()
    tool_errors: Counter = Counter()
    for t in tools:
        name = t.get("name", "unknown")
        tool_usage[name] += 1
        if t.get("status") == "error":
            tool_errors[name] += 1
    return _build_rich_stats(tool_usage, tool_errors, base_stats, path)


def _build_rich_stats(tool_usage: Counter, tool_errors: Counter,
                      base_stats: SessionStats, path: str) -> dict:
    """Shared assembly of rich stats from computed tool_usage/tool_errors."""

    flags = []
    total_calls = sum(tool_usage.values())
    total_errors = sum(tool_errors.values())
    if total_calls > 0 and total_errors / total_calls > 0.25:
        pct = round(100 * total_errors / total_calls)
        flags.append(f"High overall tool error rate: {total_errors}/{total_calls} ({pct}%)")
    msg_count = base_stats.get("user_messages", 0) + base_stats.get("assistant_messages", 0)
    if msg_count > 40:
        flags.append(f"Long conversation ({msg_count} turns)")
    if tool_usage:
        top_tool, top_count = tool_usage.most_common(1)[0]
        if top_count > 15:
            flags.append(f"'{top_tool}' called {top_count} times")

    duration_seconds = None
    started = base_stats.get("started", "")
    ended = base_stats.get("ended", "")
    if started and ended:
        try:
            t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            duration_seconds = (t1 - t0).total_seconds()
        except Exception:
            pass

    project_name = None
    try:
        project_name = os.path.basename(os.path.dirname(path))
        if project_name.startswith("-"):
            parts = project_name.split("-")
            project_name = parts[-1] if parts else project_name
    except Exception:
        project_name = None
    # 适配器自带更准的归属时优先（DSH/Codex/DimCode 的 cwd，claude 系目录已够用）。
    # 目录派生名对 session-<uuid> 这类布局毫无信息量，必须让位。
    adapter_project = str(base_stats.get("project") or "").strip() if isinstance(base_stats, dict) else ""
    if adapter_project:
        project_name = adapter_project

    return {
        "tool_usage": dict(tool_usage),
        "tool_errors": dict(tool_errors),
        "flags": flags,
        "duration_seconds": duration_seconds,
        "project_name": project_name,
    }


def _enrich_identity_fields(path: str, stats: dict, messages: list) -> dict:
    """Fill model / tokens / first_prompt / summary gaps after adapter stats."""
    model = _clean_model_name(stats.get("model") or "")
    tokens = _as_int(stats.get("total_tokens"))
    summary = (stats.get("summary") or "").strip()
    first_prompt = (stats.get("first_prompt") or "").strip()

    if not first_prompt:
        first_prompt = _first_user_prompt_from_messages(messages)

    need_scan = (
        not _is_useful_model(model)
        or tokens <= 0
        or not first_prompt
    )
    scanned = _scan_file_for_model_tokens(path) if need_scan else {}
    if scanned:
        if not _is_useful_model(model) and scanned.get("model"):
            model = scanned["model"]
        if tokens <= 0 and scanned.get("total_tokens"):
            tokens = int(scanned["total_tokens"])
        if not first_prompt and scanned.get("first_prompt"):
            first_prompt = scanned["first_prompt"]

    if not summary and first_prompt:
        summary = first_prompt[:160]
    # Prefer real model names over adapter stubs
    if not _is_useful_model(model):
        model = ""

    return {
        "model": model,
        "total_tokens": tokens,
        "summary": summary[:500] if summary else "",
        "first_prompt": first_prompt[:300] if first_prompt else "",
    }

