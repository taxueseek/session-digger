"""Adapters for ZCode / DIM / DimCode environments — extracted from _adapters.py.

All ``*_session_stats`` here rely on ``_empty_stats`` from the parent package
(`echolib._adapters`). Import is deferred inside the stat functions to keep the
package-level import order stable.
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from echolib._claude import _normalize_timestamp
from echolib._helpers import (
    DIMCODE_DB_PATH,
    DIM_DIR,
    ZCODE_DIR,
    ZCODE_V2_DIR,
    _as_int,
    _extract_content_text,
    _iter_jsonl,
    _strip_system_reminder,
    attach_cache_hit_rates,
    compute_cache_hit_rate,
    cap,
    get_ro_sqlite,
    sql_limit,
    discovery_only_active,
)
from echolib._models import SessionMeta, normalize_model_name


# ── ZCode (transcript.jsonl trace) ────────────────────────────────────
# 单一真源在 echolib._helpers（与 DIMCODE_DB_PATH 同一处），本模块只做别名。
from echolib._helpers import ZCODE_DB_PATH as _ZCODE_DB  # noqa: E402


def _zcode_resolve_usage_session_ids(session_path):
    """Map a transcript/agent path → candidate ZCode session_id values for DB usage.

    Official ``model_usage.session_id`` is typically:
      * ``sess_<uuid>`` for main sessions
      * ``sess_subagent_agent_<uuid>`` for subagents (also in metadata.childSessionId)
    """
    p = Path(session_path)
    agent_dir = p.parent if p.is_file() else p
    if not agent_dir.is_dir() and p.is_file():
        agent_dir = p.parent

    ids = []
    meta_file = agent_dir / "metadata.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
        if isinstance(meta, dict):
            for key in ("childSessionId", "sessionId"):
                val = meta.get(key)
                if isinstance(val, str) and val.strip():
                    ids.append(val.strip())

    name = agent_dir.name
    if name.startswith("agent_"):
        ids.append(f"sess_subagent_{name}")
        ids.append(name)
    parent = agent_dir.parent.name if agent_dir.parent else ""
    if parent.startswith("sess_"):
        ids.append(parent)
    # Bare sess_* / agent_* path
    if name.startswith("sess_"):
        ids.append(name)

    seen = set()
    out = []
    for sid in ids:
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _zcode_fetch_model_usage(session_ids):
    """Read official per-request usage from model_usage; first matching session_id wins.

    Returns dict with input/output/cache/total + ``by_model`` map, or None.
    Each row is one model call — SUM is billable (no run-cumulative semantics).
    """
    if not session_ids:
        return None
    conn = _zcode_db_connect()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        # Table may be absent on older installs
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_usage'"
        )
        if not cur.fetchone():
            return None

        for sid in session_ids:
            cur.execute(
                """
                SELECT model_id,
                       COUNT(*) AS model_calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_create_tokens,
                       COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                       COALESCE(SUM(computed_total_tokens), 0) AS total_tokens
                FROM model_usage
                WHERE session_id = ?
                GROUP BY model_id
                """,
                (sid,),
            )
            rows = cur.fetchall()
            if not rows:
                continue

            by_model = {}
            tot_in = tot_out = tot_cache = tot_create = tot_calls = 0
            primary = ""
            primary_in = -1
            for row in rows:
                mid = (row["model_id"] if row["model_id"] is not None else "") or "unknown"
                mid = str(mid)
                # 归一化 model_id：统一大小写，合并变体（如 GLM-5.2 / glm-5.2）
                mid_normalized = normalize_model_name(mid)
                inp = int(row["input_tokens"] or 0)
                out = int(row["output_tokens"] or 0)
                cache = int(row["cache_read_tokens"] or 0)
                create = int(row["cache_create_tokens"] or 0)
                calls = int(row["model_calls"] or 0)
                total = int(row["total_tokens"] or 0) or (inp + out)
                # 合并同一模型的不同变体
                if mid_normalized in by_model:
                    existing = by_model[mid_normalized]
                    existing["input_tokens"] += inp
                    existing["output_tokens"] += out
                    existing["cache_read_tokens"] += cache
                    existing["cache_create_tokens"] += create
                    existing["total_tokens"] += total
                    existing["model_calls"] += calls
                    existing["cache_hit_rate"] = compute_cache_hit_rate(
                        existing["input_tokens"], existing["cache_read_tokens"]
                    )
                else:
                    by_model[mid_normalized] = {
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cache_read_tokens": cache,
                        "cache_create_tokens": create,
                        "total_tokens": total,
                        "model_calls": calls,
                        "cache_hit_rate": compute_cache_hit_rate(inp, cache),
                    }
                tot_in += inp
                tot_out += out
                tot_cache += cache
                tot_create += create
                tot_calls += calls
                if inp > primary_in:
                    primary_in = inp
                    primary = mid_normalized

            return {
                "session_id": sid,
                "model": primary or "zcode",
                "input_tokens": tot_in,
                "output_tokens": tot_out,
                "cache_read_tokens": tot_cache,
                "cache_create_tokens": tot_create,
                "total_tokens": tot_in + tot_out,
                "model_calls": tot_calls,
                "by_model": by_model,
            }
        return None
    except sqlite3.Error:
        return None


def _zcode_apply_usage_payload(stats, usage):
    """Write official usage payload into stats (incl. model_usage map)."""
    if not usage:
        return False
    stats["input_tokens"] = int(usage.get("input_tokens") or 0)
    stats["output_tokens"] = int(usage.get("output_tokens") or 0)
    stats["cache_read_tokens"] = int(usage.get("cache_read_tokens") or 0)
    stats["cache_create_tokens"] = int(usage.get("cache_create_tokens") or 0)
    stats["total_tokens"] = int(
        usage.get("total_tokens")
        or (stats["input_tokens"] + stats["output_tokens"])
    )
    by_model = usage.get("by_model") or {}
    if by_model:
        stats["model_usage"] = by_model
    model = usage.get("model") or ""
    if model and (not stats.get("model") or stats["model"] in ("zcode", "unknown", "")):
        stats["model"] = model
    # ZCode model_usage / model_usage table: input includes cache_read.
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return True


def _zcode_model_name(payload):
    """Normalize ZCode model fields (string or modelRef dict)."""
    if not isinstance(payload, dict):
        return ""
    for key in ("modelRef", "model"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if isinstance(raw, dict):
            for sub in ("modelId", "id", "name", "model"):
                val = raw.get(sub)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


def _zcode_input_text(inp, max_len=0):
    """Extract user text from turn_started.input (str or content blocks)."""
    if isinstance(inp, str):
        text = inp.strip()
    elif isinstance(inp, list):
        parts = []
        for item in inp:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str) and item.strip():
                parts.append(item)
        text = "\n".join(parts).strip()
    else:
        text = ""
    if max_len and text:
        return text[:max_len]
    return text


def _zcode_content_text(content, max_len=0):
    """Extract assistant text from model_complete.content (str | list | dict)."""
    if isinstance(content, str):
        text = content.strip()
    else:
        text = _extract_content_text(content, max_len=0).strip()
    if max_len and text:
        return text[:max_len]
    return text


def _zcode_slug(path: Path) -> str:
    """Prefer agent_* / sess_* id over bare 'transcript' stem."""
    if path.name == "transcript.jsonl":
        parent = path.parent.name
        if parent.startswith("agent_") or parent.startswith("sess_"):
            return parent
        grand = path.parent.parent.name if path.parent.parent else ""
        if grand.startswith("sess_"):
            return grand
    return path.stem


def _zcode_quick_scan(transcript: Path, max_records=400):
    """One-pass metadata for list_sessions: first prompt, model, counts, times."""
    if discovery_only_active():
        return {"started": "", "ended": "", "model": "", "first_prompt": "",
                "user_messages": 0, "assistant_messages": 0, "tool_calls": 0}
    started = ended = ""
    model = ""
    first_prompt = ""
    user_n = asst_n = tool_n = 0
    n = 0
    for rec in _iter_jsonl(transcript):
        n += 1
        ts = rec.get("timestamp", "")
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not started or nts < started:
                    started = nts
                if not ended or nts > ended:
                    ended = nts
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        if rtype == "turn_started":
            text = _zcode_input_text(payload.get("input", ""), max_len=200)
            if text:
                user_n += 1
                if not first_prompt:
                    first_prompt = text
        elif rtype == "model_complete":
            # Count every model iteration (tool-only completions have empty content)
            asst_n += 1
        elif rtype == "tool_call_scheduled":
            tool_n += 1
        elif rtype in ("model_network_status", "model_request") and not model:
            model = _zcode_model_name(payload)
        # Early exit once we have prompt+model and scanned enough for a list card
        if n >= max_records and first_prompt and model:
            break
    return {
        "started": started,
        "ended": ended or started,
        "model": model,
        "first_prompt": first_prompt,
        "user_messages": user_n,
        "assistant_messages": asst_n,
        "tool_calls": tool_n,
    }


def _zcode_db_title_map():
    """sess_id → human title from SQLite when available."""
    conn = _zcode_db_connect()
    if not conn:
        return {}
    titles = {}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, slug, title FROM session "
            "WHERE task_type IS NULL OR task_type != 'subagent_child'"
        )
        for row in cur.fetchall():
            title = (row["title"] or row["slug"] or "").strip()
            if title and row["id"]:
                titles[str(row["id"])] = title
    except (sqlite3.Error, KeyError, TypeError, IndexError):
        pass
    return titles


def zcode_list_sessions(cwd=None, limit=50, keyword=""):
    """List ZCode sessions from ~/.zcode/cli/agents/ (+ DB titles when present).

    Returns SessionMeta list (same contract as Claude/Codex/Cursor adapters).
    """
    if not ZCODE_DIR.exists():
        return []

    db_titles = _zcode_db_title_map()
    sessions = []
    keyword_l = keyword.lower() if keyword else ""

    try:
        sess_dirs = sorted(
            (d for d in ZCODE_DIR.iterdir() if d.is_dir() and d.name.startswith("sess_")),
            key=lambda d: d.stat().st_mtime if d.exists() else 0,
            reverse=True,
        )
    except OSError:
        return []

    for sess_dir in sess_dirs:
        try:
            agent_dirs = [
                d for d in sess_dir.iterdir()
                if d.is_dir() and d.name.startswith("agent_")
            ]
        except OSError:
            continue
        for agent_dir in agent_dirs:
            transcript = agent_dir / "transcript.jsonl"
            if not transcript.is_file():
                continue
            try:
                meta = _zcode_quick_scan(transcript)
                mtime = _normalize_timestamp(transcript.stat().st_mtime)
            except OSError:
                continue

            first = meta["first_prompt"]
            db_title = db_titles.get(sess_dir.name, "")
            summary = (db_title or first or sess_dir.name)[:100]
            if keyword_l and keyword_l not in summary.lower() and keyword_l not in agent_dir.name.lower():
                continue

            sessions.append(SessionMeta(
                session_id=agent_dir.name,
                full_path=str(transcript),
                created=meta["started"] or mtime,
                modified=meta["ended"] or mtime,
                message_count=meta["user_messages"] + meta["assistant_messages"],
                git_branch="",
                summary=summary,
                first_prompt=first[:200] if first else "",
                project_path=sess_dir.name,
            ))

    sessions.sort(key=lambda s: str(s.modified or s.created or ""), reverse=True)
    return cap(sessions, limit)


def zcode_session_stats(session_path):
    """Stats for ZCode transcript path.

    Tokens (preferred): official SQLite ``model_usage`` (per-request SUM, by model).
    Activity (messages/tools): still from transcript.jsonl when present.
    Fallback tokens: sum ``model_complete.usage`` on the transcript.
    """
    from echolib._helpers import _empty_stats
    p = Path(session_path)
    stats = _empty_stats("zcode")
    stats["slug"] = _zcode_slug(p)

    # Official billable tokens first (fast, per-model)
    db_usage = _zcode_fetch_model_usage(_zcode_resolve_usage_session_ids(session_path))
    tokens_from_db = _zcode_apply_usage_payload(stats, db_usage)

    if not p.exists() or not p.is_file():
        if not stats["model"] or stats["model"] == "zcode":
            stats["model"] = (db_usage or {}).get("model") or "zcode"
        if not stats.get("total_tokens"):
            stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
        return stats

    text_delta_turns = 0  # model_streaming kind=finish with prior text
    saw_text_delta = False

    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        ts = rec.get("timestamp", "")
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not stats["started"] or nts < stats["started"]:
                    stats["started"] = nts
                if nts > stats["ended"]:
                    stats["ended"] = nts
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue

        if rtype == "turn_started":
            if _zcode_input_text(payload.get("input", "")):
                stats["user_messages"] += 1
                if not stats["summary"]:
                    stats["summary"] = _zcode_input_text(payload.get("input", ""), max_len=200)
        elif rtype == "model_complete":
            # Always count model iterations (empty content is normal when only tools fire)
            stats["assistant_messages"] += 1
            if not tokens_from_db:
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    stats["input_tokens"] += int(
                        usage.get("inputTokens") or usage.get("input_tokens") or 0
                    )
                    stats["output_tokens"] += int(
                        usage.get("outputTokens") or usage.get("output_tokens") or 0
                    )
                    stats["cache_read_tokens"] += int(
                        usage.get("cacheReadTokens") or usage.get("cache_read_tokens") or 0
                    )
                    stats["cache_create_tokens"] += int(
                        usage.get("cacheWriteTokens") or usage.get("cache_create_tokens") or 0
                    )
        elif rtype == "model_streaming":
            kind = payload.get("kind")
            if kind == "text_delta" and payload.get("delta"):
                saw_text_delta = True
            elif kind == "finish" and saw_text_delta:
                text_delta_turns += 1
                saw_text_delta = False
            elif kind == "finish":
                saw_text_delta = False
        elif rtype == "tool_call_scheduled":
            stats["tool_calls"] += 1
        elif rtype == "tool_batch_complete":
            stats["errors"] += int(payload.get("errorCount") or 0)
        elif rtype in ("model_network_status", "model_request"):
            if not stats["model"] or stats["model"] == "zcode":
                model = _zcode_model_name(payload)
                if model:
                    stats["model"] = model
        elif rtype == "turn_complete" and not tokens_from_db:
            usage = payload.get("usage")
            # Authoritative turn totals: fully override per-model_complete accumulators
            # to avoid double-counting when both record types appear in one session.
            if isinstance(usage, dict) and usage.get("inputTokens"):
                stats["input_tokens"] = int(usage["inputTokens"])
                stats["output_tokens"] = int(usage.get("outputTokens") or 0)
                stats["cache_read_tokens"] = int(usage.get("cacheReadTokens") or 0)
                stats["cache_create_tokens"] = int(usage.get("cacheWriteTokens") or 0)

    # If model_complete never had text but streaming did, keep assistant_messages
    # from model_complete (already counted). text_delta_turns is diagnostic only.
    _ = text_delta_turns
    if not stats.get("total_tokens"):
        stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    if not stats["model"] or stats["model"] == "zcode":
        stats["model"] = (db_usage or {}).get("model") or "zcode"
    # Transcript-fallback path may lack rates until here
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return stats


def zcode_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from ZCode trace format.

    Assistant text is often empty on ``model_complete`` (tool-only iterations).
    Reassemble ``model_streaming`` ``text_delta`` chunks on ``finish`` — same
    idea as resume-session reconstructing turns from partial records.
    """
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    text_buf: list[str] = []
    reasoning_buf: list[str] = []
    buf_ts = ""
    stream_emitted = False  # avoid double-yield with model_complete

    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        ts = rec.get("timestamp", "")
        nt = _normalize_timestamp(ts) if ts else ""
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue

        if role in ("user", "both") and rtype == "turn_started":
            text = _zcode_input_text(payload.get("input", ""), max_len=0)
            if text:
                cleaned = _strip_system_reminder(text)
                if cleaned:
                    yield {"role": "USER", "timestamp": nt, "text": cleaned}
                    count += 1
                    if limit and count >= limit:
                        return

        if role not in ("assistant", "both"):
            continue

        if rtype == "model_streaming":
            kind = payload.get("kind")
            delta = payload.get("delta") or ""
            if kind == "start":
                stream_emitted = False
                text_buf = []
                reasoning_buf = []
            elif kind == "text_delta" and delta:
                if not text_buf:
                    buf_ts = nt
                text_buf.append(str(delta))
            elif kind == "reasoning_delta" and delta and thinking_limit != -1:
                reasoning_buf.append(str(delta))
            elif kind == "finish":
                text = "".join(text_buf).strip()
                reasoning = "".join(reasoning_buf).strip()
                text_buf = []
                reasoning_buf = []
                if thinking_limit != -1 and reasoning:
                    if thinking_limit > 0:
                        reasoning = reasoning[:thinking_limit]
                    text = (
                        f"[THINKING] {reasoning}\n{text}".strip()
                        if text
                        else f"[THINKING] {reasoning}"
                    )
                if text:
                    yield {"role": "ASSISTANT", "timestamp": nt or buf_ts, "text": text}
                    stream_emitted = True
                    count += 1
                    if limit and count >= limit:
                        return

        elif rtype == "model_complete":
            # Fallback when no streaming text (older traces / content-only complete)
            text = _zcode_content_text(payload.get("content"), max_len=0)
            if text and not stream_emitted:
                yield {"role": "ASSISTANT", "timestamp": nt, "text": text}
                count += 1
                if limit and count >= limit:
                    return
            stream_emitted = False


