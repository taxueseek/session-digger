"""Index building engine — scan, fingerprint, compute, insert."""
import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import echolib

from echolib._contracts import SessionStats
from echolib._helpers import _extract_content_text  # shared content-block unpacker
from index_builder._schema import DB_DIR, DB_PATH, init_db


def _dispatch_session_stats(path):
    """Get stats via echolib's unified adapter dispatch."""
    path_str = str(path)
    if path_str.startswith("dimcode://") or path_str.startswith("dimcode:"):
        return echolib.dimcode_session_stats(path_str)
    return echolib.dispatch_session_stats(path)


def _dispatch_extract_messages(path, role="both", limit=0):
    """Extract messages via echolib's unified adapter dispatch."""
    path_str = str(path)
    if path_str.startswith("dimcode://") or path_str.startswith("dimcode:"):
        return echolib.dimcode_extract_messages(path_str, role=role, limit=limit)
    return echolib.dispatch_extract_messages(path, role=role, limit=limit)


_GENERIC_MODELS = {
    "", "claude", "codex", "kimi", "zcode", "dimcode", "grok", "unknown",
    "<synthetic>", "openai-custom", "workbuddy", "dim", "reasonix", "trae_cn",
    "trae-cn (summary only)", "trae-cn", "universal",
}


# 单一真源：模型名归一
from echolib._models import normalize_model_name, MODEL_ALIASES as _MODEL_ALIASES


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
                        tok = (
                            usage.get("total_tokens")
                            or (
                                (usage.get("input_tokens") or 0)
                                + (usage.get("output_tokens") or 0)
                            )
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
                            tok = (usage.get("inputTokens") or 0) + (usage.get("outputTokens") or 0)
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
                            tok = total_u.get("total_tokens") or (
                                (total_u.get("input_tokens") or 0)
                                + (total_u.get("output_tokens") or 0)
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
                        tok = (in_t or 0) + (out_t or 0) + (cache or 0)
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
        pass

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
    tokens = int(stats.get("total_tokens") or 0)
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


def _file_fingerprint(jsonl_path):
    """Triple fingerprint: mtime + size + head hash."""
    path_str = str(jsonl_path)
    if path_str.startswith("dimcode://") or path_str.startswith("dimcode:"):
        sid = path_str.split("://", 1)[-1] if "://" in path_str else path_str.split(":", 1)[-1]
        db = Path(os.path.expanduser("~/.dimcode/v2/dimcode.sqlite"))
        try:
            st = db.stat()
            content_hash = hashlib.md5(f"{st.st_mtime}:{st.st_size}:{sid}".encode()).hexdigest()
            return st.st_mtime, content_hash
        except OSError:
            return None, None
    if "://" in path_str and not path_str.startswith("file:"):
        content_hash = hashlib.md5(path_str.encode()).hexdigest()
        return 0.0, content_hash
    try:
        st = os.stat(jsonl_path)
        size = st.st_size
        mtime = st.st_mtime
        with open(jsonl_path, "rb") as f:
            head = f.read(4096)
        content_hash = hashlib.md5(f"{size}:{head}".encode()).hexdigest()
        return mtime, content_hash
    except OSError:
        return None, None


def scan_sessions(agent_filter="cross"):
    """Yield (session_id, jsonl_path, agent_type) tuples."""
    entries = []
    seen_ids: set = set()
    all_envs = {}
    all_envs.update(echolib.ENV_REGISTRY)
    all_envs.update(echolib.KNOWN_UNADAPTED)
    for env_id, env_info in all_envs.items():
        if agent_filter not in ("cross", env_id, "all"):
            continue
        root = Path(os.path.expanduser(env_info["root"]))
        if not root.exists():
            continue
        adapter_name = env_info.get("adapter", "universal")
        fmt = (env_info.get("format") or "").lower()
        if fmt == "sqlite" or root.is_file():
            for item in _scan_via_adapter(adapter_name, env_id):
                sid, path, agent = item
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                entries.append(item)
            continue
        try:
            jsonl_files = _find_jsonl_files(root, env_id)
        except Exception:
            continue
        for jf in jsonl_files:
            if "subagents" in str(jf):
                continue
            if ".jsonl." in jf.name:
                continue
            if jf.name.endswith(".events.jsonl"):
                continue
            if jf.name == "backfill.jsonl":
                continue
            session_id = _generate_session_id(jf, root, env_id)
            if session_id in seen_ids:
                tail = hashlib.sha1(str(jf).encode()).hexdigest()[:10]
                session_id = f"{session_id}:{tail}"
            seen_ids.add(session_id)
            entries.append((session_id, str(jf), adapter_name))
    return entries


def _scan_via_adapter(adapter_name, env_id, limit=50000):
    """List sessions through a registered adapter (for SQLite / remote stores)."""
    adapter = echolib.ADAPTER_REGISTRY.get(adapter_name)
    if not adapter or not adapter.get("list_sessions"):
        return []
    try:
        sessions = adapter["list_sessions"](limit=limit) or []
    except Exception:
        return []
    out = []
    for s in sessions:
        if isinstance(s, dict):
            raw_id = s.get("session_id") or s.get("id") or ""
            path = s.get("full_path") or s.get("path") or s.get("jsonl_path") or ""
        else:
            raw_id = getattr(s, "session_id", None) or getattr(s, "id", "") or ""
            path = getattr(s, "full_path", None) or getattr(s, "path", "") or ""
        if not raw_id and not path:
            continue
        if not path:
            path = f"{adapter_name}://{raw_id}"
        sid = f"{env_id}:{raw_id}" if raw_id else _generate_session_id(Path(path), Path("/"), env_id)
        out.append((sid, str(path), adapter_name))
    return out


def _find_jsonl_files(root, env_id):
    """Find JSONL files for a specific environment."""
    jsonl_files = []
    if env_id == "claude":
        for d in root.iterdir():
            if d.is_dir():
                for jf in d.glob("*.jsonl"):
                    jsonl_files.append(jf)
    elif env_id == "grok":
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                chat_file = session_dir / "chat_history.jsonl"
                if chat_file.exists():
                    jsonl_files.append(chat_file)
    elif env_id == "kimi_code":
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                wire_file = session_dir / "agents" / "main" / "wire.jsonl"
                if wire_file.exists():
                    jsonl_files.append(wire_file)
    elif env_id == "zcode":
        for sess_dir in root.iterdir():
            if not sess_dir.is_dir() or not sess_dir.name.startswith("sess_"):
                continue
            for agent_dir in sess_dir.iterdir():
                if not agent_dir.is_dir():
                    continue
                transcript = agent_dir / "transcript.jsonl"
                if transcript.exists():
                    jsonl_files.append(transcript)
    elif env_id == "dim":
        for mem_dir in root.iterdir():
            if not mem_dir.is_dir():
                continue
            for date_dir in mem_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                for jf in date_dir.glob("*.jsonl"):
                    jsonl_files.append(jf)
    else:
        jsonl_files = list(root.rglob("*.jsonl"))
    return jsonl_files


def _generate_session_id(jsonl_path, root, env_id):
    """Generate a unique session ID from a JSONL file path."""
    jsonl_path = Path(jsonl_path)
    if env_id == "kimi_code":
        session_dir = jsonl_path.parents[2] if len(jsonl_path.parents) >= 3 else jsonl_path.parent
        base = session_dir.name.replace("session_", "")
        try:
            project = session_dir.parent.name
            if project and project not in base:
                base = f"{project}/{base}"
        except Exception:
            pass
    elif env_id == "grok":
        base = jsonl_path.parent.name
    elif env_id == "zcode":
        base = jsonl_path.parent.name
    elif env_id == "dim":
        parts = jsonl_path.parts
        base = jsonl_path.stem
        if len(parts) >= 2:
            base = f"{parts[-2]}/{jsonl_path.stem}"
    else:
        base = jsonl_path.stem
    return f"{env_id}:{base}"


def detect_topic_boundaries(messages, min_gap_seconds=300):
    """Heuristic topic segmentation based on time gaps and content shifts."""
    if len(messages) < 3:
        return []
    boundaries = []
    for i in range(1, len(messages)):
        msg = messages[i]
        prev = messages[i - 1]
        ts_cur = msg.get("timestamp", "")
        ts_prev = prev.get("timestamp", "")
        gap_score = 0.0
        if ts_cur and ts_prev:
            try:
                fmt = "%Y-%m-%dT%H:%M:%S"
                t_cur = datetime.strptime(ts_cur[:19], fmt)
                t_prev = datetime.strptime(ts_prev[:19], fmt)
                gap = (t_cur - t_prev).total_seconds()
                if gap > min_gap_seconds:
                    gap_score = min(gap / 3600, 1.0)
            except (ValueError, TypeError):
                pass
        text_cur = set(msg.get("text", "").lower().split())
        text_prev = set(prev.get("text", "").lower().split())
        if text_cur and text_prev:
            overlap = len(text_cur & text_prev) / max(len(text_cur), 1)
            content_score = 1.0 - overlap
        else:
            content_score = 0.5
        confidence = 0.4 * gap_score + 0.6 * content_score
        if confidence > 0.5:
            boundaries.append((i, ts_cur, f"topic_{len(boundaries)+1}", round(confidence, 2)))
    return boundaries


def build_index(rebuild=False, agent_filter="cross"):
    """Build or update the session-digger index."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)
    if rebuild:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM messages_fts")
        conn.execute("DELETE FROM topic_boundaries")
        conn.commit()
    entries = scan_sessions(agent_filter)
    indexed = 0
    skipped = 0
    errors = 0
    t_start = time.time()
    for session_id, jsonl_path, agent in entries:
        mtime, content_hash = _file_fingerprint(jsonl_path)
        if mtime is None:
            errors += 1
            continue
        existing = conn.execute(
            "SELECT jsonl_mtime, content_hash FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if existing and existing[0] == mtime and existing[1] == content_hash and not rebuild:
            skipped += 1
            continue
        try:
            stats = _dispatch_session_stats(jsonl_path)
            if not isinstance(stats, dict):
                # SessionStats dataclass / mapping-like
                try:
                    stats = dict(stats)
                except Exception:
                    stats = {
                        "started": getattr(stats, "started", ""),
                        "ended": getattr(stats, "ended", ""),
                        "user_messages": getattr(stats, "user_messages", 0),
                        "assistant_messages": getattr(stats, "assistant_messages", 0),
                        "tool_calls": getattr(stats, "tool_calls", 0),
                        "errors": getattr(stats, "errors", 0),
                        "compactions": getattr(stats, "compactions", 0),
                        "total_tokens": getattr(stats, "total_tokens", 0),
                        "branch": getattr(stats, "branch", ""),
                        "summary": getattr(stats, "summary", ""),
                        "model": getattr(stats, "model", ""),
                        "first_prompt": getattr(stats, "first_prompt", ""),
                    }
        except Exception:
            errors += 1
            continue
        rich = _compute_rich_stats(jsonl_path, stats)
        try:
            all_msgs = list(_dispatch_extract_messages(jsonl_path, role="both"))
        except Exception:
            all_msgs = []
        identity = _enrich_identity_fields(jsonl_path, stats, all_msgs)
        existing_tags = "[]"
        existing_outcome = None
        if existing:
            old_row = conn.execute(
                "SELECT tags, outcome FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if old_row:
                existing_tags = old_row[0] or "[]"
                existing_outcome = old_row[1]
        conn.execute("""
            INSERT OR REPLACE INTO sessions
            (id, project_path, agent, created, modified, message_count,
             user_messages, assistant_messages, tool_calls, errors,
             compactions, total_tokens, branch, summary, first_prompt,
             jsonl_mtime, indexed_at, jsonl_path, content_hash,
             tool_usage_json, tool_errors_json, flags_json, duration_seconds,
             project_name, tags, outcome, model)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session_id, str(Path(jsonl_path).parent), agent,
            stats.get("started", ""), stats.get("ended", ""),
            stats.get("user_messages", 0) + stats.get("assistant_messages", 0),
            stats.get("user_messages", 0), stats.get("assistant_messages", 0),
            stats.get("tool_calls", 0), stats.get("errors", 0),
            stats.get("compactions", 0), identity["total_tokens"],
            stats.get("branch", ""), identity["summary"], identity["first_prompt"],
            mtime, time.time(), jsonl_path, content_hash,
            json.dumps(rich["tool_usage"], ensure_ascii=False),
            json.dumps(rich["tool_errors"], ensure_ascii=False),
            json.dumps(rich["flags"], ensure_ascii=False),
            rich["duration_seconds"], rich["project_name"],
            existing_tags, existing_outcome, identity["model"],
        ))
        if existing:
            conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
        if all_msgs:
            fts_rows = [
                (session_id, m.get("role", ""), m.get("timestamp", ""), m.get("text", "")[:2000])
                for m in all_msgs
            ]
            try:
                conn.executemany(
                    "INSERT INTO messages_fts (session_id, role, timestamp, text) VALUES (?,?,?,?)",
                    fts_rows,
                )
            except Exception:
                pass
        if existing:
            conn.execute("DELETE FROM topic_boundaries WHERE session_id = ?", (session_id,))
        if all_msgs:
            try:
                boundaries = detect_topic_boundaries(all_msgs)
                if boundaries:
                    boundary_rows = [
                        (session_id, idx, ts, label, conf)
                        for idx, ts, label, conf in boundaries
                    ]
                    conn.executemany(
                        "INSERT INTO topic_boundaries (session_id, message_index, timestamp, topic_label, confidence) VALUES (?,?,?,?,?)",
                        boundary_rows,
                    )
            except Exception:
                pass
        indexed += 1
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?,?)",
        ("last_build", str(time.time()))
    )
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?,?)",
        ("total_sessions", str(indexed + skipped))
    )
    conn.commit()
    conn.close()
    elapsed = time.time() - t_start
    return {"indexed": indexed, "skipped": skipped, "errors": errors, "elapsed": round(elapsed, 2)}
