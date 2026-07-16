"""Adapters for ZCode / DIM / DimCode environments — extracted from _adapters.py.

All ``*_session_stats`` here rely on ``_empty_stats`` from the parent package
(`echolib._adapters`). Import is deferred inside the stat functions to keep the
package-level import order stable.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from echolib._claude import _normalize_timestamp
from echolib._helpers import (
    DIMCODE_DB_PATH,
    DIM_DIR,
    ZCODE_DIR,
    _extract_content_text,
    _iter_jsonl,
    _strip_system_reminder,
    attach_cache_hit_rates,
    compute_cache_hit_rate,
)
from echolib._models import SessionMeta


# ── ZCode (transcript.jsonl trace) ────────────────────────────────────
_ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


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
                inp = int(row["input_tokens"] or 0)
                out = int(row["output_tokens"] or 0)
                cache = int(row["cache_read_tokens"] or 0)
                create = int(row["cache_create_tokens"] or 0)
                calls = int(row["model_calls"] or 0)
                total = int(row["total_tokens"] or 0) or (inp + out)
                by_model[mid] = {
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
                    primary = mid

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
    finally:
        conn.close()


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
    attach_cache_hit_rates(stats)
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
    finally:
        conn.close()
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
            if limit and len(sessions) >= limit * 3:
                # collect extra then sort/truncate
                pass

    sessions.sort(key=lambda s: str(s.modified or s.created or ""), reverse=True)
    return sessions[:limit]


def zcode_session_stats(session_path):
    """Stats for ZCode transcript path.

    Tokens (preferred): official SQLite ``model_usage`` (per-request SUM, by model).
    Activity (messages/tools): still from transcript.jsonl when present.
    Fallback tokens: sum ``model_complete.usage`` on the transcript.
    """
    from echolib._adapters import _empty_stats
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
            # Prefer authoritative turn totals when present
            if isinstance(usage, dict) and usage.get("inputTokens"):
                stats["input_tokens"] = int(usage.get("inputTokens") or stats["input_tokens"])
                stats["output_tokens"] = int(usage.get("outputTokens") or stats["output_tokens"])
                stats["cache_read_tokens"] = int(
                    usage.get("cacheReadTokens") or stats["cache_read_tokens"]
                )

    # If model_complete never had text but streaming did, keep assistant_messages
    # from model_complete (already counted). text_delta_turns is diagnostic only.
    _ = text_delta_turns
    if not stats.get("total_tokens"):
        stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    if not stats["model"] or stats["model"] == "zcode":
        stats["model"] = (db_usage or {}).get("model") or "zcode"
    # Transcript-fallback path may lack rates until here
    attach_cache_hit_rates(stats)
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
            text = _zcode_input_text(payload.get("input", ""), max_len=500)
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
                    yield {"role": "ASSISTANT", "timestamp": nt or buf_ts, "text": text[:500]}
                    stream_emitted = True
                    count += 1
                    if limit and count >= limit:
                        return

        elif rtype == "model_complete":
            # Fallback when no streaming text (older traces / content-only complete)
            text = _zcode_content_text(payload.get("content"), max_len=500)
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
            try:
                for agent_dir in sess_dir.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    if agent_dir.name == session_id or session_id in agent_dir.name:
                        t = agent_dir / "transcript.jsonl"
                        if t.is_file():
                            return str(t)
            except OSError:
                continue
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
    """Connect to ZCode SQLite DB (read-only)."""
    if not _ZCODE_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{_ZCODE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.slug, s.title, s.task_type, s.time_created, s.time_updated,
                   s.parent_id,
                   COALESCE(tu.input_tokens, 0) as input_tokens,
                   COALESCE(tu.output_tokens, 0) as output_tokens,
                   (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) as msg_count
            FROM session s
            LEFT JOIN (
                SELECT session_id, SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens
                FROM turn_usage GROUP BY session_id
            ) tu ON tu.session_id = s.id
            WHERE s.task_type != 'subagent_child'
               OR s.task_type IS NULL
            ORDER BY s.time_created DESC
            LIMIT ?
        """, (limit * 2,))

        for row in cur.fetchall():
            sid = row["id"]
            title = row["title"] or row["slug"] or sid[:12]
            created = _zcode_db_fmt_timestamp(row["time_created"])
            modified = _zcode_db_fmt_timestamp(row["time_updated"])
            msg_count = row["msg_count"] or 0

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

            if len(sessions) >= limit:
                break
    finally:
        conn.close()

    return sessions