def zcode_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from ZCode trace; mark errors via tool_batch_complete."""
    p = Path(session_path)
    if not p.exists():
        return

    # Pre-scan error call IDs (batch may mark multiple tools)
    error_call_ids = set()
    for rec in _iter_jsonl(p):
        if rec.get("type") != "tool_batch_complete":
            continue
        pl = rec.get("payload", {})
        if not isinstance(pl, dict):
            continue
        if int(pl.get("errorCount") or 0) > 0:
            for tid in pl.get("toolCallIds") or []:
                if tid:
                    error_call_ids.add(tid)

    count = 0
    for rec in _iter_jsonl(p):
        if rec.get("type") != "tool_call_scheduled":
            continue
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        name = payload.get("toolName", "")
        if tool_filter and name != tool_filter:
            continue
        call_id = payload.get("toolCallId", "")
        is_error = call_id in error_call_ids
        if errors_only and not is_error:
            continue
        ts = rec.get("timestamp", "")
        nt = _normalize_timestamp(ts) if ts else ""
        args = payload.get("input", "")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        yield {
            "timestamp": nt,
            "name": name or "[tool]",
            "status": "error" if is_error else "ok",
            "key_input": str(args)[:150] if args else "",
            "result_preview": "(error)" if is_error else "",
        }
        count += 1
        if limit and count >= limit:
            return


def zcode_session_path(cwd, session_id=None):
    """Resolve ZCode session path by agent_* or sess_* id."""
    if not ZCODE_DIR.exists():
        return None
    if session_id:
        # Direct agent match
        for sess_dir in ZCODE_DIR.iterdir():
            if not sess_dir.is_dir():
                continue
            if sess_dir.name == session_id or sess_dir.name.endswith(session_id):
                # return newest agent transcript under this session
                best = None
                best_m = -1
                try:
                    for agent_dir in sess_dir.iterdir():
                        if not agent_dir.is_dir():
                            continue
                        # Exact agent match → immediate return
                        if agent_dir.name == session_id:
                            t = agent_dir / "transcript.jsonl"
                            if t.is_file():
                                return str(t)
                        # Broad search: name contains session_id
                        if session_id in agent_dir.name:
                            t = agent_dir / "transcript.jsonl"
                            if t.is_file():
                                m = t.stat().st_mtime
                                if m > best_m:
                                    best_m = m
                                    best = t
                except OSError:
                    continue
                if best:
                    return str(best)
    # Newest transcript overall
    newest = None
    newest_m = -1
    try:
        for sess_dir in ZCODE_DIR.iterdir():
            if not sess_dir.is_dir():
                continue
            for agent_dir in sess_dir.iterdir():
                t = agent_dir / "transcript.jsonl"
                if t.is_file():
                    m = t.stat().st_mtime
                    if m > newest_m:
                        newest_m = m
                        newest = t
    except OSError:
        pass
    return str(newest) if newest else str(ZCODE_DIR)


# ── ZCode SQLite DB mirror ────────────────────────────────────────────
def _zcode_db_connect():
    """Connect to ZCode SQLite DB via cached thread-local read-only pool."""
    return get_ro_sqlite(_ZCODE_DB)


def _zcode_db_fmt_timestamp(ts):
    """Format timestamp to ISO string. Handles millisecond Unix timestamps."""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OSError, OverflowError):
            return str(ts)
    if isinstance(ts, str):
        return ts
    return str(ts)


def _zcode_db_parse_message_data(data_json):
    """Parse the JSON ``data`` field of a message row."""
    if not data_json:
        return {}
    if isinstance(data_json, str):
        try:
            return json.loads(data_json)
        except (json.JSONDecodeError, ValueError):
            return {}
    return data_json if isinstance(data_json, dict) else {}


def zcode_db_list_sessions(limit=200, keyword=""):
    """List ZCode sessions from SQLite, in echolib-compatible format."""
    conn = _zcode_db_connect()
    if not conn:
        return []

    sessions = []
    cur = conn.cursor()
    cur.execute("""
        SELECT s.id, s.slug, s.title, s.task_type, s.time_created, s.time_updated,
               s.parent_id
        FROM session s
        WHERE s.task_type != 'subagent_child'
           OR s.task_type IS NULL
        ORDER BY s.time_created DESC
        LIMIT ?
    """, (sql_limit(limit * 2),))
    rows = cur.fetchall()

    msg_counts = {}
    if rows and not discovery_only_active():
        sids = [r["id"] for r in rows]
        for start in range(0, len(sids), 900):
            chunk = sids[start:start + 900]
            ph = ",".join("?" * len(chunk))
            for mid, cnt in cur.execute(
                f"SELECT session_id, COUNT(*) FROM message WHERE session_id IN ({ph}) GROUP BY session_id",
                chunk,
            ).fetchall():
                msg_counts[mid] = cnt

    for row in rows:
        sid = row["id"]
        title = row["title"] or row["slug"] or sid[:12]
        created = _zcode_db_fmt_timestamp(row["time_created"])
        modified = _zcode_db_fmt_timestamp(row["time_updated"])
        msg_count = msg_counts.get(sid, 0)

        summary = title[:100]

        if keyword:
            haystack = f"{summary} {title}".lower()
            if keyword.lower() not in haystack:
                continue

        sessions.append({
            "id": sid, "title": title,
            "created": created, "modified": modified,
            "message_count": msg_count, "path": f"zcode://{sid}",
            "agent": "zcode", "model": "",
        })

        if limit and len(sessions) >= limit:
            break

    return sessions


def zcode_db_session_path(cwd, session_id=None):
    """Resolve a ZCode SQLite session identifier (mirrors dimcode_session_path)."""
    if session_id and str(session_id).startswith("zcode://"):
        return str(session_id)
    return f"zcode://{session_id}" if session_id else str(_ZCODE_DB)


def _zcode_db_normalize_session_id(session_id):
    """``zcode://sess_x`` / ``zcode:sess_x`` / ``sess_x`` → ``sess_x``."""
    s = str(session_id or "")
    if s.startswith("zcode://"):
        return s[len("zcode://"):]
    if ":" in s and s.split(":", 1)[0] == "zcode":
        return s.split(":", 1)[1]
    return s


