"""Index building engine — scan, fingerprint, compute, insert."""
import hashlib
import json
import os
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import echolib

from echolib._helpers import DIMCODE_DB_PATH as _DIMCODE_DB_PATH
from index_builder._cjk import split_cjk, tokenize
from index_builder._evidence import project_user_evidence
from index_builder._schema import DB_DIR, DB_PATH, FTS_TEXT_CAP, init_db, _set_index_meta
# Per-session analysis lives in its own module: _builder owns discovery,
# fingerprinting and persistence; _session_analysis owns "file in, fields out".
from index_builder._session_analysis import (
    _compute_rich_stats,
    _enrich_identity_fields,
    _rich_stats_from_tools,
    _single_pass_analyze,
)

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

# Test seam: ``_file_fingerprint`` prefers this over the live dimcode map when
# a test injects one (see tests/test_cache_and_incremental.py). Production
# leaves it None and resolves through ``_dimcode_session_fingerprints``.
# ``build_index`` resets it to None on entry so each build re-reads dimcode.
_DIMCODE_FP_MAP = None

# Environments whose listing raised during the last scan_sessions() call.
# build_index must not read a failed listing as "the user deleted every session
# in that environment" — see _prune_stale_sessions.
_SCAN_FAILED_ENVS: set = set()

# Bump when adapter/token parsing changes incompatibly so incremental index
# re-parses once without requiring --rebuild (avoids stale 0-token rows).
# Honoured by both fingerprint paths: _file_fingerprint (file-backed sessions)
# and _dimcode_session_fingerprints (SQLite-backed).
_PARSER_EPOCH = "v9-cjk-symbol-and-digit-boundary"

# Tool payloads dominate the evidence that is not prose. Measured 2026-09-17 on
# a 21.6 MB subagent trace: after deduplicating its 53 request snapshots the
# distinct evidence is 670 KB, of which 576 KB (86%) is tool results — and none
# of it was reachable, because only the assistant's ``[TOOL: name] <key>`` line
# was indexed. One digest per distinct tool result closes that hole; the cap
# keeps payload volume from swamping prose in ranking.
TOOL_DIGEST_CAP = 400


def _tool_fts_rows(session_id, tools):
    """FTS rows for distinct tool results (facet ``TOOL``).

    ``tools`` comes from the single-pass analyzer, which already parses each
    ``tool_result`` block into ``result_preview`` — the digest is bounded there,
    not re-read here. Identical previews inside one session collapse to a single
    row: a tool called 20 times on the same file is one piece of evidence.

    Deliberately *not* folded into ``all_msgs``: that list also feeds evidence
    projection, topic boundaries and identity inference, and widening it would
    change all three as a side effect.
    """
    rows, seen = [], set()
    for tool in tools or []:
        preview = str(tool.get("result_preview") or "").strip()
        if not preview or preview == "(no result captured)":
            continue
        # 前缀不进切分：split_cjk 的符号边界会把 [TOOL Bash] 切成 [ TOOL Bash ]，
        # 破坏「digest 以工具名开头」的可读契约（且 unicode61 下 [ 本就是分隔符，
        # 切不切检索语义相同）。cap 在 split 后生效：切分插空格会撑长（实测 400→402）。
        prefix = f"[TOOL {tool.get('name') or '?'}] "
        body = split_cjk(preview)[:TOOL_DIGEST_CAP - len(prefix)]
        digest = prefix + body
        if digest in seen:
            continue
        seen.add(digest)
        rows.append((session_id, "TOOL", str(tool.get("timestamp") or ""), digest))
    return rows


