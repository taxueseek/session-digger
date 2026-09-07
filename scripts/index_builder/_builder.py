"""Index building engine — scan, fingerprint, compute, insert."""
import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import echolib

from echolib._contracts import SessionStats
from echolib._helpers import DIMCODE_DB_PATH as _DIMCODE_DB_PATH
from echolib._helpers import _extract_content_text  # shared content-block unpacker
from index_builder._cjk import split_cjk
from index_builder._schema import DB_DIR, DB_PATH, FTS_TEXT_CAP, init_db
# 模块级 logger：用于捕获被「吃掉」的单文件错误，避免无感数据损失
import logging as _logging
_log = _logging.getLogger("index_builder")


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
        fp_map = _dimcode_session_fingerprints()
        if fp_map is None:
            return None, None
        content_hash = fp_map.get(sid) or hashlib.md5(f"missing:{sid}".encode()).hexdigest()
        # mtime slot carries no meaning for the shared SQLite store; the
        # per-session hash above is the sole change detector.
        return 0.0, content_hash
    if "://" in path_str and not path_str.startswith("file:"):
        content_hash = hashlib.md5(path_str.encode()).hexdigest()
        return 0.0, content_hash
    try:
        st = os.stat(jsonl_path)
        size = st.st_size
        mtime = st.st_mtime
        with open(jsonl_path, "rb") as f:
            head = f.read(4096)
            # Tail sampling: detect appends when mtime is preserved (e.g. git checkout)
            tail = b""
            if size > 8192:
                f.seek(-4096, 2)
                tail = f.read(4096)
        content_hash = hashlib.md5(f"{size}:{head}:{tail}".encode()).hexdigest()
        return mtime, content_hash
    except OSError:
        return None, None


# DimCode 会话级指纹缓存：key = 主库与 WAL 的 (mtime, size)。
_DIMCODE_FP_CACHE = {"key": None, "map": None}