def _zcode_db_fetch_parts(cur, session_id: str) -> dict[str, list[dict]]:
    """Batch-fetch all parts for a ZCode session grouped by message_id (single query)."""
    by_msg: dict[str, list[dict]] = {}
    try:
        rows = cur.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        # Fallback for minimal schemas lacking part.session_id
        rows = cur.execute(
            """SELECT p.message_id, p.data
               FROM part p JOIN message m ON m.id = p.message_id
               WHERE m.session_id = ? ORDER BY p.id ASC""",
            (session_id,),
        ).fetchall()
    for row in rows:
        mid = row["message_id"]
        raw = row["data"]
        try:
            pd = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(pd, dict):
            by_msg.setdefault(mid, []).append(pd)
    return by_msg


_ZCODE_EDIT_TOOLS = frozenset({"edit_file", "write_file", "Edit", "Write", "MultiEdit"})


def zcode_db_parse_session(session_id, *, thinking_limit: int = 0):
    """Single-pass batched ZCode SQLite parser returning {'stats', 'messages', 'tools'}."""
    from echolib._helpers import _empty_stats

    session_id = _zcode_db_normalize_session_id(session_id)
    conn = _zcode_db_connect()
    if not conn:
        return {"stats": _empty_stats("zcode"), "messages": [], "tools": []}

    stats = _empty_stats("zcode")
    stats["slug"] = session_id[:12]
    messages = []
    tools = []

    cur = conn.cursor()
    # Session metadata (tolerant of minimal test schemas)
    try:
        row = cur.execute(
            "SELECT slug, title, time_created, time_updated, parent_id, task_type, directory FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        row = cur.execute(
            "SELECT slug, title, time_created, time_updated, parent_id, task_type FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row:
        keys = row.keys()
        stats["slug"] = row["slug"] or session_id[:12]
        stats["summary"] = (row["title"] or "")[:200]
        stats["started"] = _zcode_db_fmt_timestamp(row["time_created"])
        stats["ended"] = _zcode_db_fmt_timestamp(row["time_updated"])
        parent_id = row["parent_id"] if "parent_id" in keys else None
        task_type = (row["task_type"] if "task_type" in keys else "") or ""
        if parent_id or task_type == "subagent_child" or "subagent" in session_id:
            stats["session_role"] = "subagent"
        else:
            stats["session_role"] = "main"
        if "directory" in keys and row["directory"]:
            stats["project"] = os.path.basename(str(row["directory"]).rstrip("/"))

    # Batch-fetch all messages and all parts in 2 queries total (zero N+1)
    mrows = cur.execute(
        "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created ASC",
        (session_id,),
    ).fetchall()
    parts_by_msg = _zcode_db_fetch_parts(cur, session_id)

    for mrow in mrows:
        md = _zcode_db_parse_message_data(mrow["data"])
        msg_role = md.get("role", "unknown")
        ts = _zcode_db_fmt_timestamp(mrow["time_created"])
        if msg_role == "user":
            stats["user_messages"] += 1
            mapped_role = "USER"
        elif msg_role in ("assistant", "model"):
            stats["assistant_messages"] += 1
            mapped_role = "ASSISTANT"
        else:
            mapped_role = "ASSISTANT"

        text_parts = []
        for pd in parts_by_msg.get(mrow["id"], ()):
            ptype = pd.get("type")
            if ptype == "text":
                t = pd.get("text") or ""
                if t:
                    text_parts.append(t)
            elif ptype == "reasoning" and thinking_limit != -1:
                th = (pd.get("text") or "").strip()
                if th:
                    if thinking_limit > 0:
                        th = th[:thinking_limit]
                    text_parts.append(f"[THINKING] {th}")
            elif ptype == "compaction":
                stats["compactions"] += 1
            elif ptype == "tool":
                stats["tool_calls"] += 1
                tool_name = pd.get("tool", pd.get("name", "")) or "[tool]"
                if tool_name in _ZCODE_EDIT_TOOLS:
                    stats["files_edited"] += 1
                state = pd.get("state", pd.get("status", {}))
                is_err = isinstance(state, dict) and state.get("status") in ("error", "failure")
                if is_err:
                    stats["errors"] += 1
                inp = state.get("input", "") if isinstance(state, dict) else ""
                inp_str = json.dumps(inp, ensure_ascii=False)[:150] if isinstance(inp, dict) else str(inp or "")[:150]
                if is_err and isinstance(state, dict) and state.get("error"):
                    out_raw = state.get("error")
                else:
                    out_raw = state.get("output", "") if isinstance(state, dict) else ""
                if isinstance(out_raw, str):
                    res_prev = out_raw[:400].replace("\n", " ")
                elif isinstance(out_raw, dict):
                    res_prev = json.dumps(out_raw, ensure_ascii=False)[:400]
                else:
                    res_prev = ""
                tools.append({
                    "timestamp": ts,
                    "name": tool_name,
                    "status": "error" if is_err else "ok",
                    "key_input": inp_str,
                    "result_preview": res_prev,
                })

        joined = "\n".join(text_parts).strip()
        if joined:
            messages.append({
                "role": mapped_role,
                "timestamp": ts,
                "text": joined[:16000],
            })

    # Token usage — prefer official model_usage
    usage = _zcode_fetch_model_usage([session_id])
    if usage:
        _zcode_apply_usage_payload(stats, usage)
    else:
        try:
            trow = cur.execute("""
                SELECT COALESCE(SUM(input_tokens), 0) as inp,
                       COALESCE(SUM(output_tokens), 0) as out
                FROM turn_usage WHERE session_id = ?
            """, (session_id,)).fetchone()
            if trow:
                stats["input_tokens"] = _as_int(trow["inp"])
                stats["output_tokens"] = _as_int(trow["out"])
        except sqlite3.Error:
            pass

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return {"stats": stats, "messages": messages, "tools": tools}


def zcode_db_session_stats(session_id):
    """Get stats for a ZCode session from SQLite."""
    from echolib._helpers import _empty_stats
    session_id = _zcode_db_normalize_session_id(session_id)
    conn = _zcode_db_connect()
    if not conn:
        return _empty_stats("zcode")

    stats = _empty_stats("zcode")
    stats["slug"] = session_id[:12]
    cur = conn.cursor()

    try:
        row = cur.execute(
            "SELECT slug, title, time_created, time_updated, parent_id, task_type, directory FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        row = cur.execute(
            "SELECT slug, title, time_created, time_updated FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row:
        keys = row.keys()
        stats["slug"] = row["slug"] or session_id[:12]
        stats["summary"] = (row["title"] or "")[:200]
        stats["started"] = _zcode_db_fmt_timestamp(row["time_created"])
        stats["ended"] = _zcode_db_fmt_timestamp(row["time_updated"])
        parent_id = row["parent_id"] if "parent_id" in keys else None
        task_type = (row["task_type"] if "task_type" in keys else "") or ""
        if parent_id or task_type == "subagent_child" or "subagent" in session_id:
            stats["session_role"] = "subagent"
        else:
            stats["session_role"] = "main"
        if "directory" in keys and row["directory"]:
            stats["project"] = os.path.basename(str(row["directory"]).rstrip("/"))

    cur.execute("""
        SELECT CASE WHEN json_valid(data) THEN json_extract(data, '$.role') END AS role,
               COUNT(*) AS cnt
        FROM message WHERE session_id = ?
        GROUP BY role
    """, (session_id,))
    user_count = 0
    assistant_count = 0
    for mrow in cur.fetchall():
        role = mrow["role"] or ""
        if role == "user":
            user_count += mrow["cnt"]
        elif role in ("assistant", "model"):
            assistant_count += mrow["cnt"]

    stats["user_messages"] = user_count
    stats["assistant_messages"] = assistant_count

    usage = _zcode_fetch_model_usage([session_id])
    if usage:
        _zcode_apply_usage_payload(stats, usage)
    else:
        cur.execute("""
            SELECT COALESCE(SUM(input_tokens), 0) as inp,
                   COALESCE(SUM(output_tokens), 0) as out
            FROM turn_usage WHERE session_id = ?
        """, (session_id,))
        row = cur.fetchone()
        if row:
            stats["input_tokens"] = _as_int(row["inp"])
            stats["output_tokens"] = _as_int(row["out"])
            stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]

    cur.execute("""
        SELECT COUNT(*) as cnt,
               SUM(CASE WHEN json_extract(p.data, '$.state.status') = 'error'
                        THEN 1 ELSE 0 END) as errs
        FROM part p
        JOIN message m ON m.id = p.message_id
        WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
    """, (session_id,))
    row = cur.fetchone()
    stats["tool_calls"] = (row["cnt"] or 0) if row else 0
    stats["errors"] = (row["errs"] or 0) if row else 0

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return stats


def _zcode_db_fetch_model_usage_by_session(conn, session_id):
    """Fetch per-model aggregated usage for one session from model_usage table.

    Returns dict keyed by normalized model_id, or empty dict.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT model_id,
               COUNT(*) AS model_calls,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_create_tokens,
               COALESCE(SUM(computed_total_tokens), 0) AS total_tokens
        FROM model_usage
        WHERE session_id = ?
        GROUP BY model_id
        """,
        (session_id,),
    )
    by_model = {}
    for row in cur.fetchall():
        mid = (row["model_id"] if row["model_id"] is not None else "") or "unknown"
        mid_n = normalize_model_name(str(mid))
        inp = int(row["input_tokens"] or 0)
        outp = int(row["output_tokens"] or 0)
        cache = int(row["cache_read_tokens"] or 0)
        create = int(row["cache_create_tokens"] or 0)
        total = int(row["total_tokens"] or 0) or (inp + outp)
        calls = int(row["model_calls"] or 0)
        if mid_n in by_model:
            e = by_model[mid_n]
            e["input_tokens"] += inp
            e["output_tokens"] += outp
            e["cache_read_tokens"] += cache
            e["cache_create_tokens"] += create
            e["total_tokens"] += total
            e["model_calls"] += calls
            e["cache_hit_rate"] = compute_cache_hit_rate(
                e["input_tokens"], e["cache_read_tokens"]
            )
        else:
            by_model[mid_n] = {
                "input_tokens": inp,
                "output_tokens": outp,
                "cache_read_tokens": cache,
                "cache_create_tokens": create,
                "total_tokens": total,
                "model_calls": calls,
                "cache_hit_rate": compute_cache_hit_rate(inp, cache),
            }
    return by_model