def _file_fingerprint(jsonl_path):
    """Content fingerprint: (mtime, content_hash) for incremental skip.

    * Plain files: filesystem mtime + MD5(size || first 4KiB)
    * dimcode://: per-session map from SQLite (not whole-DB mtime)
    * Other virtual schemes: stable hash of the URI
    """
    path_str = str(jsonl_path)
    if path_str.startswith("dimcode://") or path_str.startswith("dimcode:"):
        sid = path_str.split("://", 1)[-1] if "://" in path_str else path_str.split(":", 1)[-1]
        # index ids look like dimcode:sess_xxx — strip env prefix if present
        if sid.startswith("dimcode:"):
            sid = sid.split(":", 1)[1]
        fp_map = _DIMCODE_FP_MAP if _DIMCODE_FP_MAP is not None else _dimcode_session_fingerprints()
        if fp_map is None:
            return None, None
        fp = fp_map.get(sid)
        if fp:
            return fp
        # Unknown session: force reindex attempt via unstable hash
        content_hash = hashlib.md5(f"missing:{sid}".encode()).hexdigest()
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
        content_hash = hashlib.md5(
            f"{_PARSER_EPOCH}:{size}:{head}:{tail}".encode()
        ).hexdigest()
        return mtime, content_hash
    except OSError:
        return None, None


# DimCode 会话级指纹缓存：key = 主库与 WAL 的 (mtime, size)。
_DIMCODE_FP_CACHE = {"key": None, "map": None}