def zcode_db_session_stats(session_id):
    """Get stats for a ZCode session from SQLite."""
    from echolib._adapters import _empty_stats
    conn = _zcode_db_connect()
    if not conn:
        return _empty_stats("zcode")

    stats = _empty_stats("zcode")
    stats["slug"] = session_id[:12]

    try:
        cur = conn.cursor()

        # Session metadata
        cur.execute("SELECT slug, title, time_created, time_updated FROM session WHERE id = ?", (session_id,))
        row = cur.fetchone()
        if row:
            stats["slug"] = row["slug"] or session_id[:12]
            stats["started"] = _zcode_db_fmt_timestamp(row["time_created"])
            stats["ended"] = _zcode_db_fmt_timestamp(row["time_updated"])

        # Message counts by role
        cur.execute("SELECT data FROM message WHERE session_id = ? ORDER BY time_created", (session_id,))
        user_count = 0
        assistant_count = 0
        for mrow in cur.fetchall():
            md = _zcode_db_parse_message_data(mrow["data"])
            role = md.get("role", "")
            if role == "user":
                user_count += 1
            elif role in ("assistant", "model"):
                assistant_count += 1

        stats["user_messages"] = user_count
        stats["assistant_messages"] = assistant_count

        # Token usage — prefer official model_usage (per-request, by model)
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
                stats["input_tokens"] = row["inp"]
                stats["output_tokens"] = row["out"]
                stats["total_tokens"] = row["inp"] + row["out"]

        # Tool calls
        cur.execute("""
            SELECT COUNT(*) as cnt FROM part p
            JOIN message m ON m.id = p.message_id
            WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
        """, (session_id,))
        row = cur.fetchone()
        stats["tool_calls"] = row["cnt"] if row else 0

        # Errors
        cur.execute("""
            SELECT COUNT(*) as cnt FROM part p
            JOIN message m ON m.id = p.message_id
            WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
              AND json_extract(p.data, '$.state.status') = 'error'
        """, (session_id,))
        row = cur.fetchone()
        stats["errors"] = row["cnt"] if row else 0

    finally:
        conn.close()

    return stats