def _zcode_db_sum_model_maps(model_maps):
    """Merge multiple per-model maps by summing counters."""
    merged = {}
    for mu in model_maps:
        if not isinstance(mu, dict):
            continue
        for mid, leg in mu.items():
            if mid in merged:
                e = merged[mid]
                for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                           "cache_create_tokens", "total_tokens", "model_calls"):
                    e[k] += int(leg.get(k) or 0)
                e["cache_hit_rate"] = compute_cache_hit_rate(
                    e["input_tokens"], e["cache_read_tokens"]
                )
            else:
                merged[mid] = dict(leg)
    return merged


def _zcode_db_model_map_totals(mu):
    """Sum all counters in a per-model map into a single bucket."""
    bucket = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "total_tokens": 0, "model_calls": 0,
    }
    if not isinstance(mu, dict):
        return bucket
    for leg in mu.values():
        if not isinstance(leg, dict):
            continue
        for k in bucket:
            bucket[k] += int(leg.get(k) or 0)
    bucket["cache_hit_rate"] = compute_cache_hit_rate(
        bucket["input_tokens"], bucket["cache_read_tokens"]
    )
    return bucket


def zcode_aggregate_model_usage(session_ids=None, limit=200, mode="family"):
    """Per-model billable totals from official ``model_usage`` table.

    *mode*:
      - ``family`` (default, recommended):
          Parent sessions with children are counted once (parent + children
          merged). Child-only sessions are skipped. Standalone sessions count
          as themselves.
      - ``session``: Each session counted individually; child sessions skipped
          (may under-count separate families).
      - ``raw``: Every session counted, allowing parent+child double-count
          (debugging only).

    *session_ids*: Optional list of session IDs to scope to. None = all.

    Returns: {model_id: {input_tokens, output_tokens, cache_read_tokens,
                         cache_create_tokens, total_tokens, model_calls, sessions}}
    """
    if mode not in ("family", "session", "raw"):
        mode = "family"

    conn = _zcode_db_connect()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_usage'"
        )
        if not cur.fetchone():
            return {}

        # Check if session table exists (needed for family dedup)
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session'"
        )
        has_session_table = cur.fetchone() is not None

        # Build child→parent mapping from session table
        child_to_parent = {}
        parents_with_children = set()
        if has_session_table:
            try:
                cur.execute(
                    "SELECT id, parent_id FROM session WHERE parent_id IS NOT NULL"
                )
                for row in cur.fetchall():
                    sid = str(row["id"])
                    pid = str(row["parent_id"])
                    child_to_parent[sid] = pid
                    parents_with_children.add(pid)
            except sqlite3.Error:
                pass

        # Determine which sessions to iterate
        if session_ids:
            target_ids = set(session_ids)
        elif has_session_table:
            cur.execute("SELECT id FROM session")
            target_ids = {str(r["id"]) for r in cur.fetchall()}
        else:
            # No session table → fall back to raw mode (no family dedup possible)
            mode = "raw"
            target_ids = set()

        # Special case: no session table + no session_ids → global aggregation
        if not has_session_table and not session_ids:
            cur.execute(
                """
                SELECT model_id,
                       COUNT(*) AS model_calls,
                       COUNT(DISTINCT session_id) AS sessions,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_create_tokens,
                       COALESCE(SUM(computed_total_tokens), 0) AS total_tokens
                FROM model_usage
                GROUP BY model_id
                ORDER BY input_tokens DESC
                """
            )
            out = {}
            for row in cur.fetchall():
                mid = (row["model_id"] if row["model_id"] is not None else "") or "unknown"
                mid_n = normalize_model_name(str(mid))
                inp = int(row["input_tokens"] or 0)
                outp = int(row["output_tokens"] or 0)
                total = int(row["total_tokens"] or 0) or (inp + outp)
                cache = int(row["cache_read_tokens"] or 0)
                create = int(row["cache_create_tokens"] or 0)
                calls = int(row["model_calls"] or 0)
                sessions = int(row["sessions"] or 0)
                if mid_n in out:
                    e = out[mid_n]
                    e["input_tokens"] += inp
                    e["output_tokens"] += outp
                    e["cache_read_tokens"] += cache
                    e["cache_create_tokens"] += create
                    e["total_tokens"] += total
                    e["model_calls"] += calls
                    e["sessions"] += sessions
                    e["cache_hit_rate"] = compute_cache_hit_rate(
                        e["input_tokens"], e["cache_read_tokens"]
                    )
                else:
                    out[mid_n] = {
                        "input_tokens": inp,
                        "output_tokens": outp,
                        "cache_read_tokens": cache,
                        "cache_create_tokens": create,
                        "total_tokens": total,
                        "model_calls": calls,
                        "sessions": sessions,
                        "cache_hit_rate": compute_cache_hit_rate(inp, cache),
                    }
            return out

        totals = {}

        def _add_model_map(mu, sessions_inc=1):
            if not mu:
                return
            for mid, leg in mu.items():
                bucket = totals.setdefault(mid, {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_create_tokens": 0,
                    "total_tokens": 0, "model_calls": 0, "sessions": 0,
                })
                for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                           "cache_create_tokens", "total_tokens", "model_calls"):
                    bucket[k] += int(leg.get(k) or 0)
                bucket["sessions"] += sessions_inc
                bucket["cache_hit_rate"] = compute_cache_hit_rate(
                    bucket["input_tokens"], bucket["cache_read_tokens"]
                )

        if mode == "raw":
            for sid in target_ids:
                mu = _zcode_db_fetch_model_usage_by_session(conn, sid)
                _add_model_map(mu)
            return totals

        # mode == family or session: skip children whose parent is also in scope
        for sid in target_ids:
            parent_id = child_to_parent.get(sid)
            if parent_id and parent_id in target_ids:
                if mode in ("session", "family"):
                    continue  # skip child

            if mode == "session":
                mu = _zcode_db_fetch_model_usage_by_session(conn, sid)
                _add_model_map(mu)
                continue

            # mode == family: if parent, merge children's usage
            if sid in parents_with_children:
                mu = _zcode_db_fetch_model_usage_by_session(conn, sid)
                # Find children of this parent in scope
                child_mus = []
                for cid, cpid in child_to_parent.items():
                    if cpid == sid and cid in target_ids:
                        cmu = _zcode_db_fetch_model_usage_by_session(conn, cid)
                        child_mus.append(cmu)
                # Merge parent + all children
                all_mus = [mu] + child_mus
                merged = _zcode_db_sum_model_maps(all_mus)
                _add_model_map(merged)
                continue

            # standalone
            mu = _zcode_db_fetch_model_usage_by_session(conn, sid)
            _add_model_map(mu)

        return totals
    except sqlite3.Error:
        return {}