def _dimcode_session_fingerprints():
    """Per-session fingerprint map for the shared DimCode SQLite store.

    The DB file's mtime/size change on ANY dimcode activity, so keying every
    session's fingerprint on them re-extracts all sessions on every build.
    Key on per-session state instead (message count + the session row), cached
    per DB state so one build ≈ one pass over two small tables. WAL growth must
    invalidate the cache too — committed rows can live in the -wal file while
    the main db stays untouched.

    Only index-covered columns may be read here. ``messages`` is ~436 MB / 135k
    rows in a live install, and ``MAX(createdAt) GROUP BY sessionId`` is not
    covered by any of its indexes, so it forced one table lookup per row:
    measured 2.30 s per cold build (18% of the whole incremental build) against
    ~25 ms for the covered pair below. Append detection rides on COUNT(*), which
    the sessionId-covering index answers; edit detection rides on the session
    row's updatedAt, which is O(sessions) and free.

    ``_PARSER_EPOCH`` participates in the hash so a parser/tokenization change
    re-parses dimcode sessions too. Without it the other 68% of the index
    (dimcode rows) kept stale derived columns forever, while only file-backed
    sessions honoured the epoch through ``_file_fingerprint``.

    Values are ``(mtime, content_hash)`` tuples: mtime is derived from the
    session row's updatedAt (ISO-8601 → epoch) so downstream mtime-based
    comparisons stay meaningful; the hash remains the sole change detector.
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
            for sid, updated in conn.execute(
                "SELECT sessionId, COALESCE(updatedAt,'') FROM sessions"
            ):
                mtime = 0.0
                if updated:
                    try:
                        mtime = datetime.fromisoformat(
                            updated.replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError, OSError):
                        mtime = 0.0
                fp_map[str(sid)] = (mtime, hashlib.md5(
                    f"{_PARSER_EPOCH}|{counts.get(sid, 0)}:{updated or ''}".encode()
                ).hexdigest())
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _log.warning("dimcode fingerprint query failed: %s", exc)
        return None
    _DIMCODE_FP_CACHE["key"] = key
    _DIMCODE_FP_CACHE["map"] = fp_map
    return fp_map


def _indexed_env_ids():
    """Environment ids the index already holds session rows for.

    Feeds :func:`echolib.adopted_envs`. Read-only and best-effort: an absent or
    unreadable index means "nothing to adopt", which is exactly the first build.
    """
    if not DB_PATH.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    # substr up to the first ':' — ids are "{env}:{adapter_id}" by contract.
    try:
        rows = conn.execute(
            "SELECT DISTINCT substr(id, 1, instr(id, ':') - 1) FROM sessions"
            " WHERE instr(id, ':') > 1"
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
    return {r[0] for r in rows if r[0]}


def _scan_envs():
    """Every environment the index builder should scan and keep rows for.

    One definition, used by both :func:`scan_sessions` (what gets refreshed) and
    :func:`_prune_stale_sessions` (what is ours to judge). They have to agree:
    an environment in the scan set but not the prune set leaves rows that are
    never updated, and one in the prune set but not the scan set deletes rows
    it never looked at.
    """
    envs = {}
    envs.update(echolib.ENV_REGISTRY)
    envs.update(echolib.KNOWN_UNADAPTED)
    envs.update(echolib.adopted_envs(_indexed_env_ids()))
    return envs


def scan_sessions(agent_filter="cross"):
    """Yield (session_id, jsonl_path, agent_type) tuples.

    Single source of truth: all environments go through adapter list_sessions
    when available. _find_jsonl_files is retained as fallback for adapters
    without list_sessions (rare) — new environments should register a
    list_sessions function instead of adding elif branches there.

    Side effect: resets :data:`_SCAN_FAILED_ENVS` to the set of environments
    this scan could not list (see ``build_index``'s prune step).
    """
    _SCAN_FAILED_ENVS.clear()
    entries = []
    seen_ids: set = set()
    all_envs = _scan_envs()
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
        except Exception as exc:
            # 与适配器分支同一契约：列举失败必须登记，否则 _prune_stale_sessions
            # 会把这个环境读成「用户把这里的会话全删了」，而它的 root 还在，
            # 于是整环境的行连同 FTS/边界行被 DELETE。
            _log.warning("glob discovery failed for %s: %s", env_id, exc)
            _SCAN_FAILED_ENVS.add(env_id)
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
        # Enumeration only: the caller reads session_id/full_path and drops
        # every preview field, so adapters skip their per-file head parse.
        # Measured 5.40 s -> 1.05 s on the live 6294-session install with the
        # session id set identical; see echolib.discovery_only.
        with echolib.discovery_only():
            if adapter_name == "universal" and home_dir:
                sessions = adapter["list_sessions"](home_dir=home_dir, env_name=env_id, limit=limit) or []
            else:
                sessions = adapter["list_sessions"](limit=limit) or []
    except Exception as exc:  # 单环境扫描失败 → 留痕 + 返回空列表继续下一个
        _log.warning("adapter[%s] list_sessions failed: %s", env_id, exc, exc_info=True)
        _SCAN_FAILED_ENVS.add(env_id)
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
        # 解析失败时 normalize_session_path 会原样返回目录，而目录不是
        # transcript：下游 fingerprint 无法读它，这个会话会永久报错且永远
        # 进不了索引（实测 4 个 kimix 目录只有 summary.json、没有会话文件，
        # 每次构建稳定产生 errors=4）。没有会话内容就如实不列，而不是列出来
        # 再失败。
        if path and "://" not in str(path) and Path(path).is_dir():
            continue
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
    elif env_id == "kimix":
        # Kimix CLI: identical session layout to Grok
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                chat_file = session_dir / "chat_history.jsonl"
                if chat_file.exists():
                    jsonl_files.append(chat_file)
    elif env_id == "kimi":
        # Standalone Kimi: project/session/wire.jsonl (skip context.jsonl noise)
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                wire_file = session_dir / "wire.jsonl"
                if wire_file.exists():
                    jsonl_files.append(wire_file)
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
    elif env_id == "kimix":
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
    """Heuristic topic segmentation based on time gaps and content shifts.

    Content shift is measured on CJK-aware tokens. Splitting a Chinese sentence
    on whitespace yields ONE token for the whole run, so overlap between any two
    distinct Chinese messages is 0, every pair reads as a total topic change and
    the table fills with a "boundary" between essentially every message
    (measured: 48763 rows over 71003 messages, one per 1.46). Same tokenizer
    contract as the FTS layer — see index_builder._cjk.
    """
    if len(messages) < 3:
        return []
    boundaries = []
    # Each step compares message i against i-1. Carrying the previous token set
    # forward means every message is tokenized once instead of twice — the
    # ``prev`` look-back re-tokenized the same text on the next iteration.
    # Tokenizing was 6.7 s of a 74 s profile (see _cjk), and this halves it
    # without changing a single pair: ``text_prev`` here *is* the previous
    # iteration's ``text_cur``, same expression, same input text.
    text_prev = set(tokenize(messages[0].get("text", "")))
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
        text_cur = set(tokenize(msg.get("text", "")))
        if text_cur and text_prev:
            overlap = len(text_cur & text_prev) / max(len(text_cur), 1)
            content_score = 1.0 - overlap
        else:
            content_score = 0.5
        confidence = 0.4 * gap_score + 0.6 * content_score
        if confidence > 0.5:
            boundaries.append((i, ts_cur, f"topic_{len(boundaries)+1}", round(confidence, 2)))
        text_prev = text_cur
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

    # Claude-like JSONL: one read produces stats + tools + messages + identity.
    # Everything else falls through to adapter dispatch (ground truth for
    # formats whose billable tokens live outside the transcript).
    single = _single_pass_analyze(jsonl_path, agent)
    if single is not None:
        stats, tools, all_msgs, identity = single
        rich = _rich_stats_from_tools(tools, stats, jsonl_path)
    else:
        # One parse-once scope around every adapter read. Stats, tools and
        # messages are three separate walks of the same file; outside a scope
        # each one starts over at byte 0 (measured over a full rebuild: 73% of
        # parsed bytes were a re-read). The fingerprint was taken before we got
        # here, which is the scope's precondition.
        with echolib.parse_once():
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
        # Adapter dispatch yields USER/ASSISTANT only — the per-call tool results
        # it drops are invisible here, so this path contributes no TOOL facet.
        # Boundary measured 2026-09-17: of the environments whose transcripts
        # carry tool_result blocks (zcode, zcode_v2, universal, grok, kimix) the
        # single-pass analyzer handles them; dimcode carries no tool calls at all
        # (4325 sessions, 0 tools) so its rows are unaffected either way.
        tools = []

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
    session_role = echolib.classify_session_role(agent, session_id, jsonl_path)
    # Grok: refine with summary.session_kind when available
    if agent == "grok" and session_role != "subagent":
        try:
            summary_path = Path(jsonl_path).parent / "summary.json"
            if summary_path.is_file():
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                session_role = echolib.classify_session_role(
                    agent, session_id, jsonl_path, summary=summary
                )
        except Exception:
            pass
    row = row + (session_role, project_user_evidence(all_msgs),)
    # CJK runs must be per-character tokens or Chinese queries never match.
    fts_rows = [
        (session_id, m.get("role", ""), m.get("timestamp", ""),
         split_cjk(m.get("text", "")[:FTS_TEXT_CAP]))
        for m in all_msgs
    ]
    fts_rows.extend(_tool_fts_rows(session_id, tools))
    boundaries = detect_topic_boundaries(all_msgs)
    boundary_rows = [
        (session_id, idx, ts, label, conf)
        for idx, ts, label, conf in boundaries
    ]
    return session_id, row, fts_rows, boundary_rows, None


def _prune_stale_sessions(conn, live_ids, agent_filter="cross"):
    """Delete rows for sessions that are no longer discoverable on disk.

    The index only ever grew. A session whose transcript was deleted or renamed
    kept its row, its FTS rows and its topic boundaries forever — and because
    only *discovered* sessions are ever rewritten, a row that stops being
    re-scanned also freezes whatever the schema held at the time. Measured on
    the live index before this existed: 53 rows for sessions the scan no longer
    found (22 of them pointing at deleted files, the rest from renamed
    directories and from a discovery bug that indexed a backup tree), 36 of
    which were still carrying the pre-``user_evidence_json`` empty string and
    so rendered zero evidence in search results.

    Three guards, because a wrong delete here destroys history:
    * only a full scan prunes — ``--agent X`` never touches other environments;
    * a row survives if its path is virtual (``dimcode://``) or outside every
      registry root, so DB-backed and relocated sessions are untouched;
    * environments whose listing *failed* this run are skipped wholesale, so an
      unmounted volume or a transient sqlite lock cannot wipe an environment.

    Returns the number of rows removed.
    """
    if agent_filter not in ("cross", "all"):
        return 0

    # Same set the scan used (``_scan_envs``): the two must agree, or rows end up
    # either never refreshed or deleted without ever being listed.
    live_roots = []
    for env_id, env_info in _scan_envs().items():
        if env_id in _SCAN_FAILED_ENVS:
            continue
        root = Path(os.path.expanduser(env_info["root"]))
        if root.exists():
            live_roots.append(os.path.realpath(str(root)) + os.sep)

    stale = []
    for sid, path in conn.execute("SELECT id, jsonl_path FROM sessions"):
        if sid in live_ids or not path:
            continue
        if "://" in path and not path.startswith("file:"):
            continue  # dimcode:// and friends are DB-backed, not on disk
        # realpath once per candidate, not once per root: inside the generator
        # below it was re-evaluated for every live root (up to ~30 syscalls per
        # row). Only stale candidates pay for this at all.
        real = os.path.realpath(path)
        if not any(real.startswith(r) for r in live_roots):
            continue  # outside every live root we scanned → not ours to judge
        stale.append(sid)

    if not stale:
        return 0
    _delete_scoped_rows(conn, stale)
    for start in range(0, len(stale), 900):
        chunk = stale[start:start + 900]
        conn.execute(
            f"DELETE FROM sessions WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    conn.commit()
    _log.info("pruned %d session(s) no longer on disk", len(stale))
    return len(stale)


def _delete_scoped_rows(conn, session_ids, chunk_size=900):
    """Delete messages_fts / topic_boundaries rows for many sessions in batches.

    ``chunk_size`` stays under SQLite's default 999 bound-parameter limit.
    """
    ids = [sid for sid in session_ids if sid]
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start:start + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM messages_fts WHERE session_id IN ({placeholders})", chunk)
        conn.execute(
            f"DELETE FROM topic_boundaries WHERE session_id IN ({placeholders})", chunk)


# FTS5 never merges segments by itself: every incremental delete+insert leaves
# more of them. ``messages_fts_data`` grew from 51 MB (one bulk insert) to
# 219 MB over this index's life while the content stayed at ~99 MB, and nothing
# but merging reclaims it — `optimize` merges the segments and frees the pages,
# ``VACUUM`` returns them to the filesystem. Measured on the live index:
# 333 MB -> 162 MB (optimize 3.7 s + vacuum 1.1 s).
#
# Triggered by rewrite volume rather than a size heuristic: fragmentation comes
# from rewriting rows, so a build that rewrote a large share of the index is
# exactly the moment to merge. Incremental builds touching a handful of
# sessions skip it and stay fast.
_COMPACT_MIN_REWRITES = 200
_COMPACT_REWRITE_RATIO = 0.10


def _compact_index(conn):
    """Merge FTS segments and return the freed pages to the filesystem."""
    before = conn.execute("SELECT page_count FROM pragma_page_count").fetchone()[0]
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
    conn.commit()
    conn.execute("VACUUM")
    after = conn.execute("SELECT page_count FROM pragma_page_count").fetchone()[0]
    return {"pages_before": before, "pages_after": after}


def _should_compact(indexed, scanned):
    """Does this build's rewrite volume justify the merge + VACUUM?

    Named so the decision has one home: it was an inline boolean that the tests
    re-typed verbatim, so the tests only proved the copy kept matching itself.
    ``scanned`` is the post-scan session count (indexed + skipped), *not* a
    rewrite count — the ratio it feeds is "what share of what we just looked at
    did we rewrite".
    """
    return indexed >= _COMPACT_MIN_REWRITES and indexed >= scanned * _COMPACT_REWRITE_RATIO


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

def _grok_child_session_ids():
    """Session ids marked as Grok subagents via parent subagents/*/meta.json."""
    ids = set()
    try:
        root = Path(os.path.expanduser("~/.grok/sessions"))
        if not root.is_dir():
            return ids
        for sub_root in root.rglob("subagents"):
            if not sub_root.is_dir():
                continue
            try:
                for meta in echolib.grok_list_subagents(sub_root.parent):
                    cid = meta.get("child_session_id") or meta.get("subagent_id")
                    if cid:
                        ids.add(str(cid))
            except Exception:
                continue
    except Exception as exc:
        _log.debug("grok child id scan failed: %s", exc)
    return ids


def _backfill_session_roles(conn):
    """Fill session_role for existing rows without full reparse.

    Every statement is guarded to touch only rows that still need the value.
    An unguarded ``UPDATE ... SET session_role = 'subagent' WHERE <predicate>``
    rewrites matching rows on every build even when the value already matches —
    SQLite cannot skip a no-op row update, so it pages-churns the whole table
    and the freed pages accumulate (measured: a freshly built index was 168 MB
    against a live 333 MB one). Rows are written once at insert time, so after
    the first backfill this function must be a pure no-op.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "session_role" not in cols:
        return
    # Needs a role still — the one predicate every statement below shares.
    needs_role = "(session_role IS NULL OR session_role = '' OR session_role = 'unknown')"
    # DimCode: id prefix is authoritative
    conn.execute(f"""
        UPDATE sessions SET session_role = 'subagent'
        WHERE agent = 'dimcode'
          AND (id LIKE '%:subagent_%' OR id LIKE 'dimcode:subagent_%')
          AND {needs_role}
    """)
    conn.execute(f"""
        UPDATE sessions SET session_role = 'main'
        WHERE agent = 'dimcode'
          AND (id LIKE '%:sess_%' OR id LIKE 'dimcode:sess_%')
          AND {needs_role}
    """)
    # Path markers (Claude subagents if ever indexed; Kimi non-main wire)
    conn.execute(f"""
        UPDATE sessions SET session_role = 'subagent'
        WHERE (jsonl_path LIKE '%/subagents/%' OR jsonl_path LIKE '%/agents/agent-%')
          AND {needs_role}
    """)
    conn.execute(f"""
        UPDATE sessions SET session_role = 'main'
        WHERE agent = 'claude'
          AND jsonl_path NOT LIKE '%/subagents/%'
          AND {needs_role}
    """)
    conn.execute(f"""
        UPDATE sessions SET session_role = 'main'
        WHERE agent IN ('kimi_code', 'kimi')
          AND jsonl_path LIKE '%/agents/main/%'
          AND {needs_role}
    """)
    # Grok children discovered from parent meta. One scan + one UPDATE per
    # chunk, not one UPDATE per child: each child statement is a full pass over
    # ``sessions`` (the id/jsonl_path predicates are LIKEs, so no index applies),
    # and there are 29 children on the live install — measured 2026-09-17 as the
    # largest single item in the backfill (85 ms of a 0.67 s no-change build).
    # Same shape as ``_delete_scoped_rows`` below.
    child_ids = sorted(_grok_child_session_ids())
    if child_ids:
        hits = [
            sid for sid, path in conn.execute(
                "SELECT id, jsonl_path FROM sessions"
                f" WHERE agent = 'grok' AND {needs_role}"
            )
            if any(cid in sid or cid in (path or "") for cid in child_ids)
        ]
        for start in range(0, len(hits), 900):  # < SQLite's 999 bind limit
            chunk = hits[start:start + 900]
            conn.execute(
                "UPDATE sessions SET session_role = 'subagent'"
                f" WHERE id IN ({','.join('?' * len(chunk))}) AND {needs_role}",
                chunk,
            )
    conn.execute(f"""
        UPDATE sessions SET session_role = 'main'
        WHERE agent = 'grok'
          AND {needs_role}
    """)
    # ZCode subagent sessions
    conn.execute(f"""
        UPDATE sessions SET session_role = 'subagent'
        WHERE agent = 'zcode'
          AND (id LIKE '%sess_subagent%' OR jsonl_path LIKE '%sess_subagent%')
          AND {needs_role}
    """)
    conn.commit()


def build_index(rebuild=False, agent_filter="cross", compact=False):
    """Build or update the session-digger index (incremental by default).

    ``compact=True`` always runs the FTS/VACUUM maintenance; otherwise it is
    triggered by rewrite volume (see ``_COMPACT_MIN_REWRITES``).
    """
    global _DIMCODE_FP_MAP
    # Fresh fingerprint map each build (dimcode may have changed since last run).
    _DIMCODE_FP_MAP = None
    _DIMCODE_FP_CACHE["key"] = None

    # Wall clock for the whole command, not just the insert loop. The previous
    # t_start sat *after* init_db, the role backfill, the 6337-row fingerprint
    # preload and scan_sessions — so the reported ``elapsed`` excluded ~80% of
    # an unchanged incremental build (0.85 s reported against 3.4-4.2 s wall,
    # and 0.71 s reported against 4.67 s wall on a 349 MB index). Callers that
    # log or gate on this number were reading a number that could not explain
    # the command they measured.
    t_start = time.time()

    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)
    _backfill_session_roles(conn)
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
        # 重建窗口闸门：DELETE 到新行灌入之间并发 search 会把空索引当真实
        # 「零命中」。标记期间 _fts_search 响亮回退文件扫描；成功路径末尾
        # 清除，中途崩溃则标记残留——一直慢但正确，直到下次 build 清除。
        _set_index_meta(conn, "rebuild_in_progress", agent_filter)

    # Batch-load prior fingerprints (+ tags/outcome) — one query, not N round-trips.
    existing_map = {
        row[0]: (row[1], row[2], row[3], row[4])
        for row in conn.execute(
            "SELECT id, jsonl_mtime, content_hash, tags, outcome FROM sessions"
        )
    }

    entries = scan_sessions(agent_filter)
    indexed = 0
    skipped = 0
    errors = 0

    # Fingerprint + skip check in the parent (cheap); heavy parsing goes to
    # workers via _map_sessions with the fingerprint riding along in the task.
    pending = []
    for session_id, jsonl_path, agent in entries:
        mtime, content_hash = _file_fingerprint(jsonl_path)
        if mtime is None and content_hash is None:
            errors += 1
            continue
        prior = existing_map.get(session_id)
        if (
            prior
            and prior[0] == mtime
            and prior[1] == content_hash
            and not rebuild
        ):
            skipped += 1
            continue
        pending.append((session_id, jsonl_path, agent, mtime, content_hash,
                        prior[2] if prior else "[]", prior[3] if prior else None))

    # Drop the old rows for every session about to be rewritten in ONE pass per
    # batch, before the insert loop. messages_fts keeps session_id UNINDEXED, so
    # each `DELETE ... WHERE session_id = ?` is a full scan of the content table
    # — measured 28.9 ms, i.e. 3 minutes for a 6287-session rebuild and 73% of
    # it. Batching ~900 ids per statement turns N scans into N/900. Only
    # sessions that re-parsed successfully are dropped, so a corrupt file keeps
    # its previous rows exactly as it did when the delete sat inside the loop.
    computed = _map_sessions(_compute_session, pending)
    _delete_scoped_rows(conn, [sid for sid, _r, _f, _b, err in computed if err is None])

    for session_id, row, fts_rows, boundary_rows, error in computed:
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
             project_name, tags, outcome, model, cache_hit_rate, session_role,
             user_evidence_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, row)
        if fts_rows:
            try:
                conn.executemany(
                    "INSERT INTO messages_fts (session_id, role, timestamp, text) VALUES (?,?,?,?)",
                    fts_rows,
                )
            except Exception as exc:
                # 会话行与它的 FTS 行必须同生共死。批量删除（上面的
                # _delete_scoped_rows）已经清掉旧 FTS 行，此时再保留会话行，
                # 下一轮构建会因为指纹一致直接跳过它 —— 于是「列表里看得见、
                # 搜不到」是永久的，而且不计入 errors，没有任何信号。删掉行，
                # 下一轮 prior 为空必然重试。
                _log.warning("FTS insert failed for %s: %s", session_id, exc)
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                errors += 1
                continue
        if boundary_rows:
            try:
                conn.executemany(
                    "INSERT INTO topic_boundaries (session_id, message_index, timestamp, topic_label, confidence) VALUES (?,?,?,?,?)",
                    boundary_rows,
                )
            except Exception as exc:
                _log.warning("topic boundary failed for %s: %s", session_id, exc)
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

    # 成功路径清重建标记：到这里索引已可信任。崩溃路径不清——标记残留
    # 会让 search 一直回退文件扫描（慢但正确），直到下次 build 清除。
    conn.execute("DELETE FROM index_meta WHERE key = 'rebuild_in_progress'")
    conn.commit()

    # Runs last so the delete covers every environment this scan actually saw,
    # and only after a build that produced a trustworthy session list.
    pruned = _prune_stale_sessions(conn, {e[0] for e in entries}, agent_filter)

    scanned = indexed + skipped
    do_compact = compact or _should_compact(indexed, scanned)
    stats_pages = _compact_index(conn) if do_compact else None
    conn.close()
    elapsed = time.time() - t_start
    result = {"indexed": indexed, "skipped": skipped, "errors": errors,
              "elapsed": round(elapsed, 2)}
    if pruned:
        result["pruned"] = pruned
    if stats_pages:
        result["compacted"] = (f"{stats_pages['pages_before'] * 4096 // 1048576}MB -> "
                               f"{stats_pages['pages_after'] * 4096 // 1048576}MB")
    return result
