import json
import math
import os
import re
import sqlite3
import sys
import concurrent.futures
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import time as _time

from echolib._helpers import (
    CLAUDE_DIR,
    CODEX_DIR,
    DIMCODE_DB_PATH,
    DIM_DIR,
    GROK_DIR,
    KIMI_CODE_DIR,
    KIMI_DIR,
    KNOWN_TYPES,
    NOISE_TYPES,
    REASONIX_DIR,
    TRAE_DIR,
    WORKBUDDY_DIR,
    ZCODE_DIR,
    _NOISE_STRINGS,
    _extract_content_text,
    _iter_jsonl,
    _match_call_results,
    _strip_system_reminder,
)
from echolib._models import (
    Record,
    SessionMeta,
    normalize_model_name,
)

def iter_records(path, types=None, skip_noise=True, limit=0):
    """
    Yield Record objects from a .jsonl file.

    Args:
        path: Path to the .jsonl file.
        types: Optional set/list of record types to include.
        skip_noise: Skip progress/queue-operation records.
        limit: Stop after this many yielded records (0 = unlimited).
    """
    type_filter = set(types) if types else None
    count = 0

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # Pre-filter: skip noise by string match before json.loads
            if skip_noise:
                if any(ns in line for ns in _NOISE_STRINGS):
                    continue

            # Pre-filter: skip types we don't want (cheap string check)
            if type_filter and '"file-history-snapshot"' in line and "file-history-snapshot" not in type_filter:
                continue

            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            rtype = d.get("type", "")
            if skip_noise and rtype in NOISE_TYPES:
                continue
            if type_filter and rtype not in type_filter:
                continue

            yield Record(d)
            count += 1
            if limit and count >= limit:
                return

def detect_schema(path):
    """
    Probe a .jsonl file and return a schema report dict.

    Returns dict with keys: file, lines, bytes, first_timestamp, last_timestamp,
    versions, models, unknown_types, record_types (type -> {count, fields}).
    """
    type_counts = Counter()
    field_sets = {}
    versions = set()
    models = set()
    first_ts = ""
    last_ts = ""
    total_bytes = 0
    line_count = 0
    unknown_types = set()

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line_count += 1
            total_bytes += len(line)
            line = line.strip()
            if not line:
                continue

            if '"type"' not in line:
                continue

            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            rtype = rec.get("type", "")
            type_counts[rtype] += 1

            if rtype not in KNOWN_TYPES:
                unknown_types.add(rtype)

            if rtype not in field_sets:
                field_sets[rtype] = set()
            if type_counts[rtype] <= 5:
                field_sets[rtype].update(rec.keys())

            v = rec.get("version", "")
            if v:
                versions.add(v)

            msg = rec.get("message", {})
            if isinstance(msg, dict):
                m = msg.get("model", "")
                if m and m != "<synthetic>":
                    models.add(m)

            ts = rec.get("timestamp", "")
            if ts:
                if not first_ts or ts < first_ts:
                    first_ts = ts
                if ts > last_ts:
                    last_ts = ts

    return {
        "file": str(path),
        "lines": line_count,
        "bytes": total_bytes,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "versions": sorted(versions),
        "models": sorted(models),
        "unknown_types": sorted(unknown_types),
        "record_types": {
            rtype: {
                "count": count,
                "fields": sorted(field_sets.get(rtype, set())),
            }
            for rtype, count in type_counts.most_common()
        },
    }