def zcode_family_usage_report(session_id):
    """主会话 vs 子代理 token 分账报告（账单级）。

    Data source: ZCode SQLite ``model_usage`` + ``session`` tables.

    Accounting mode detection (same logic as Grok):
      * If any child model m has input > parent's input for that model → separate
      * Otherwise → rollup (parent's usage already includes children)
      * No children → standalone

    Returns dict with:
      session_id, accounting, parent, children[], main_only, subagents_total,
      by_model_main, by_model_subagents, by_model_family, family_total
    """
    conn = _zcode_db_connect()
    if not conn:
        return {"session_id": session_id, "accounting": "standalone",
                "parent": {}, "children": [], "main_only": {},
                "subagents_total": {}, "family_total": {},
                "by_model_main": {}, "by_model_subagents": {},
                "by_model_family": {}, "subagent_count": 0}
    try:
        cur = conn.cursor()

        # 1. Fetch parent session info
        cur.execute(
            "SELECT id, title, slug, task_type FROM session WHERE id = ?",
            (session_id,),
        )
        parent_row = cur.fetchone()
        if not parent_row:
            return {"session_id": session_id, "accounting": "standalone",
                    "parent": {}, "children": [], "main_only": {},
                    "subagents_total": {}, "family_total": {},
                    "by_model_main": {}, "by_model_subagents": {},
                    "by_model_family": {}, "subagent_count": 0}

        # 2. Parent model usage
        parent_mu = _zcode_db_fetch_model_usage_by_session(conn, session_id)
        parent_bucket = _zcode_db_model_map_totals(parent_mu)
        parent_bucket["model"] = _zcode_db_primary_model(parent_mu)

        # 3. Find children
        cur.execute(
            "SELECT id, title, task_type FROM session WHERE parent_id = ?",
            (session_id,),
        )
        children = []
        child_mus = []
        sub_bucket = _zcode_db_model_map_totals({})

        for crow in cur.fetchall():
            cid = str(crow["id"])
            cmu = _zcode_db_fetch_model_usage_by_session(conn, cid)
            cb = _zcode_db_model_map_totals(cmu)
            cb["model"] = _zcode_db_primary_model(cmu)
            children.append({
                "session_id": cid,
                "title": (crow["title"] or cid)[:100],
                "task_type": crow["task_type"] or "",
                "model": cb["model"],
                "input_tokens": cb["input_tokens"],
                "output_tokens": cb["output_tokens"],
                "total_tokens": cb["total_tokens"],
                "by_model": cmu,
            })
            child_mus.append(cmu)
            for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                       "cache_create_tokens", "total_tokens", "model_calls"):
                sub_bucket[k] += cb[k]

        child_mu_sum = _zcode_db_sum_model_maps(child_mus)
        sub_bucket["cache_hit_rate"] = compute_cache_hit_rate(
            sub_bucket["input_tokens"], sub_bucket["cache_read_tokens"]
        )

        # 4. Determine accounting mode
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
            if not parent_mu and sub_bucket["input_tokens"] > 0:
                accounting = "separate"

        # 5. Compute main_only, by_model splits
        if accounting == "rollup" and children:
            # Parent already includes children → subtract to get main_only
            by_model_main = {}
            all_models = set(parent_mu) | set(child_mu_sum)
            for mid in all_models:
                p = parent_mu.get(mid) or {}
                c = child_mu_sum.get(mid) or {}
                leg = {}
                for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                           "cache_create_tokens", "total_tokens", "model_calls"):
                    leg[k] = max(0, int(p.get(k) or 0) - int(c.get(k) or 0))
                leg["cache_hit_rate"] = compute_cache_hit_rate(
                    leg["input_tokens"], leg["cache_read_tokens"]
                )
                if any(leg[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens")):
                    by_model_main[mid] = leg
            main_only = _zcode_db_model_map_totals(by_model_main)
            by_model_subagents = child_mu_sum
            by_model_family = parent_mu  # already family-wide under rollup
            family_total = parent_bucket
        else:
            # separate or standalone
            by_model_main = parent_mu
            main_only = parent_bucket
            by_model_subagents = child_mu_sum
            by_model_family = _zcode_db_sum_model_maps([parent_mu, child_mu_sum])
            family_total = _zcode_db_model_map_totals(by_model_family)

        return {
            "session_id": session_id,
            "accounting": accounting,
            "parent": {
                "model": parent_bucket["model"],
                "input_tokens": parent_bucket["input_tokens"],
                "output_tokens": parent_bucket["output_tokens"],
                "cache_read_tokens": parent_bucket["cache_read_tokens"],
                "cache_create_tokens": parent_bucket["cache_create_tokens"],
                "total_tokens": parent_bucket["total_tokens"],
                "model_calls": parent_bucket["model_calls"],
                "cache_hit_rate": parent_bucket["cache_hit_rate"],
                "by_model": parent_mu,
            },
            "children": children,
            "main_only": main_only,
            "subagents_total": sub_bucket,
            "family_total": family_total,
            "by_model_main": by_model_main,
            "by_model_subagents": by_model_subagents,
            "by_model_family": by_model_family,
            "subagent_count": len(children),
        }
    except sqlite3.Error:
        return {"session_id": session_id, "accounting": "error",
                "parent": {}, "children": [], "main_only": {},
                "subagents_total": {}, "family_total": {},
                "by_model_main": {}, "by_model_subagents": {},
                "by_model_family": {}, "subagent_count": 0}


def _zcode_db_primary_model(by_model):
    """Return the model_id with highest input_tokens from a by_model map."""
    best = ""
    best_in = -1
    if not isinstance(by_model, dict):
        return ""
    for mid, leg in by_model.items():
        if isinstance(leg, dict) and int(leg.get("input_tokens") or 0) > best_in:
            best_in = int(leg.get("input_tokens") or 0)
            best = mid
    return best


# ── P2: Tool usage + Turn performance analytics ──────────────────────


def _zcode_percentile(values, pct):
    """Compute percentile from a sorted list of values."""
    if not values:
        return 0
    idx = int(len(values) * pct / 100)
    idx = min(idx, len(values) - 1)
    return values[idx]


def zcode_tool_usage_stats(session_id=None, top_n=20):
    """工具调用性能统计，从 ``tool_usage`` 表聚合。

    *session_id*: 限定单个会话；None = 全局。

    Returns dict with:
      total_calls, error_count, tools: [{name, calls, errors, error_rate,
      avg_duration_ms, p50_duration_ms, p95_duration_ms, total_duration_ms,
      read_only_count, destructive_count}]
    """
    conn = _zcode_db_connect()
    if not conn:
        return {"total_calls": 0, "error_count": 0, "tools": []}
    try:
        cur = conn.cursor()
        # Check table exists
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_usage'"
        )
        if not cur.fetchone():
            return {"total_calls": 0, "error_count": 0, "tools": []}

        where = "WHERE session_id = ?" if session_id else ""
        params = (session_id,) if session_id else ()

        # Aggregate per tool
        cur.execute(
            f"""
            SELECT tool_name,
                   COUNT(*) AS calls,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                   AVG(duration_ms) AS avg_dur,
                   SUM(COALESCE(duration_ms, 0)) AS total_dur,
                   SUM(CASE WHEN read_only = 1 THEN 1 ELSE 0 END) AS ro_cnt,
                   SUM(CASE WHEN destructive = 1 THEN 1 ELSE 0 END) AS des_cnt
            FROM tool_usage
            {where}
            GROUP BY tool_name
            ORDER BY calls DESC
            LIMIT ?
            """,
            (*params, sql_limit(top_n)),
        )
        tools = []
        total_calls = 0
        total_errors = 0
        for row in cur.fetchall():
            name = row["tool_name"] or "unknown"
            calls = int(row["calls"] or 0)
            errors = int(row["errors"] or 0)
            total_calls += calls
            total_errors += errors

            # Percentiles from duration distribution
            pwhere = "WHERE tool_name = ?"
            pparams = [name]
            if session_id:
                pwhere += " AND session_id = ?"
                pparams.append(session_id)
            cur.execute(
                f"SELECT duration_ms FROM tool_usage {pwhere} "
                "AND duration_ms IS NOT NULL ORDER BY duration_ms",
                pparams,
            )
            durations = [int(r[0]) for r in cur.fetchall() if r[0] is not None]

            tools.append({
                "name": name,
                "calls": calls,
                "errors": errors,
                "error_rate": round(errors / calls, 4) if calls else 0,
                "avg_duration_ms": round(float(row["avg_dur"] or 0), 1),
                "p50_duration_ms": _zcode_percentile(durations, 50),
                "p95_duration_ms": _zcode_percentile(durations, 95),
                "total_duration_ms": int(row["total_dur"] or 0),
                "read_only_count": int(row["ro_cnt"] or 0),
                "destructive_count": int(row["des_cnt"] or 0),
            })

        return {
            "total_calls": total_calls,
            "error_count": total_errors,
            "tools": tools,
        }
    except sqlite3.Error:
        return {"total_calls": 0, "error_count": 0, "tools": []}


def zcode_turn_usage_stats(session_id=None, limit=100):
    """Turn 级性能统计，从 ``turn_usage`` 表聚合。

    *session_id*: 限定单个会话；None = 全局。
    *limit*: 返回的最大 turn 数。

    Returns dict with:
      total_turns, total_duration_ms, avg_tokens_per_turn,
      avg_tool_calls_per_turn, error_turns,
      turns: [{turn_id, session_id, input_tokens, output_tokens,
               reasoning_tokens, tool_call_count, tool_error_count,
               duration_ms, status}]
    """
    conn = _zcode_db_connect()
    if not conn:
        return {"total_turns": 0, "total_duration_ms": 0,
                "avg_tokens_per_turn": 0, "avg_tool_calls_per_turn": 0,
                "error_turns": 0, "turns": []}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_usage'"
        )
        if not cur.fetchone():
            return {"total_turns": 0, "total_duration_ms": 0,
                    "avg_tokens_per_turn": 0, "avg_tool_calls_per_turn": 0,
                    "error_turns": 0, "turns": []}

        where = "WHERE session_id = ?" if session_id else ""
        params = (session_id,) if session_id else ()

        # Aggregate stats
        cur.execute(
            f"""
            SELECT COUNT(*) AS total_turns,
                   COALESCE(SUM(duration_ms), 0) AS total_dur,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
                   COALESCE(SUM(tool_call_count), 0) AS total_tools,
                   SUM(CASE WHEN tool_error_count > 0 THEN 1 ELSE 0 END) AS error_turns
            FROM turn_usage
            {where}
            """,
            params,
        )
        agg = cur.fetchone()
        total_turns = int(agg["total_turns"] or 0)
        total_dur = int(agg["total_dur"] or 0)
        total_tokens = int(agg["total_tokens"] or 0)
        total_tools = int(agg["total_tools"] or 0)
        error_turns = int(agg["error_turns"] or 0)

        # Detailed turn list (most recent first)
        cur.execute(
            f"""
            SELECT turn_id, session_id, input_tokens, output_tokens,
                   reasoning_tokens, tool_call_count, tool_error_count,
                   duration_ms, status
            FROM turn_usage
            {where}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (*params, sql_limit(limit)),
        )
        turns = []
        for row in cur.fetchall():
            turns.append({
                "turn_id": row["turn_id"],
                "session_id": row["session_id"],
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "reasoning_tokens": int(row["reasoning_tokens"] or 0),
                "tool_call_count": int(row["tool_call_count"] or 0),
                "tool_error_count": int(row["tool_error_count"] or 0),
                "duration_ms": int(row["duration_ms"] or 0),
                "status": row["status"] or "",
            })

        return {
            "total_turns": total_turns,
            "total_duration_ms": total_dur,
            "avg_tokens_per_turn": round(total_tokens / total_turns) if total_turns else 0,
            "avg_tool_calls_per_turn": round(total_tools / total_turns, 1) if total_turns else 0,
            "error_turns": error_turns,
            "turns": turns,
        }
    except sqlite3.Error:
        return {"total_turns": 0, "total_duration_ms": 0,
                "avg_tokens_per_turn": 0, "avg_tool_calls_per_turn": 0,
                "error_turns": 0, "turns": []}


def zcode_db_extract_tools(session_id, tool_filter="", errors_only=False, limit=30):
    """Extract tool calls from a ZCode session via SQLite."""
    session_id = _zcode_db_normalize_session_id(session_id)
    conn = _zcode_db_connect()
    if not conn:
        return []

    tools = []
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.data, m.time_created
        FROM part p
        JOIN message m ON m.id = p.message_id
        WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
        ORDER BY p.id ASC
    """, (session_id,))

    for row in cur.fetchall():
        pd = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        if not isinstance(pd, dict):
            continue
        tool_name = pd.get("tool", pd.get("name", ""))
        state = pd.get("state", pd.get("status", {}))
        if isinstance(state, dict):
            status = "error" if state.get("status") in ("error", "failure") else "ok"
        else:
            status = "ok"

        if errors_only and status != "error":
            continue
        if tool_filter and tool_filter not in str(tool_name):
            continue

        inp = state.get("input", "") if isinstance(state, dict) else ""
        if isinstance(inp, dict):
            inp_str = json.dumps(inp, ensure_ascii=False)[:150]
        elif isinstance(inp, str):
            inp_str = inp[:150]
        else:
            inp_str = str(inp)[:150]

        if status == "error" and isinstance(state, dict) and state.get("error"):
            output = state.get("error")
        else:
            output = state.get("output", "") if isinstance(state, dict) else ""
        if isinstance(output, str):
            result_preview = output[:400].replace("\n", " ")
        elif isinstance(output, dict):
            result_preview = json.dumps(output, ensure_ascii=False)[:400]
        else:
            result_preview = ""

        ts = _zcode_db_fmt_timestamp(row["time_created"])
        tools.append({
            "timestamp": ts, "name": tool_name,
            "status": status, "key_input": inp_str,
            "result_preview": result_preview,
        })
        if limit and len(tools) >= limit:
            break

    return tools