def _dimcode_session_fingerprints():
    """Per-session fingerprint map for the shared DimCode SQLite store.

    The DB file's mtime/size change on ANY dimcode activity, so keying every
    session's fingerprint on them re-extracts all sessions on every build.
    Key on per-session state instead (message count + newest message + the
    session row), cached per DB state so one build ≈ two aggregate queries.
    WAL growth must invalidate the cache too — committed rows can live in the
    -wal file while the main db stays untouched.
    """
    db = _DIMCODE_DB_PATH
    try:
        st = db.stat()
        wal = db.with_name(db.name + "-wal")
        wal_key = (wal.stat().st_mtime, wal.stat().st_size) if wal.exists() else (0.0, 0)
        key = (st.st_mtime, st.st_size, wal_key)
    except OSError:
        return None
    if _DIMCODE_FP_CACHE["key"] == key:
        return _DIMCODE_FP_CACHE["map"]
    fp_map = {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            counts = dict(conn.execute(
                "SELECT sessionId, COUNT(*) FROM messages GROUP BY sessionId"))
            latest = dict(conn.execute(
                "SELECT sessionId, COALESCE(MAX(createdAt),'') FROM messages GROUP BY sessionId"))
            for sid, updated in conn.execute(
                "SELECT sessionId, COALESCE(updatedAt,'') FROM sessions"
            ):
                fp_map[str(sid)] = hashlib.md5(
                    f"{counts.get(sid, 0)}:{latest.get(sid, '')}:{updated or ''}".encode()
                ).hexdigest()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _log.warning("dimcode fingerprint query failed: %s", exc)
        return None
    _DIMCODE_FP_CACHE["key"] = key
    _DIMCODE_FP_CACHE["map"] = fp_map
    return fp_map


def scan_sessions(agent_filter="cross"):
    """Yield (session_id, jsonl_path, agent_type) tuples.

    Single source of truth: all environments go through adapter list_sessions
    when available. _find_jsonl_files is retained as fallback for adapters
    without list_sessions (rare) — new environments should register a
    list_sessions function instead of adding elif branches there.
    """
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
        adapter = echolib.ADAPTER_REGISTRY.get(adapter_name, {})
        # Prefer adapter list_sessions (OCP: new envs need no builder changes)
        if adapter.get("list_sessions"):
            for item in _scan_via_adapter(adapter_name, env_id, home_dir=str(root)):
                sid, path, agent = item
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                entries.append(item)
            continue
        # Fallback: glob-based discovery for adapters without list_sessions
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


def _scan_via_adapter(adapter_name, env_id, limit=50000, home_dir=None):
    """List sessions through a registered adapter (for SQLite / remote stores).

    ``home_dir`` scopes discovery for adapters that accept it (universal
    SchemaProbe): without it the universal scan would crawl the whole $HOME
    and truncate at its internal cap, missing deep envs like ~/.kigi.
    """
    adapter = echolib.ADAPTER_REGISTRY.get(adapter_name)
    if not adapter or not adapter.get("list_sessions"):
        return []
    try:
        if adapter_name == "universal" and home_dir:
            sessions = adapter["list_sessions"](home_dir=home_dir, env_name=env_id, limit=limit) or []
        else:
            sessions = adapter["list_sessions"](limit=limit) or []
    except Exception as exc:  # 单环境扫描失败 → 留痕 + 返回空列表继续下一个
        _log.warning("adapter[%s] list_sessions failed: %s", env_id, exc, exc_info=True)
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
        # 适配器常返回会话目录（Grok/Kimix 等）；下游需要具体 JSONL 文件
        # （fingerprint / stats / FTS 都按文件操作）。目录 → 落到
        # chat_history.jsonl 等具体文件；virtual scheme 原样透传。
        path = echolib.normalize_session_path(path)
        if adapter_name == "universal":
            # SchemaProbe 的 session_id 是文件名 stem（chat_history 等），
            # 同布局下会互相碰撞。一律从路径派生唯一 id（kigi → 会话 uuid）。
            sid = _generate_session_id(Path(path), Path(home_dir or "/"), env_id)
        elif raw_id:
            sid = f"{env_id}:{raw_id}"
        else:
            sid = _generate_session_id(Path(path), Path("/"), env_id)
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
    elif env_id in ("grok", "kigi"):
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
    elif env_id in ("grok", "kigi"):
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


def _compute_session(task):
    """Compute all index data for one session. Module-level so workers can pickle it.

    task: (session_id, jsonl_path, agent, jsonl_mtime, content_hash,
           existing_tags, existing_outcome)
    Returns (session_id, row, fts_rows, boundary_rows, error) — error is a
    string on failure and row/fts_rows/boundary_rows are None/empty.
    """
    (session_id, jsonl_path, agent,
     jsonl_mtime, content_hash, existing_tags, existing_outcome) = task
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
    except Exception as exc:
        return session_id, None, [], [], str(exc)

    rich = _compute_rich_stats(jsonl_path, stats)
    try:
        all_msgs = list(_dispatch_extract_messages(jsonl_path, role="both"))
    except Exception as exc:  # 文件损坏 → 留痕 + 用空消息继续
        _log.warning("extract_messages failed for %s: %s", jsonl_path, exc)
        all_msgs = []
    identity = _enrich_identity_fields(jsonl_path, stats, all_msgs)

    row = (
        session_id, str(Path(jsonl_path).parent), agent,
        stats.get("started", ""), stats.get("ended", ""),
        stats.get("user_messages", 0) + stats.get("assistant_messages", 0),
        stats.get("user_messages", 0), stats.get("assistant_messages", 0),
        stats.get("tool_calls", 0), stats.get("errors", 0),
        stats.get("compactions", 0), identity["total_tokens"],
        stats.get("branch", ""), identity["summary"], identity["first_prompt"],
        jsonl_mtime, time.time(), jsonl_path, content_hash,
        json.dumps(rich["tool_usage"], ensure_ascii=False),
        json.dumps(rich["tool_errors"], ensure_ascii=False),
        json.dumps(rich["flags"], ensure_ascii=False),
        rich["duration_seconds"], rich["project_name"],
        existing_tags, existing_outcome, identity["model"],
        stats.get("cache_hit_rate"),
    )
    # CJK runs must be per-character tokens or Chinese queries never match.
    fts_rows = [
        (session_id, m.get("role", ""), m.get("timestamp", ""),
         split_cjk(m.get("text", "")[:FTS_TEXT_CAP]))
        for m in all_msgs
    ]
    boundaries = detect_topic_boundaries(all_msgs)
    boundary_rows = [
        (session_id, idx, ts, label, conf)
        for idx, ts, label, conf in boundaries
    ]
    return session_id, row, fts_rows, boundary_rows, None


def _map_sessions(fn, tasks):
    """Run per-session computation, parallel when the batch is large enough.

    A small batch stays serial — pool startup outweighs the win.
    SESSION_DIGGER_JOBS=1 forces serial (debugging).
    """
    if not tasks:
        return []
    try:
        jobs = int(os.environ.get("SESSION_DIGGER_JOBS", "") or 0)
    except ValueError:
        jobs = 0
    if jobs <= 0:
        jobs = min(os.cpu_count() or 1, 8)
    if jobs > 1 and len(tasks) >= 24:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            chunk = max(1, len(tasks) // (jobs * 4))
            return list(pool.map(fn, tasks, chunksize=chunk))
    return [fn(t) for t in tasks]


def build_index(rebuild=False, agent_filter="cross"):
    """Build or update the session-digger index."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)
    if rebuild:
        # Scoped delete: --rebuild --agent X must not wipe other environments.
        # FTS/boundaries go first (their subqueries still see the sessions rows).
        if agent_filter in ("cross", "all"):
            conn.execute("DELETE FROM messages_fts")
            conn.execute("DELETE FROM topic_boundaries")
            conn.execute("DELETE FROM sessions")
        else:
            conn.execute(
                "DELETE FROM messages_fts WHERE session_id IN (SELECT id FROM sessions WHERE agent = ?)",
                (agent_filter,),
            )
            conn.execute(
                "DELETE FROM topic_boundaries WHERE session_id IN (SELECT id FROM sessions WHERE agent = ?)",
                (agent_filter,),
            )
            conn.execute("DELETE FROM sessions WHERE agent = ?", (agent_filter,))
        conn.commit()
    entries = scan_sessions(agent_filter)
    indexed = 0
    skipped = 0
    errors = 0
    t_start = time.time()

    # Fingerprint + skip check in the parent (cheap); heavy parsing goes to
    # workers via _map_sessions with the fingerprint riding along in the task.
    pending = []
    for session_id, jsonl_path, agent in entries:
        mtime, content_hash = _file_fingerprint(jsonl_path)
        if mtime is None:
            errors += 1
            continue
        existing = conn.execute(
            "SELECT jsonl_mtime, content_hash, tags, outcome FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if existing and existing[0] == mtime and existing[1] == content_hash and not rebuild:
            skipped += 1
            continue
        pending.append((session_id, jsonl_path, agent, mtime, content_hash,
                        existing[2] if existing else "[]", existing[3] if existing else None))

    for session_id, row, fts_rows, boundary_rows, error in _map_sessions(_compute_session, pending):
        if error is not None:
            errors += 1
            _log.warning("index session %s failed: %s", session_id, error)
            continue
        conn.execute("""
            INSERT OR REPLACE INTO sessions
            (id, project_path, agent, created, modified, message_count,
             user_messages, assistant_messages, tool_calls, errors,
             compactions, total_tokens, branch, summary, first_prompt,
             jsonl_mtime, indexed_at, jsonl_path, content_hash,
             tool_usage_json, tool_errors_json, flags_json, duration_seconds,
             project_name, tags, outcome, model, cache_hit_rate)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, row)
        conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
        if fts_rows:
            try:
                conn.executemany(
                    "INSERT INTO messages_fts (session_id, role, timestamp, text) VALUES (?,?,?,?)",
                    fts_rows,
                )
            except Exception:
                pass
        conn.execute("DELETE FROM topic_boundaries WHERE session_id = ?", (session_id,))
        if boundary_rows:
            try:
                conn.executemany(
                    "INSERT INTO topic_boundaries (session_id, message_index, timestamp, topic_label, confidence) VALUES (?,?,?,?,?)",
                    boundary_rows,
                )
            except Exception:
                pass
        indexed += 1

    # 环境级缓存指标写入 index_meta（适配器提供 cache_metrics 时）。
    # 例如 Kimix 0.1.16 的 metrics/cache_hit-*.jsonl 每请求级汇总。
    for _env_id, _env_info in echolib.ENV_REGISTRY.items():
        _adapter = echolib.ADAPTER_REGISTRY.get(_env_info.get("adapter", ""), {})
        _cache_metrics = _adapter.get("cache_metrics")
        if not _cache_metrics:
            continue
        try:
            _metrics = _cache_metrics()
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                (f"env_cache_metrics_{_env_id}",
                 json.dumps(_metrics, ensure_ascii=False)),
            )
        except Exception as _exc:  # 单环境指标失败不阻断索引
            _log.warning("cache_metrics failed for %s: %s", _env_id, _exc)

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
