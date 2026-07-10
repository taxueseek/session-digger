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

def _encode_project_path(project_path):
    """Encode an absolute path to Claude's directory format."""
    # /Users/joker/github/myproject -> -Users-joker-github-myproject
    normalized = project_path.replace("/", "-")
    if normalized.startswith("-"):
        return normalized
    return "-" + normalized

def find_project_dir(target):
    """
    Map a project path to its Claude session directory.

    Returns the Path to the directory, or None if not found.
    Uses exact encoded-path match first, then falls back to full-path matching.
    """
    if not CLAUDE_DIR.exists():
        return None

    # Exact match
    encoded = _encode_project_path(target)
    exact = CLAUDE_DIR / encoded
    if exact.is_dir():
        return exact

    # Try without leading dash variations
    stripped = target.rstrip("/")
    encoded2 = _encode_project_path(stripped)
    exact2 = CLAUDE_DIR / encoded2
    if exact2.is_dir():
        return exact2

    # Full-path substring match: check sessions-index.json originalPath
    for index_path in CLAUDE_DIR.glob("*/sessions-index.json"):
        try:
            with open(index_path, encoding="utf-8") as f:
                data = json.load(f)
            orig = data.get("originalPath", "")
            if orig and (orig == target or orig == stripped):
                return index_path.parent
        except (json.JSONDecodeError, OSError):
            continue

    # Last resort: match the full encoded path (not just basename)
    # This handles minor encoding differences
    target_parts = stripped.strip("/").split("/")
    best_match = None
    best_score = 0

    for d in CLAUDE_DIR.iterdir():
        if not d.is_dir():
            continue
        dirname = d.name.lstrip("-")
        dir_parts = dirname.split("-")

        # Check if target_parts appear as a contiguous subsequence in dir_parts
        if len(target_parts) <= len(dir_parts):
            score = 0
            for i in range(len(dir_parts) - len(target_parts) + 1):
                match = all(
                    target_parts[j] == dir_parts[i + j]
                    for j in range(len(target_parts))
                )
                if match:
                    score = len(target_parts)
                    break
            if score > best_score:
                best_score = score
                best_match = d

    return best_match

def resolve_project_root(project_dir):
    """
    Map a Claude encoded project directory back to its real filesystem path.
    Returns: absolute path string, or None if unresolvable.
    """
    project_dir = Path(project_dir) if not isinstance(project_dir, Path) else project_dir
    if not project_dir.is_dir():
        return None

    # Strategy 1: sessions-index.json
    index_path = project_dir / "sessions-index.json"
    if index_path.exists():
        try:
            with open(index_path, encoding="utf-8") as f:
                data = json.load(f)
            orig = data.get("originalPath", "")
            if orig and os.path.isdir(orig):
                return orig
            entries = data.get("entries", [])
            if isinstance(entries, list):
                for s in entries:
                    pp = s.get("projectPath", "")
                    if pp and os.path.isdir(pp):
                        return pp
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    # Strategy 2: best-effort decode
    dirname = project_dir.name
    if dirname.startswith("-"):
        decoded = "/" + dirname[1:].replace("-", "/")
        if os.path.isdir(decoded):
            return decoded

    return None

def all_project_dirs():
    """Yield all project directories under ~/.claude/projects/."""
    if not CLAUDE_DIR.exists():
        return
    for d in CLAUDE_DIR.iterdir():
        if d.is_dir() and d.name != ".":
            yield d

def _sanitize_tsv(s, max_len=0):
    """Clean a string for TSV output."""
    s = s.replace("\t", " ").replace("\n", " ")
    if max_len and len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s