def zcode_db_extract_messages(session_id, role="both", limit=5, thinking_limit=0):
    """Extract messages from a ZCode session via SQLite (batched, zero N+1)."""
    session_id = _zcode_db_normalize_session_id(session_id)
    conn = _zcode_db_connect()
    if not conn:
        return []

    messages = []
    cur = conn.cursor()
    cur.execute("""
        SELECT id, data, time_created FROM message
        WHERE session_id = ?
        ORDER BY time_created ASC
    """, (session_id,))
    mrows = cur.fetchall()
    parts_by_msg = _zcode_db_fetch_parts(cur, session_id)

    for mrow in mrows:
        md = _zcode_db_parse_message_data(mrow["data"])
        msg_role = md.get("role", "unknown")
        ts = _zcode_db_fmt_timestamp(mrow["time_created"])
        mapped_role = "USER" if msg_role == "user" else "ASSISTANT"

        if role == "user" and mapped_role != "USER":
            continue
        if role == "assistant" and mapped_role != "ASSISTANT":
            continue

        text_parts = []
        for pd in parts_by_msg.get(mrow["id"], ()):
            if pd.get("type") == "text":
                t = pd.get("text", "") or ""
                if t:
                    text_parts.append(t)

        text = "\n".join(text_parts)
        if not text.strip():
            continue

        messages.append({
            "role": mapped_role, "timestamp": ts,
            "text": text[:16000],
        })

        if limit and len(messages) >= limit:
            break

    return messages