def session_stats(path):
    """
    Compute session statistics in a single pass.

    Returns a dict with: slug, model, branch, started, ended, user_messages,
    assistant_messages, tool_calls, files_edited, errors, input_tokens,
    output_tokens, cache_read_tokens, cache_create_tokens, total_tokens,
    compactions, summary.
    """
    stats = {
        "slug": "", "model": "", "branch": "",
        "started": "", "ended": "",
        "user_messages": 0, "assistant_messages": 0,
        "tool_calls": 0, "files_edited": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "compactions": 0, "summary": "",
    }

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # Count errors by string match BEFORE parsing (cheap)
            if '"is_error": true' in line or '"is_error":true' in line:
                stats["errors"] += 1

            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            rtype = d.get("type", "")
            ts = d.get("timestamp", "")
            # Normalize timestamp to string before comparison to avoid
            # float-vs-str crashes on non-Claude formats that slip through
            if ts and not isinstance(ts, str):
                ts = _normalize_timestamp(ts)

            if ts:
                if not stats["started"] or str(ts) < str(stats["started"]):
                    stats["started"] = ts
                if str(ts) > str(stats["ended"]):
                    stats["ended"] = ts

            if not stats["branch"]:
                stats["branch"] = d.get("gitBranch", "")
            if not stats["slug"]:
                stats["slug"] = d.get("slug", "")

            if rtype == "user":
                msg = d.get("message", {})
                if not isinstance(msg, dict):
                    continue
                if d.get("isMeta") or d.get("isCompactSummary"):
                    continue
                content = msg.get("content", "")
                # Fallback: some Claude forks (e.g. Qwen) use message.parts instead of content
                if not content and msg.get("parts"):
                    content = msg.get("parts")
                if isinstance(content, list):
                    has_tr = any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    )
                    if has_tr:
                        continue
                    has_text = any(
                        isinstance(b, dict) and b.get("type") == "text"
                        for b in content
                    )
                    if has_text:
                        stats["user_messages"] += 1
                    # Also check parts-style: [{"text": "..."}]
                    elif any(isinstance(b, dict) and b.get("text") for b in content):
                        stats["user_messages"] += 1
                elif isinstance(content, str) and content.strip():
                    stats["user_messages"] += 1

            elif rtype == "assistant":
                msg = d.get("message", {})
                if not isinstance(msg, dict):
                    continue
                m = msg.get("model", "")
                if m == "<synthetic>":
                    continue
                stats["assistant_messages"] += 1
                if not stats["model"] and m:
                    stats["model"] = m

                usage = msg.get("usage", {})
                if isinstance(usage, dict):
                    stats["input_tokens"] += usage.get("input_tokens", 0)
                    stats["output_tokens"] += usage.get("output_tokens", 0)
                    stats["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
                    stats["cache_create_tokens"] += usage.get("cache_creation_input_tokens", 0)

                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            stats["tool_calls"] += 1

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

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats

def _extract_first_prompt(path):
    """单次扫描取首条用户文本（轻量版，不解析 full record tree）。

    取代 broad_list_claude_sessions 里先 session_stats 再 extract_messages
    的双遍扫描。只在打开文件后向前扫描，取到即返回。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or '"progress"' in line or '"queue-operation"' in line:
                    continue
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if d.get("type") != "user":
                    continue
                if d.get("isMeta") or d.get("isCompactSummary"):
                    continue
                msg = d.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    fp = content.strip()
                    if not fp.startswith("<") and len(fp) > 2:
                        return fp[:200]
                elif isinstance(content, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                        continue
                    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    fp = " ".join(t for t in texts if t).strip()
                    if fp and not fp.startswith("<") and len(fp) > 2:
                        return fp[:200]
                break  # 只取第一条 user 消息
    except OSError:
        pass
    return ""


def extract_messages(path, role="both", no_tools=False, limit=0, thinking_limit=0):
    """
    Yield dicts with keys: role, timestamp, text.

    Args:
        role: "user", "assistant", or "both".
        no_tools: If True, omit tool_use summaries from assistant messages.
        limit: Max messages to yield (0 = unlimited).
        thinking_limit: Max chars for thinking blocks (0 = full, -1 = hide).
    """
    count = 0

    for rec in iter_records(path, types={"user", "assistant"}, skip_noise=True, limit=0):
        if limit and count >= limit:
            return

        if role != "both" and rec.type != role:
            continue

        if rec.type == "user":
            if rec.is_meta_user() or rec.is_compact_summary():
                continue
            if rec.is_tool_result_message():
                continue

            text = rec.text_content()
            if not text or text.startswith("<system-reminder>") or text.startswith("[Request interrupted") or text.startswith("<runtime_context>"):
                continue

            yield {"role": "USER", "timestamp": rec.timestamp, "text": text}
            count += 1

        elif rec.type == "assistant":
            if rec.is_synthetic():
                continue

            content = rec.content
            if not isinstance(content, list):
                continue

            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                if btype == "text":
                    t = block.get("text", "").strip()
                    if t:
                        parts.append(t)

                elif btype == "thinking" and thinking_limit != -1:
                    t = block.get("thinking", "").strip()
                    if t:
                        if thinking_limit > 0:
                            t = t[:thinking_limit]
                        parts.append("[THINKING] " + t)

                elif btype == "tool_use" and not no_tools:
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    if not isinstance(inp, dict):
                        inp = {}
                    key = _tool_key(name, inp)
                    if key:
                        parts.append("[TOOL: {}] {}".format(name, key))
                    else:
                        parts.append("[TOOL: {}]".format(name))

            if not parts:
                continue

            yield {"role": "ASSISTANT", "timestamp": rec.timestamp, "text": "\n".join(parts)}
            count += 1

def _tool_key(name, inp):
    """Extract the most informative field from a tool_use input."""
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

def extract_tools(path, tool_filter="", errors_only=False, limit=0):
    """
    Yield tool call dicts: {timestamp, name, status, key_input, result_preview}.

    Two-pass: first collect all tool_use and tool_result, then join by ID.
    """
    tool_calls = {}
    tool_order = []
    tool_results = {}

    for rec in iter_records(path, types={"user", "assistant"}, skip_noise=True, limit=0):
        ts = rec.timestamp[:19] if rec.timestamp else ""
        content = rec.content

        if rec.type == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tid = block.get("id", "")
                name = block.get("name", "")
                inp = block.get("input", {})
                if not isinstance(inp, dict):
                    inp = {}

                if tool_filter and name != tool_filter:
                    continue

                key = _tool_key(name, inp)
                tool_calls[tid] = (ts, name, key)
                tool_order.append(tid)

        elif rec.type == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id", "")
                is_error = block.get("is_error", False)
                rc = block.get("content", "")
                if isinstance(rc, list):
                    preview = " ".join(
                        b.get("text", "")[:100]
                        for b in rc if isinstance(b, dict)
                    )
                elif isinstance(rc, str):
                    preview = rc[:150].replace("\n", " ").replace("\t", " ")
                else:
                    preview = ""
                tool_results[tid] = ("error" if is_error else "ok", preview)

    count = 0
    for tid in tool_order:
        if tid not in tool_calls:
            continue
        ts, name, key = tool_calls[tid]
        status, preview = tool_results.get(tid, ("ok", "(no result captured)"))

        if errors_only and status != "error":
            continue
        if limit and count >= limit:
            return

        yield {
            "timestamp": ts,
            "name": name,
            "status": status,
            "key_input": key,
            "result_preview": preview,
        }
        count += 1

def extract_files_changed(path, with_versions=False):
    """
    Return list of files edited in the session from the last file-history-snapshot.

    Uses reverse read to find the last snapshot efficiently.
    Returns list of (filepath,) or (filepath, version_count) tuples.
    """
    last_snapshot = None

    # Read file in reverse to find the last snapshot quickly
    try:
        size = os.path.getsize(path)
    except OSError:
        return []

    if size < 50_000_000:  # < 50MB: just iterate forward, it's fast enough
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"file-history-snapshot"' in line:
                    last_snapshot = line
    else:
        # Large file: read from the end in chunks
        last_snapshot = _reverse_find(path, '"file-history-snapshot"')

    if not last_snapshot:
        return []

    try:
        rec = json.loads(last_snapshot.strip())
    except (json.JSONDecodeError, ValueError):
        return []

    backups = rec.get("snapshot", {}).get("trackedFileBackups", {})
    if not isinstance(backups, dict):
        return []

    result = []
    for filepath in sorted(backups.keys()):
        info = backups[filepath]
        if with_versions:
            ver = info.get("version", 1) if isinstance(info, dict) else 1
            result.append((filepath, ver))
        else:
            result.append((filepath,))
    return result

def _reverse_find(path, needle, chunk_size=1_048_576):
    """Find the last line containing needle by reading from end of file."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        pos = file_size
        remainder = b""
        last_match = None

        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size) + remainder
            lines = chunk.split(b"\n")
            remainder = lines[0]  # May be partial line

            for line in reversed(lines[1:]):
                try:
                    decoded = line.decode("utf-8", errors="replace")
                except Exception:
                    continue
                if needle in decoded:
                    return decoded

        # Check remainder (first line of file)
        if remainder:
            try:
                decoded = remainder.decode("utf-8", errors="replace")
                if needle in decoded:
                    return decoded
            except Exception:
                pass

    return None