def load_index(index_path):
    """Load sessions from a sessions-index.json file. Returns list of SessionMeta."""
    try:
        with open(index_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    entries = data.get("entries", [])
    result = []
    for e in entries:
        result.append(SessionMeta(
            session_id=e.get("sessionId", ""),
            full_path=e.get("fullPath", ""),
            created=e.get("created", ""),
            modified=e.get("modified", ""),
            message_count=e.get("messageCount", 0),
            git_branch=e.get("gitBranch", ""),
            summary=e.get("summary", ""),
            first_prompt=e.get("firstPrompt", ""),
            project_path=e.get("projectPath", ""),
        ))
    return result

def build_fallback_index(project_dir):
    """
    Build index entries for a project directory that has no sessions-index.json.

    Reads the first user message and last summary from each .jsonl file.
    Caches the result in .session-digger-index.json within the project dir.
    """
    project_dir = Path(project_dir)
    cache_path = project_dir / ".session-digger-index.json"

    # Check cache freshness
    jsonl_files = sorted(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return []

    latest_mtime = max(f.stat().st_mtime for f in jsonl_files)

    if cache_path.exists():
        try:
            cache_mtime = cache_path.stat().st_mtime
            if cache_mtime >= latest_mtime:
                with open(cache_path, encoding="utf-8") as f:
                    cached = json.load(f)
                return [SessionMeta(**e) for e in cached]
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    # Build index from raw files
    entries = []
    # Derive project_path from directory name
    dir_name = project_dir.name
    # Reverse the encoding: -Users-joker-github-myproject -> /Users/joker/github/myproject
    # This is lossy (can't distinguish - that was / vs literal -), but best effort
    project_path = "/" + dir_name.lstrip("-").replace("-", "/") if dir_name.startswith("-") else dir_name

    for jsonl_path in jsonl_files:
        # Skip subagent directories
        if "subagents" in str(jsonl_path):
            continue

        session_id = jsonl_path.stem
        first_prompt = ""
        summary = ""
        first_ts = ""
        last_ts = ""
        msg_count = 0
        branch = ""

        try:
            with open(jsonl_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    # Quick string checks before parsing
                    if '"progress"' in line or '"queue-operation"' in line:
                        continue

                    try:
                        d = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    rtype = d.get("type", "")
                    ts = d.get("timestamp", "")

                    if ts:
                        if not first_ts or ts < first_ts:
                            first_ts = ts
                        if ts > last_ts:
                            last_ts = ts

                    if not branch:
                        branch = d.get("gitBranch", "")

                    if rtype == "user":
                        if d.get("isMeta") or d.get("isCompactSummary"):
                            continue
                        msg = d.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        content = msg.get("content", "")
                        if isinstance(content, str) and content.strip():
                            msg_count += 1
                            if not first_prompt:
                                fp = content.strip()
                                if not fp.startswith("<") and len(fp) > 2:
                                    first_prompt = fp[:180]
                        elif isinstance(content, list):
                            has_tr = any(
                                isinstance(b, dict) and b.get("type") == "tool_result"
                                for b in content
                            )
                            if not has_tr:
                                msg_count += 1
                                if not first_prompt:
                                    texts = [
                                        b.get("text", "")
                                        for b in content
                                        if isinstance(b, dict) and b.get("type") == "text"
                                    ]
                                    fp = " ".join(t for t in texts if t).strip()
                                    if fp and not fp.startswith("<") and len(fp) > 2:
                                        first_prompt = fp[:180]

                    elif rtype == "assistant":
                        msg = d.get("message", {})
                        if isinstance(msg, dict) and msg.get("model") != "<synthetic>":
                            msg_count += 1

                    elif rtype == "summary":
                        summary = d.get("summary", "")

        except OSError:
            continue

        entries.append(SessionMeta(
            session_id=session_id,
            full_path=str(jsonl_path),
            created=first_ts,
            modified=last_ts,
            message_count=msg_count,
            git_branch=branch,
            summary=summary,
            first_prompt=first_prompt,
            project_path=project_path,
        ))

    # Cache for next time
    try:
        cache_data = []
        for e in entries:
            cache_data.append({k: getattr(e, k) for k in SessionMeta.__slots__})
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False)
    except OSError:
        pass  # Cache write failure is non-fatal

    return entries

def _fast_find_jsonl(directory):
    """快速查找JSONL文件，使用os.scandir()比Path.rglob()快2-3倍。"""
    results = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False) and entry.name.endswith(".jsonl"):
                    results.append(Path(entry.path))
                elif entry.is_dir(follow_symlinks=False):
                    results.extend(_fast_find_jsonl(entry.path))
    except OSError:
        pass
    return results

def _scan_project_dir(project_dir, since="", grep_pat=""):
    """扫描单个项目目录，返回会话列表。用于并行处理。"""
    index_path = project_dir / "sessions-index.json"
    if index_path.exists():
        entries = load_index(index_path)
    else:
        entries = build_fallback_index(project_dir)
    
    # 应用过滤
    grep_lower = grep_pat.lower() if grep_pat else ""
    filtered = []
    for e in entries:
        if since and str(e.created)[:10] < since:
            continue
        if grep_lower:
            haystack = (str(e.summary) + " " + str(e.first_prompt)).lower()
            if grep_lower not in haystack:
                continue
        filtered.append(e)
    return filtered

def list_sessions(scope="current", target=None, limit=50, since="", grep_pat=""):
    """
    List sessions matching criteria.

    Args:
        scope: "current", "all", or "path".
        target: Project path (used when scope is "path" or "current" uses cwd).
        limit: Maximum results.
        since: ISO date string (YYYY-MM-DD) minimum.
        grep_pat: Case-insensitive substring filter on summary+first_prompt.

    Returns list of SessionMeta sorted by created descending.
    """
    grep_lower = grep_pat.lower() if grep_pat else ""
    all_entries = []

    if scope == "all":
        # 并行处理所有项目目录，使用线程池加速I/O密集型任务
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            # 提交所有项目目录的扫描任务
            futures = {}
            for project_dir in all_project_dirs():
                future = executor.submit(_scan_project_dir, project_dir, since, grep_pat)
                futures[future] = project_dir
            
            # 收集结果
            for future in concurrent.futures.as_completed(futures):
                try:
                    results = future.result()
                    all_entries.extend(results)
                except Exception:
                    # 静默处理异常，继续处理其他项目
                    pass
    else:
        if scope == "current":
            target = target or os.getcwd()
        proj_dir = find_project_dir(target)
        if not proj_dir:
            return []
        index_path = proj_dir / "sessions-index.json"
        if index_path.exists():
            all_entries = load_index(index_path)
        else:
            all_entries = build_fallback_index(proj_dir)

    # 过滤已经在_scan_project_dir中完成，这里只进行排序
    # Sort by created descending
    all_entries.sort(key=lambda e: str(e.created), reverse=True)

    return all_entries[:limit]

def broad_list_claude_sessions(limit=50, keyword=""):
    """
    广域扫描 Claude 会话，直接扫描 JSONL 文件而非依赖索引。
    与 sd-recall.py 的 find_sessions() 行为一致。
    """
    entries = []
    seen = set()
    for d in all_project_dirs():
        for jf in _fast_find_jsonl(d):
            spath = str(jf)
            if "subagents" in spath or ".jsonl.summary" in spath or ".jsonl." in jf.name:
                continue
            key = str(jf.resolve())
            if key in seen:
                continue
            seen.add(key)
            entries.append(jf)
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    result = []
    for jf in entries[:limit * 3]:
        try:
            sid = jf.stem
            mtime = _normalize_timestamp(jf.stat().st_mtime)
            stats = session_stats(jf)
            summary = stats.get("summary", "")[:100]
            first_prompt = ""
            if stats.get("user_messages", 0) > 0:
                for m in extract_messages(jf, role="user", limit=1):
                    first_prompt = m["text"][:200]
                    break
            if keyword and keyword.lower() not in (summary + " " + first_prompt).lower():
                continue
            result.append(SessionMeta(
                session_id=sid, full_path=str(jf),
                created=stats.get("started", mtime),
                modified=stats.get("ended", mtime),
                message_count=stats.get("user_messages", 0) + stats.get("assistant_messages", 0),
                git_branch=stats.get("branch", ""),
                summary=summary,
                first_prompt=first_prompt,
                project_path="",
            ))
        except Exception:
            continue
    return result[:limit]

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

def detect_agent_type(path=None):
    """
    Detect which agent produced the session data.

    Returns: "claude", "grok", "kimi_code", "codex", "workbuddy", "trae_cn",
             "both", or "unknown".
    Note: "kimi" (non-code) is not supported — those sessions use a
          fundamentally different format and are silently ignored.
    """
    if path:
        raw = str(path)
        # Virtual URI schemes (SQLite-backed adapters) — before Path.resolve()
        if raw.startswith("dimcode://") or raw.startswith("dimcode:"):
            return "dimcode"
        if "://" in raw and not raw.startswith("/") and not raw.startswith("file:"):
            scheme = raw.split("://", 1)[0].lower()
            if scheme in ("dimcode", "dim", "zcode", "kimi_code", "claude", "grok", "codex"):
                return "dimcode" if scheme == "dim" and "sess_" in raw else scheme

        try:
            p = Path(path).expanduser().resolve()
            ps = str(p)
        except Exception:
            ps = raw

        # Check more specific paths BEFORE the .jsonl fallback (which is broad)
        grok_sessions_marker = str(Path.home() / ".grok" / "sessions")
        if ps.startswith(grok_sessions_marker):
            return "grok"

        kimi_code_sessions_marker = str(KIMI_CODE_DIR)
        if ps.startswith(kimi_code_sessions_marker):
            return "kimi_code"

        codex_marker = str(CODEX_DIR)
        if ps.startswith(codex_marker):
            return "codex"

        workbuddy_marker = str(WORKBUDDY_DIR)
        if ps.startswith(workbuddy_marker):
            return "workbuddy"

        trae_marker = str(TRAE_DIR)
        if ps.startswith(trae_marker):
            return "trae_cn"

        zcode_marker = str(ZCODE_DIR)
        if ps.startswith(zcode_marker):
            return "zcode"

        dim_marker = str(DIM_DIR)
        if ps.startswith(dim_marker):
            return "dim"

        # DimCode SQLite file path
        if "dimcode" in ps and ps.endswith((".sqlite", ".db")):
            return "dimcode"

        reasonix_marker = str(REASONIX_DIR)
        if ps.startswith(reasonix_marker):
            return "reasonix"

        # Claude: must be .claude/projects ancestor (NOT just any .jsonl)
        claude_projects_marker = str(Path.home() / ".claude" / "projects")
        if ps.startswith(claude_projects_marker):
            return "claude"

        # Unknown: .jsonl not in any known environment directory
        if p.name.endswith(".jsonl"):
            return "unknown"

    # Fallback: check which supported directories exist
    claude_dir = Path.home() / ".claude" / "projects"
    existing = []
    if claude_dir.exists():
        existing.append("claude")
    if GROK_DIR.exists():
        existing.append("grok")
    if KIMI_CODE_DIR.exists():
        existing.append("kimi_code")
    if CODEX_DIR.exists():
        existing.append("codex")
    if WORKBUDDY_DIR.exists():
        existing.append("workbuddy")
    if TRAE_DIR.exists():
        existing.append("trae_cn")
    if ZCODE_DIR.exists():
        existing.append("zcode")
    if DIM_DIR.exists():
        existing.append("dim")
    if REASONIX_DIR.exists():
        existing.append("reasonix")
    if len(existing) == 0:
        return "unknown"
    if len(existing) == 1:
        return existing[0]
    return "both"

def _normalize_model_name(raw):
    """Normalize Grok model names to canonical form."""
    if not raw:
        return raw
    mapping = {
        "longcat": "LongCat-2.0",
    }
    return mapping.get(raw, raw)