def zcode_aggregate_model_usage():
    """Global per-model billable totals from official ``model_usage`` table.

    One row = one model call; SUM is exact (no parent/child rollup double-count
    in this table — subagents use distinct session_id values).

    Returns: {model_id: {input_tokens, output_tokens, cache_read_tokens,
                         cache_create_tokens, total_tokens, model_calls, sessions}}
    """
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
            inp = int(row["input_tokens"] or 0)
            outp = int(row["output_tokens"] or 0)
            total = int(row["total_tokens"] or 0) or (inp + outp)
            cache = int(row["cache_read_tokens"] or 0)
            out[str(mid)] = {
                "input_tokens": inp,
                "output_tokens": outp,
                "cache_read_tokens": cache,
                "cache_create_tokens": int(row["cache_create_tokens"] or 0),
                "total_tokens": total,
                "model_calls": int(row["model_calls"] or 0),
                "sessions": int(row["sessions"] or 0),
                "cache_hit_rate": compute_cache_hit_rate(inp, cache),
            }
        return out
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def zcode_db_extract_tools(session_id, limit=30):
    """Extract tool calls from a ZCode session via SQLite."""
    conn = _zcode_db_connect()
    if not conn:
        return []

    tools = []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.data, m.time_created
            FROM part p
            JOIN message m ON m.id = p.message_id
            WHERE m.session_id = ? AND json_extract(p.data, '$.type') = 'tool'
            ORDER BY p.id ASC
            LIMIT ?
        """, (session_id, limit))

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

            inp = state.get("input", "") if isinstance(state, dict) else ""
            if isinstance(inp, dict):
                inp_str = json.dumps(inp, ensure_ascii=False)[:150]
            elif isinstance(inp, str):
                inp_str = inp[:150]
            else:
                inp_str = str(inp)[:150]

            output = state.get("output", "") if isinstance(state, dict) else ""
            if isinstance(output, str):
                result_preview = output[:150].replace("\n", " ")
            elif isinstance(output, dict):
                result_preview = json.dumps(output, ensure_ascii=False)[:150]
            else:
                result_preview = ""

            ts = _zcode_db_fmt_timestamp(row["time_created"])
            tools.append({
                "timestamp": ts, "name": tool_name,
                "status": status, "key_input": inp_str,
                "result_preview": result_preview,
            })
    finally:
        conn.close()

    return tools


def zcode_db_extract_messages(session_id, role="both", limit=5):
    """Extract messages from a ZCode session via SQLite."""
    conn = _zcode_db_connect()
    if not conn:
        return []

    messages = []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, data, time_created FROM message
            WHERE session_id = ?
            ORDER BY time_created ASC
        """, (session_id,))

        for mrow in cur.fetchall():
            md = _zcode_db_parse_message_data(mrow["data"])
            msg_role = md.get("role", "unknown")
            ts = _zcode_db_fmt_timestamp(mrow["time_created"])
            mapped_role = "USER" if msg_role == "user" else "ASSISTANT"

            if role == "user" and mapped_role != "USER":
                continue
            if role == "assistant" and mapped_role != "ASSISTANT":
                continue

            cur2 = conn.cursor()
            cur2.execute("""
                SELECT p.data FROM part p
                WHERE p.message_id = ? AND json_extract(p.data, '$.type') = 'text'
                ORDER BY p.id ASC LIMIT 5
            """, (mrow["id"],))
            text_parts = []
            for part_row in cur2.fetchall():
                pd = json.loads(part_row["data"]) if isinstance(part_row["data"], str) else part_row["data"]
                if isinstance(pd, dict):
                    text_parts.append(pd.get("text", "") or "")
                elif isinstance(pd, str):
                    text_parts.append(pd)

            text = "\n".join(text_parts)
            if not text.strip():
                continue

            messages.append({
                "role": mapped_role, "timestamp": ts,
                "text": text[:2000],
            })

            if limit and len(messages) >= limit:
                break
    finally:
        conn.close()

    return messages