# ── DIM & DimCode adapters (re-exported from _adapters_dimcode.py) ──
from echolib._adapters_dimcode import (  # noqa: E402
    _dimcode_db_connect,
    _dimcode_normalize_session_id,
    dim_extract_messages,
    dim_extract_tools,
    dim_list_sessions,
    dim_session_path,
    dim_session_stats,
    dimcode_extract_messages,
    dimcode_extract_tools,
    dimcode_list_sessions,
    dimcode_parse_session,
    dimcode_session_path,
    dimcode_session_stats,
    dimcode_skill_events,
)


# ── ZCode v2 (主会话库, Claude-Code 同构 transcript) ──────────────────
# ~/.zcode/v2/{agent-config,acp-config}/claude/<userhash>/projects/<slug>/<uuid>.jsonl
# 与 ~/.claude/projects/ 同构（parentUuid / type:user|assistant / message.content /
# cwd / gitBranch），解析直接复用 claude 适配器；这里只负责发现与归属。


def _zcode_v2_transcripts():
    """Yield every claude-format transcript under ~/.zcode/v2/."""
    for sub in ("agent-config", "acp-config"):
        root = ZCODE_V2_DIR / sub
        if not root.is_dir():
            continue
        try:
            yield from root.glob("*/*/projects/*/*.jsonl")
        except OSError:
            continue