# ── Project / index discovery (moved to claude_index.py — forwarded below) ──
from echolib._claude_index import (
    all_project_dirs, build_fallback_index, detect_agent_type,
    _encode_project_path, _fast_find_jsonl, find_project_dir,
    list_sessions, load_index, _sanitize_tsv,
    _scan_project_dir, resolve_project_root, broad_list_claude_sessions,
)

def find_subagent_files(session_jsonl_path):
    """
    Find subagent .jsonl files for a session.

    Returns list of Paths to subagent files.
    """
    session_path = Path(session_jsonl_path)
    session_id = session_path.stem
    subagent_dir = session_path.parent / session_id / "subagents"
    if not subagent_dir.exists():
        return []
    return sorted(subagent_dir.glob("agent-*.jsonl"))

def cli_error(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)

def parse_int_or_die(val, name):
    try:
        return int(val)
    except (ValueError, TypeError):
        cli_error("{} must be a number, got: {}".format(name, val))

def _normalize_timestamp(ts):
    """Normalize timestamp to ISO format string.

    Handles:
    - int/float: Unix seconds or milliseconds (auto-detected by magnitude)
    - str: ISO format strings passed through; numeric strings parsed as numbers
    """
    if ts is None or ts == "":
        return ""
    if isinstance(ts, (int, float)):
        from datetime import datetime, timezone
        # Detect millisecond timestamps (>1e12) and convert to seconds
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        except (ValueError, OSError, OverflowError):
            return str(ts)
    if isinstance(ts, str):
        # Try to parse numeric strings (e.g. "1780494657361") as epoch timestamps
        stripped = ts.strip()
        if stripped.isdigit() and len(stripped) >= 10:
            try:
                num = int(stripped)
                return _normalize_timestamp(num)
            except (ValueError, OverflowError):
                pass
        return ts
    return str(ts)