# ── DIM (memory-summary) ─────────────────────────────────────────────
def dim_list_sessions(cwd=None, limit=50, keyword=""):
    """List DIM memory sessions from ~/.dim/memory/."""
    sessions = []
    if not DIM_DIR.exists():
        return sessions
    for mem_dir in sorted(DIM_DIR.iterdir(), reverse=True):
        if not mem_dir.is_dir():
            continue
        for date_dir in sorted(mem_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for jf in date_dir.glob("*.jsonl"):
                try:
                    # backfill.jsonl: each record is a separate session
                    if jf.name == "backfill.jsonl":
                        for idx, rec in enumerate(_iter_jsonl(jf)):
                            st_val = rec.get("session_time", rec.get("timestamp", ""))
                            started = _normalize_timestamp(st_val) if st_val else ""
                            intent = str(rec.get("intent", ""))[:80] if rec.get("intent") else f"backfill #{idx+1}"
                            sessions.append({
                                "id": f"{jf.stem}_{idx:03d}",
                                "title": intent,
                                "created": started, "modified": "",
                                "message_count": 0, "path": str(jf),
                                "agent": "DIM", "model": "",
                            })
                    else:
                        started = ""
                        intent = ""
                        for rec in _iter_jsonl(jf):
                            st_val = rec.get("session_time", rec.get("timestamp", ""))
                            if st_val and not started:
                                started = _normalize_timestamp(st_val)
                            if rec.get("intent") and not intent:
                                intent = str(rec["intent"])[:80]
                            if started and intent:
                                break
                        sessions.append({
                            "id": jf.stem,
                            "title": intent or f"DIM {jf.stem[:20]}",
                            "created": started, "modified": "",
                            "message_count": 0, "path": str(jf),
                            "agent": "DIM", "model": "",
                        })
                except OSError:
                    continue
    if keyword:
        keyword_lower = keyword.lower()
        sessions = [s for s in sessions if keyword_lower in s.get("title", "").lower()]
    return sessions[:limit]


def dim_session_stats(session_path):
    """Stats for DIM memory-summary format."""
    from echolib._adapters import _empty_stats
    p = Path(session_path)
    stats = _empty_stats("dim")
    stats["slug"] = p.stem
    if not p.exists() or not p.is_file():
        return stats
    for rec in _iter_jsonl(p):
        ts = rec.get("session_time", rec.get("timestamp", ""))
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not stats["started"] or nts < stats["started"]:
                    stats["started"] = nts
                if nts > stats["ended"]:
                    stats["ended"] = nts
        if rec.get("intent"):
            stats["user_messages"] += 1
        if rec.get("learned") or rec.get("outcome"):
            stats["assistant_messages"] += 1
        actions = rec.get("actions", [])
        if isinstance(actions, list):
            stats["tool_calls"] += len(actions)
        if rec.get("model_perf"):
            mp = rec["model_perf"]
            if isinstance(mp, dict) and mp.get("model"):
                stats["model"] = str(mp["model"])
        if rec.get("intent") and not stats["summary"]:
            stats["summary"] = str(rec["intent"])[:200]
    return stats


def dim_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from DIM memory-summary format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        ts = rec.get("session_time", rec.get("timestamp", ""))
        nt = _normalize_timestamp(ts) if ts else ""
        if role in ("user", "both") and rec.get("intent"):
            text = str(rec["intent"])
            actions = rec.get("actions", [])
            if isinstance(actions, list) and actions:
                text += "\n[Actions: " + ", ".join(str(a.get("name", a) if isinstance(a, dict) else a)[:30] for a in actions[:5]) + "]"
            yield {"role": "USER", "timestamp": nt, "text": text[:500]}
            count += 1
            if limit and count >= limit:
                return
            continue
        if role in ("assistant", "both") and (rec.get("learned") or rec.get("outcome")):
            parts = []
            if rec.get("learned"):
                parts.append("[Learned] " + str(rec["learned"])[:200])
            if rec.get("outcome"):
                parts.append("[Outcome] " + str(rec["outcome"])[:200])
            yield {"role": "ASSISTANT", "timestamp": nt, "text": "\n".join(parts)[:500]}
            count += 1
            if limit and count >= limit:
                return


def dim_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Extract tool-like actions from DIM memory format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        actions = rec.get("actions", [])
        if not isinstance(actions, list):
            continue
        ts = rec.get("session_time", rec.get("timestamp", ""))
        nt = _normalize_timestamp(ts) if ts else ""
        for action in actions:
            # DIM backfill often stores plain strings; live notes may use dicts.
            if isinstance(action, str):
                name = action.strip()[:120] or "action"
                if tool_filter and name != tool_filter:
                    continue
                if errors_only:
                    continue
                yield {
                    "timestamp": nt,
                    "name": name[:80],
                    "status": "ok",
                    "key_input": name[:150],
                    "result_preview": "",
                }
                count += 1
                if limit and count >= limit:
                    return
                continue
            if not isinstance(action, dict):
                continue
            name = action.get("name", action.get("type", "action"))
            if tool_filter and name != tool_filter:
                continue
            if errors_only and not action.get("error"):
                continue
            status = "error" if action.get("error") else "ok"
            yield {"timestamp": nt, "name": str(name), "status": status,
                   "key_input": str(action.get("input", action.get("args", "")))[:150],
                   "result_preview": str(action.get("result", ""))[:80]}
            count += 1
            if limit and count >= limit:
                return


def dim_session_path(cwd, session_id=None):
    """Resolve DIM session path."""
    if session_id:
        # Check if session_id is already a full path
        p = Path(session_id)
        if p.is_file():
            return str(p)
        # Search for a matching file
        for mem_dir in DIM_DIR.iterdir():
            if not mem_dir.is_dir():
                continue
            for date_dir in mem_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                for jf in date_dir.glob(f"*{session_id}*.jsonl"):
                    return str(jf)
    return str(DIM_DIR)


# ── DimCode (SQLite) ────────────────────────────────────────────────
def _dimcode_db_connect():
    """Connect to DimCode SQLite DB (read-only)."""
    if not DIMCODE_DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{DIMCODE_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def dimcode_list_sessions(cwd=None, limit=50, keyword=""):
    """List DimCode sessions from dimcode.sqlite."""
    conn = _dimcode_db_connect()
    if not conn:
        return []
    sessions = []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.sessionId, s.title, s.cwd, s.createdAt, s.status,
                   (SELECT COUNT(*) FROM messages m WHERE m.sessionId = s.sessionId) as msg_count
            FROM sessions s
            ORDER BY s.createdAt DESC
            LIMIT ?
        """, (limit * 2,))
        for row in cur.fetchall():
            sid = row["sessionId"]
            title = row["title"] or sid[:20]
            created = row["createdAt"] or ""
            msg_count = row["msg_count"] or 0
            summary = title[:100]
            if keyword:
                haystack = f"{summary} {title}".lower()
                if keyword.lower() not in haystack:
                    continue
            sessions.append({
                "id": sid, "title": title,
                "created": created, "modified": "",
                "message_count": msg_count, "path": f"dimcode://{sid}",
                "agent": "DimCode", "model": "",
            })
            if len(sessions) >= limit:
                break
    finally:
        conn.close()
    return sessions


def _dimcode_normalize_session_id(session_id):
    """Accept dimcode://URI, dimcode:id, or bare sessionId."""
    s = str(session_id or "")
    if s.startswith("dimcode://"):
        return s[len("dimcode://"):]
    if s.startswith("dimcode:"):
        return s.split(":", 1)[1]
    # index ids look like dimcode:sess_xxx
    if s.startswith("dimcode:") is False and "/" not in s and s.startswith("sess_"):
        return s
    if ":" in s and s.split(":", 1)[0] in ("dimcode", "dim"):
        return s.split(":", 1)[1]
    return s


def dimcode_session_stats(session_id):
    """Get stats for a DimCode session from SQLite."""
    session_id = _dimcode_normalize_session_id(session_id)
    conn = _dimcode_db_connect()
    if not conn:
        return {}
    stats = {"slug": "", "model": "dimcode", "started": "", "ended": "",
             "user_messages": 0, "assistant_messages": 0, "tool_calls": 0,
             "errors": 0, "input_tokens": 0, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_create_tokens": 0,
             "total_tokens": 0, "summary": ""}
    try:
        cur = conn.cursor()
        cur.execute("SELECT title, createdAt, updatedAt FROM sessions WHERE sessionId = ?", (session_id,))
        row = cur.fetchone()
        if row:
            stats["slug"] = session_id[:20]
            stats["summary"] = (row["title"] or "")[:200]
            stats["started"] = row["createdAt"] or ""
            # updatedAt if column exists
            try:
                stats["ended"] = row["updatedAt"] or ""
            except (IndexError, KeyError, TypeError):
                stats["ended"] = ""
        # Count messages by role + last message time as ended
        cur.execute("SELECT role, COUNT(*) as cnt FROM messages WHERE sessionId = ? GROUP BY role", (session_id,))
        for r in cur.fetchall():
            if r["role"] == "user":
                stats["user_messages"] = r["cnt"]
            elif r["role"] == "assistant":
                stats["assistant_messages"] = r["cnt"]
            elif r["role"] in ("tool", "function"):
                stats["tool_calls"] += r["cnt"]
        cur.execute("SELECT MAX(createdAt) as t1 FROM messages WHERE sessionId = ?", (session_id,))
        r2 = cur.fetchone()
        if r2 and r2["t1"] and not stats.get("ended"):
            stats["ended"] = r2["t1"]
        # Token usage
        cur.execute("""
            SELECT COALESCE(SUM(inputTokens), 0) as inp,
                   COALESCE(SUM(outputTokens), 0) as out
            FROM usage_run_stats WHERE sessionId = ?
        """, (session_id,))
        row = cur.fetchone()
        if row:
            stats["input_tokens"] = row["inp"]
            stats["output_tokens"] = row["out"]
            stats["total_tokens"] = row["inp"] + row["out"]
        # Cache tokens (may not exist in older schemas)
        try:
            cur.execute("""
                SELECT COALESCE(SUM(cacheReadTokens), 0) as cr,
                       COALESCE(SUM(cacheCreationTokens), 0) as cc
                FROM usage_run_stats WHERE sessionId = ?
            """, (session_id,))
            crow = cur.fetchone()
            if crow:
                stats["cache_read_tokens"] = crow["cr"]
                stats["cache_create_tokens"] = crow["cc"]
        except Exception:
            pass
    except Exception:
        pass
    finally:
        conn.close()
    return stats


def dimcode_extract_messages(session_id, role="both", limit=0, thinking_limit=0):
    """Extract messages from a DimCode session via SQLite."""
    session_id = _dimcode_normalize_session_id(session_id)
    conn = _dimcode_db_connect()
    if not conn:
        return
    count = 0
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT messageId, role, parts, createdAt
            FROM messages
            WHERE sessionId = ?
            ORDER BY orderKey ASC
        """, (session_id,))
        for row in cur.fetchall():
            msg_role = row["role"].upper() if row["role"] else "USER"
            if role not in ("both", msg_role.lower(), msg_role):
                continue
            ts = row["createdAt"] or ""
            parts = row["parts"]
            text = ""
            if parts:
                try:
                    parsed = json.loads(parts)
                    if isinstance(parsed, list):
                        texts = []
                        for p in parsed:
                            if isinstance(p, dict) and p.get("type") == "text":
                                texts.append(p.get("text", ""))
                            elif isinstance(p, dict) and p.get("type") == "tool_call":
                                fn = p.get("function", {}).get("name", "?")
                                texts.append(f"[TOOL: {fn}]")
                        text = "\n".join(texts)
                    elif isinstance(parsed, dict) and parsed.get("type") == "text":
                        text = parsed.get("text", "")
                except (json.JSONDecodeError, ValueError):
                    text = str(parts)[:200]
            if not text:
                continue
            yield {"role": msg_role, "timestamp": ts, "text": text[:500]}
            count += 1
            if limit and count >= limit:
                return
    finally:
        conn.close()


def dimcode_extract_tools(session_id, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from DimCode session via SQLite."""
    session_id = _dimcode_normalize_session_id(session_id)
    conn = _dimcode_db_connect()
    if not conn:
        return
    count = 0
    try:
        cur = conn.cursor()
        # Look for assistant messages with tool_call parts
        cur.execute("""
            SELECT messageId, parts, createdAt
            FROM messages
            WHERE sessionId = ? AND role = 'assistant'
            ORDER BY orderKey ASC
        """, (session_id,))
        for row in cur.fetchall():
            parts = row["parts"]
            if not parts:
                continue
            try:
                parsed = json.loads(parts)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(parsed, list):
                continue
            for p in parsed:
                if not isinstance(p, dict) or p.get("type") != "tool_call":
                    continue
                name = p.get("function", {}).get("name", "")
                if tool_filter and name != tool_filter:
                    continue
                ts = row["createdAt"] or ""
                args = p.get("function", {}).get("arguments", "")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                if errors_only:
                    # DimCode doesn't expose error status in parts-based format
                    continue
                yield {"timestamp": ts, "name": name or "[tool]", "status": "ok",
                       "key_input": str(args)[:150] if args else "", "result_preview": ""}
                count += 1
                if limit and count >= limit:
                    return
    finally:
        conn.close()


def dimcode_session_path(cwd, session_id=None):
    """Resolve DimCode session identifier."""
    if session_id and session_id.startswith("dimcode://"):
        return session_id
    return f"dimcode://{session_id}" if session_id else str(DIMCODE_DB_PATH)