def _zcode_v2_quick_scan(transcript, max_records=4000):
    """One-pass metadata for list_sessions: first prompt + user message count."""
    if discovery_only_active():
        return "", 0
    first_prompt = ""
    user_n = 0
    n = 0
    for rec in _iter_jsonl(transcript):
        n += 1
        if max_records and n > max_records:
            break
        if rec.get("type") != "user":
            continue
        if rec.get("isMeta") or rec.get("isCompactSummary"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            text = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
        if text and not text.startswith("<") and len(text) > 2:
            user_n += 1
            if not first_prompt:
                first_prompt = text[:200]
    return first_prompt, user_n


def zcode_v2_list_sessions(cwd=None, limit=50, keyword=""):
    """List ZCode v2 main-session transcripts (agent-config + acp-config)."""
    sessions = []
    keyword_l = keyword.lower() if keyword else ""
    for transcript in _zcode_v2_transcripts():
        sid = transcript.stem
        try:
            mtime = _normalize_timestamp(transcript.stat().st_mtime)
        except OSError:
            continue
        first_prompt, user_n = _zcode_v2_quick_scan(transcript)
        summary = first_prompt[:100] if first_prompt else f"ZCode v2 {sid[:12]}"
        if keyword_l and keyword_l not in summary.lower() and keyword_l not in transcript.stem.lower():
            continue
        sessions.append(SessionMeta(
            session_id=sid,
            full_path=str(transcript),
            created=mtime,
            modified=mtime,
            message_count=user_n,
            git_branch="",
            summary=summary,
            first_prompt=first_prompt,
            project_path=transcript.parent.name,
        ))
    sessions.sort(key=lambda s: str(s.modified or s.created or ""), reverse=True)
    return cap(sessions, limit)


def zcode_v2_session_stats(session_path):
    """Stats for a ZCode v2 transcript — claude-format, delegate to claude adapter."""
    from echolib._claude import session_stats as _claude_session_stats

    stats = _claude_session_stats(session_path)
    stats["slug"] = Path(session_path).stem
    # 委托路径与转写路径必须同一口径：PROVIDER_POLICY 说 zcode_v2 的 input
    # 含缓存腿，而 claude 的统计函数只按 claude 的口径盖 False。漏了这里，
    # 同一个会话走 dispatch 与走单遍聚合命中率差近一半（v0.9.30 只修了后者）。
    attach_cache_hit_rates(stats, input_includes_cache=True)
    return stats


def zcode_v2_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from a ZCode v2 transcript — claude-format delegate."""
    from echolib._claude import extract_messages as _claude_extract_messages

    yield from _claude_extract_messages(
        session_path, role=role, limit=limit, thinking_limit=thinking_limit
    )


def zcode_v2_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from a ZCode v2 transcript — claude-format delegate."""
    from echolib._claude import extract_tools as _claude_extract_tools

    yield from _claude_extract_tools(
        session_path, tool_filter=tool_filter, errors_only=errors_only, limit=limit
    )


def zcode_v2_session_path(cwd, session_id=None):
    """Resolve a ZCode v2 transcript by uuid stem (or newest overall)."""
    if session_id:
        wanted = str(session_id).split("/")[-1]
        for transcript in _zcode_v2_transcripts():
            if transcript.stem == wanted or transcript.stem.startswith(wanted):
                return str(transcript)
    newest = None
    newest_m = -1.0
    for transcript in _zcode_v2_transcripts():
        try:
            m = transcript.stat().st_mtime
        except OSError:
            continue
        if m > newest_m:
            newest_m = m
            newest = transcript
    return str(newest) if newest else str(ZCODE_V2_DIR)
