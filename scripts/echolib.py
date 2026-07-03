"""
echolib.py — Core parsing library for session-digger.

Single-file, stdlib-only (Python 3.6+). All scripts are thin wrappers around this.

Classes:
    Record      — A parsed JSONL record with type-aware accessors.
    SessionMeta — Lightweight session metadata (from index or built from .jsonl).
    Memory      — A parsed memory file with frontmatter fields.

Functions:
    iter_records()        — Stream records from a .jsonl file with filtering.
    detect_schema()       — Probe a .jsonl file and report its structure.
    session_stats()       — Compute statistics for a session file.
    extract_messages()    — Yield human-readable messages from a session.
    extract_tools()       — Yield tool calls joined with their results.
    extract_files_changed() — Get files edited from the last snapshot (reverse-read).
    list_sessions()       — List sessions across projects (index + fallback).
    find_project_dir()    — Map a project path to its Claude session directory.
    build_fallback_index() — Build index entries for projects without sessions-index.json.

    # Memory management:
    parse_frontmatter()   — Parse simple key:value frontmatter from .md files.
    resolve_project_root() — Map encoded project dir back to filesystem path.
    all_memory_dirs()     — Find all projects with memory/ directories.
    iter_memories()       — Yield parsed Memory objects from a memory directory.
    staleness_score()     — Compute heuristic staleness for a memory.
    estimate_tokens()     — Rough token count estimate.
    memory_stats()        — Aggregate stats for one project's memories.
"""

import json
import math
import os
import sys
import concurrent.futures
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional env-adapter imports (graceful degradation if missing)
# ---------------------------------------------------------------------------
try:
    import importlib.util as _ilu
    _env_adapters_path = Path(__file__).parent / "env-adapters.py"
    if _env_adapters_path.exists():
        _spec = _ilu.spec_from_file_location("env_adapters", str(_env_adapters_path))
        _env_adapters = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_env_adapters)
        codex_list_sessions = _env_adapters.codex_list_sessions
        workbuddy_list_sessions = _env_adapters.workbuddy_list_sessions
        trae_list_sessions = _env_adapters.trae_list_sessions
    else:
        codex_list_sessions = None
        workbuddy_list_sessions = None
        trae_list_sessions = None
except Exception:
    codex_list_sessions = None
    workbuddy_list_sessions = None
    trae_list_sessions = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_DIR = Path.home() / ".claude" / "projects"

NOISE_TYPES = frozenset({"progress", "queue-operation"})

KNOWN_TYPES = frozenset({
    "user", "assistant", "system", "summary", "progress",
    "queue-operation", "file-history-snapshot", "pr-link",
})

# Pre-filter strings for noise skipping (avoids json.loads)
_NOISE_STRINGS = ('"queue-operation"', '"progress"')


# ---------------------------------------------------------------------------
# Record wrapper
# ---------------------------------------------------------------------------

class Record:
    """Thin wrapper around a parsed JSONL dict with convenience accessors."""

    __slots__ = ("_d",)

    def __init__(self, d):
        self._d = d

    @property
    def raw(self):
        return self._d

    @property
    def type(self):
        return self._d.get("type", "")

    @property
    def timestamp(self):
        return self._d.get("timestamp", "")

    @property
    def message(self):
        return self._d.get("message") or {}

    @property
    def content(self):
        msg = self.message
        return msg.get("content", "") if isinstance(msg, dict) else ""

    @property
    def model(self):
        msg = self.message
        return msg.get("model", "") if isinstance(msg, dict) else ""

    @property
    def usage(self):
        msg = self.message
        return msg.get("usage", {}) if isinstance(msg, dict) else {}

    @property
    def uuid(self):
        return self._d.get("uuid", "")

    @property
    def session_id(self):
        return self._d.get("sessionId", "")

    @property
    def git_branch(self):
        return self._d.get("gitBranch", "")

    @property
    def slug(self):
        return self._d.get("slug", "")

    @property
    def version(self):
        return self._d.get("version", "")

    @property
    def subtype(self):
        return self._d.get("subtype", "")

    def get(self, key, default=None):
        return self._d.get(key, default)

    def is_noise(self):
        return self.type in NOISE_TYPES

    def is_meta_user(self):
        return self._d.get("isMeta", False)

    def is_compact_summary(self):
        return self._d.get("isCompactSummary", False)

    def is_synthetic(self):
        return self.model == "<synthetic>"

    def is_tool_result_message(self):
        """True if this is a user record that only contains tool_result blocks."""
        if self.type != "user":
            return False
        c = self.content
        if not isinstance(c, list):
            return False
        return any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in c
        )

    def text_content(self):
        """Extract human-readable text from this record's content."""
        c = self.content
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            parts = []
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    t = b.get("text", "").strip()
                    if t:
                        parts.append(t)
            return "\n".join(parts)
        return ""


# ---------------------------------------------------------------------------
# Core iterator
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Schema detection
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Session statistics (single-pass)
# ---------------------------------------------------------------------------

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

            if ts:
                if not stats["started"] or ts < stats["started"]:
                    stats["started"] = ts
                if ts > stats["ended"]:
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


# ---------------------------------------------------------------------------
# Message extraction
# ---------------------------------------------------------------------------

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
            if not text or text.startswith("<system-reminder>") or text.startswith("[Request interrupted"):
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


# ---------------------------------------------------------------------------
# Tool extraction
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Files changed (reverse-read for last snapshot)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Memory file parsing
# ---------------------------------------------------------------------------

def parse_frontmatter(text):
    """
    Parse simple key: value frontmatter from a markdown string.

    Handles ONLY the subset used by Claude Code memory files:
    - Block delimited by --- on its own line (first line must be ---)
    - Key-value pairs: "key: value" (one per line, value is everything after first ": ")
    - No nested structures, no lists, no multi-line values

    Returns: (dict, body_string)
    If no valid frontmatter: (empty dict, full text)
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx < 0:
        return {}, text

    fm = {}
    for line in lines[1:end_idx]:
        line = line.strip()
        if not line:
            continue
        sep = line.find(": ")
        if sep > 0:
            key = line[:sep].strip()
            value = line[sep + 2:].strip()
            fm[key] = value

    body = "\n".join(lines[end_idx + 1:])
    return fm, body


class Memory:
    """A parsed memory file."""

    __slots__ = ("path", "name", "description", "type", "content",
                 "project", "project_dir", "mtime", "size")

    def __init__(self, **kwargs):
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k))


def iter_memories(memory_dir):
    """
    Yield Memory objects from a memory/ directory (or a directory containing
    .md memory files).

    Layout 1 (index + files): MEMORY.md is index, individual .md files have
    frontmatter. Yields individual files, skips MEMORY.md and archive/.
    Layout 2 (standalone): only MEMORY.md exists (no other .md files outside
    archive/). Yields single Memory with type=unknown.
    """
    memory_dir = str(memory_dir)

    # Collect .md files excluding MEMORY.md and archive/
    md_files = []
    for entry in sorted(os.listdir(memory_dir)):
        full = os.path.join(memory_dir, entry)
        if not os.path.isfile(full):
            continue
        if not entry.endswith(".md"):
            continue
        if entry == "MEMORY.md":
            continue
        md_files.append(full)

    if md_files:
        # Layout 1: index + individual files
        for fpath in md_files:
            try:
                with open(fpath, encoding="utf-8") as f:
                    text = f.read()
                stat = os.stat(fpath)
            except OSError:
                continue
            fm, body = parse_frontmatter(text)
            yield Memory(
                path=fpath,
                name=fm.get("name"),
                description=fm.get("description"),
                type=fm.get("type", "unknown"),
                content=body,
                project=os.path.basename(os.path.dirname(memory_dir))
                    if os.path.basename(memory_dir) == "memory"
                    else os.path.basename(memory_dir),
                project_dir=os.path.dirname(memory_dir)
                    if os.path.basename(memory_dir) == "memory"
                    else memory_dir,
                mtime=stat.st_mtime,
                size=stat.st_size,
            )
    else:
        # Layout 2: standalone MEMORY.md (or empty)
        mem_path = os.path.join(memory_dir, "MEMORY.md")
        if not os.path.isfile(mem_path):
            return
        try:
            with open(mem_path, encoding="utf-8") as f:
                text = f.read()
            stat = os.stat(mem_path)
        except OSError:
            return
        # Skip if effectively empty (just a heading)
        body = text.strip()
        lines = body.split("\n")
        content_lines = [l for l in lines if not l.startswith("# ")]
        content = "\n".join(content_lines).strip()
        if not content:
            return
        project_name = (os.path.basename(os.path.dirname(memory_dir))
                        if os.path.basename(memory_dir) == "memory"
                        else os.path.basename(memory_dir))
        yield Memory(
            path=mem_path,
            name=None,
            description=None,
            type="unknown",
            content=content,
            project=project_name,
            project_dir=os.path.dirname(memory_dir)
                if os.path.basename(memory_dir) == "memory"
                else memory_dir,
            mtime=stat.st_mtime,
            size=stat.st_size,
        )


# ---------------------------------------------------------------------------
# Staleness scoring and memory statistics
# ---------------------------------------------------------------------------

class StaleScore:
    """Staleness assessment for a memory."""
    __slots__ = ("score", "reasons", "action")
    def __init__(self, **kwargs):
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k))

class MemoryStats:
    """Aggregate stats for one project's memories."""
    __slots__ = ("project", "file_count", "total_bytes",
                 "estimated_tokens", "staleness_distribution")
    def __init__(self, **kwargs):
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k))

_HALF_LIVES = {
    "project": 14, "feedback": 90, "user": 180,
    "reference": 60, "value": 365, "unknown": 30,
}

def staleness_score(memory):
    """Compute type-based heuristic staleness score using exponential decay."""
    import time
    age_days = (time.time() - memory.mtime) / 86400.0
    half_life = _HALF_LIVES.get(memory.type, 30)
    score = int(100 * (1 - math.exp(-age_days * math.log(2) / half_life)))
    score = max(0, min(100, score))
    reasons = []
    if age_days > half_life:
        reasons.append("older than half-life (%d days for type=%s)" % (half_life, memory.type))
    if age_days > half_life * 3:
        reasons.append("significantly past expiry")
    if score < 50:
        action = "keep"
    elif score < 75:
        action = "review"
    else:
        action = "prune"
    return StaleScore(score=score, reasons=reasons, action=action)

def estimate_tokens(text):
    """Rough token estimate: len(text) // 4."""
    return len(text) // 4

def all_memory_dirs():
    """Scan ~/.claude/projects/ for directories containing memory/ subdirs.
    Returns list of (encoded_project_name, memory_dir_path) tuples."""
    result = []
    if not CLAUDE_DIR.exists():
        return result
    for d in CLAUDE_DIR.iterdir():
        if not d.is_dir():
            continue
        mem_dir = d / "memory"
        if mem_dir.is_dir():
            result.append((d.name, str(mem_dir)))
    return result

def memory_stats(memory_dir):
    """Aggregate stats for one project's memories."""
    mems = list(iter_memories(memory_dir))
    total_bytes = sum(m.size for m in mems)
    total_content = "".join(m.content for m in mems)
    dist = {"fresh": 0, "aging": 0, "review": 0, "stale": 0}
    for m in mems:
        ss = staleness_score(m)
        if ss.score < 25:
            dist["fresh"] += 1
        elif ss.score < 50:
            dist["aging"] += 1
        elif ss.score < 75:
            dist["review"] += 1
        else:
            dist["stale"] += 1
    project_name = (os.path.basename(os.path.dirname(memory_dir))
                    if os.path.basename(memory_dir) == "memory"
                    else os.path.basename(memory_dir))
    return MemoryStats(
        project=project_name, file_count=len(mems),
        total_bytes=total_bytes,
        estimated_tokens=estimate_tokens(total_content),
        staleness_distribution=dist,
    )


# ---------------------------------------------------------------------------
# Project directory resolution
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Session listing (with fallback index building)
# ---------------------------------------------------------------------------

class SessionMeta:
    """Lightweight session metadata."""

    __slots__ = (
        "session_id", "full_path", "created", "modified",
        "message_count", "git_branch", "summary", "first_prompt",
        "project_path",
    )

    def __init__(self, **kwargs):
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k, ""))

    def to_tsv(self):
        fields = [
            str(self.session_id),
            str(self.created),
            str(self.modified),
            str(self.message_count),
            str(self.git_branch),
            _sanitize_tsv(str(self.summary), 80),
            _sanitize_tsv(str(self.first_prompt), 100),
            str(self.project_path),
            str(self.full_path),
        ]
        return "\t".join(fields)


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


# ---------------------------------------------------------------------------
# Subagent discovery
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

def cli_error(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def parse_int_or_die(val, name):
    try:
        return int(val)
    except (ValueError, TypeError):
        cli_error("{} must be a number, got: {}".format(name, val))


# ---------------------------------------------------------------------------
# Grok Build adapter
# ---------------------------------------------------------------------------

GROK_DIR = Path.home() / ".grok" / "sessions"
GROK_SEARCH_DB = GROK_DIR / "session_search.sqlite"
KIMI_DIR = Path.home() / ".kimi" / "sessions"
KIMI_CODE_DIR = Path.home() / ".kimi-code" / "sessions"


def _normalize_timestamp(ts):
    """Normalize timestamp to ISO format string. Handles int/float (Unix seconds or milliseconds) and str."""
    if ts is None:
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
        return ts
    return str(ts)


def detect_agent_type(path=None):
    """
    Detect which agent produced the session data.

    Returns: "claude", "grok", "kimi_code", "both", or "unknown".
    Note: "kimi" (non-code) is not supported — those sessions use a
          fundamentally different format and are silently ignored.
    """
    if path:
        p = Path(path).resolve()
        ps = str(p)
        # Exact ancestor check: .grok/sessions anywhere in path
        grok_sessions_marker = str(Path.home() / ".grok" / "sessions")
        if ps.startswith(grok_sessions_marker):
            return "grok"
        # Exact ancestor check: .kimi-code/sessions (check before .kimi)
        kimi_code_sessions_marker = str(KIMI_CODE_DIR)
        if ps.startswith(kimi_code_sessions_marker):
            return "kimi_code"
        # Claude: .jsonl file or .claude/projects ancestor
        claude_projects_marker = str(Path.home() / ".claude" / "projects")
        if p.name.endswith(".jsonl") or ps.startswith(claude_projects_marker):
            return "claude"

    # Fallback: check which supported directories exist
    claude_dir = Path.home() / ".claude" / "projects"
    grok_dir = GROK_DIR
    kimi_code_dir = KIMI_CODE_DIR
    existing = []
    if claude_dir.exists():
        existing.append("claude")
    if grok_dir.exists():
        existing.append("grok")
    if kimi_code_dir.exists():
        existing.append("kimi_code")
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


def _encode_grok_cwd(cwd):
    """Encode a path to Grok's URL-encoded format."""
    import urllib.parse
    return urllib.parse.quote(cwd, safe='')


def _decode_grok_cwd(encoded):
    """Decode Grok's URL-encoded path back to filesystem path."""
    import urllib.parse
    return urllib.parse.unquote(encoded)


def grok_list_sessions(cwd=None, limit=50, keyword=""):
    """
    List Grok sessions. Uses session_search.sqlite if available,
    otherwise falls back to scanning summary.json files.

    Returns list of SessionMeta (reusing the existing class).
    """
    if not GROK_DIR.exists():
        return []

    # Try SQLite FTS5 search first
    if GROK_SEARCH_DB.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(GROK_SEARCH_DB))
            if keyword:
                rows = conn.execute("""
                    SELECT s.session_id, s.cwd, s.updated_at, s.title, s.content
                    FROM session_docs s
                    JOIN session_docs_fts fts ON s.rowid = fts.rowid
                    WHERE session_docs_fts MATCH ?
                    ORDER BY s.updated_at DESC LIMIT ?
                """, (keyword, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT session_id, cwd, updated_at, title, content
                    FROM session_docs
                    ORDER BY updated_at DESC LIMIT ?
                """, (limit,)).fetchall()
            conn.close()

            entries = []
            for row in rows:
                sid, session_cwd, updated_at, title, content = row
                # Filter by cwd if specified
                if cwd and session_cwd != cwd:
                    continue
                # Build full_path from cwd + session_id
                encoded_cwd = _encode_grok_cwd(session_cwd)
                full_path = str(GROK_DIR / encoded_cwd / sid)
                created = updated_at
                entries.append(SessionMeta(
                    session_id=sid,
                    full_path=full_path,
                    created=_normalize_timestamp(updated_at),
                    modified=_normalize_timestamp(updated_at),
                    message_count=0,
                    git_branch="",
                    summary=title or "",
                    first_prompt=(content or "")[:100],
                    project_path=session_cwd,
                ))
            return entries
        except Exception:
            pass  # Fall through to filesystem scan

    # Fallback: scan summary.json files
    import urllib.parse
    entries = []
    for d in sorted(GROK_DIR.iterdir()):
        if not d.is_dir():
            continue
        session_cwd = _decode_grok_cwd(d.name)
        if cwd and session_cwd != cwd:
            continue
        for session_dir in sorted(d.iterdir()):
            if not session_dir.is_dir():
                continue
            summary_file = session_dir / "summary.json"
            if not summary_file.exists():
                continue
            try:
                with open(summary_file, encoding="utf-8") as f:
                    summary = json.load(f)
                info = summary.get("info", {})
                sid = info.get("id") or summary.get("session_id") or session_dir.name
                created = info.get("created_at") or summary.get("created_at") or ""
                updated = info.get("updated_at") or summary.get("updated_at") or ""
                title = (summary.get("session_summary")
                         or summary.get("generated_title")
                         or summary.get("summary")
                         or "")
                entries.append(SessionMeta(
                    session_id=sid,
                    full_path=str(session_dir),
                    created=_normalize_timestamp(created),
                    modified=_normalize_timestamp(updated),
                    message_count=summary.get("num_messages", 0),
                    git_branch="",
                    summary=title,
                    first_prompt="",
                    project_path=session_cwd,
                ))
            except (json.JSONDecodeError, OSError):
                continue

    entries.sort(key=lambda e: str(e.created), reverse=True)
    return entries[:limit]


def grok_session_stats(session_dir):
    """
    Get session statistics for a Grok session.

    Reads signals.json (pre-aggregated) instead of parsing JSONL.
    Returns a dict compatible with the existing session_stats() output format.
    """
    session_dir = Path(session_dir)
    signals_file = session_dir / "signals.json"
    summary_file = session_dir / "summary.json"

    stats = {
        "slug": "",
        "model": "",
        "branch": "",
        "started": "",
        "ended": "",
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
        "files_edited": "N/A (use rewind_points.jsonl)",
        "errors": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "compactions": 0,
        "summary": "",
    }

    # Timestamps come from summary.json (signals.json has no time fields)
    if summary_file.exists():
        try:
            with open(summary_file, encoding="utf-8") as f:
                summary = json.load(f)
            info = summary.get("info", {})
            stats["started"] = info.get("created_at") or summary.get("created_at") or ""
            stats["ended"] = info.get("updated_at") or summary.get("updated_at") or ""
            stats["summary"] = (summary.get("session_summary")
                                or summary.get("generated_title")
                                or summary.get("summary")
                                or "")
            raw_model = (summary.get("current_model_id")
                         or info.get("model_id")
                         or "")
            stats["model"] = _normalize_model_name(raw_model)
        except (json.JSONDecodeError, OSError):
            pass

    # Stats come from signals.json (pre-aggregated)
    if signals_file.exists():
        try:
            with open(signals_file, encoding="utf-8") as f:
                signals = json.load(f)
            stats["user_messages"] = signals.get("userMessageCount", 0)
            stats["assistant_messages"] = signals.get("assistantMessageCount", 0)
            stats["tool_calls"] = signals.get("toolCallCount", 0)
            stats["errors"] = signals.get("errorCount", 0)
            stats["compactions"] = signals.get("compactionCount", 0)
            if not stats["model"]:
                stats["model"] = signals.get("primaryModelId", "")
            context_used = signals.get("contextTokensUsed", 0)
            stats["input_tokens"] = context_used
            stats["output_tokens"] = 0
        except (json.JSONDecodeError, OSError):
            pass

    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


def grok_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """
    Extract human-readable messages from a Grok session.

    Reads chat_history.jsonl (not events.jsonl).
    Grok's tool calls are in chat_history.jsonl too.

    Yields dicts with keys: role, timestamp, text.
    """
    session_dir = Path(session_dir)
    chat_file = session_dir / "chat_history.jsonl"
    if not chat_file.exists():
        return

    count = 0
    with open(chat_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            rtype = record.get("type", "")
            content = record.get("content", "")
            # Extract timestamp: try 'timestamp' then 'ts', normalize to ISO
            raw_ts = record.get("timestamp") or record.get("ts") or ""
            ts = _normalize_timestamp(raw_ts) if raw_ts else ""

            # Skip tool_result records (not human messages)
            if rtype == "tool_result":
                continue

            if rtype == "user":
                if role not in ("user", "both"):
                    continue
                text = _grok_join_content(content)
                if text:
                    import re
                    # Skip system-injected context blocks
                    if text.startswith("<user_info>") or text.startswith("<system-reminder>"):
                        continue
                    # Extract actual user message from <user_query>...</user_query>
                    # Handle unclosed tags (e.g., when <skill_information> follows)
                    m = re.search(r'<user_query>(.*?)(?:</user_query>|$)', text, re.DOTALL)
                    if m:
                        text = m.group(1).strip()
                    yield {"role": "USER", "timestamp": ts, "text": text}
                    count += 1

            elif rtype == "assistant":
                if role not in ("assistant", "both"):
                    continue
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype == "text":
                            t = block.get("text", "").strip()
                            if t:
                                parts.append(t)
                        elif btype == "tool_use":
                            name = block.get("name", "?")
                            inp = block.get("input", {})
                            if not isinstance(inp, dict):
                                inp = {}
                            key = _tool_key(name, inp)
                            if key:
                                parts.append("[TOOL: {}] {}".format(name, key))
                            else:
                                parts.append("[TOOL: {}]".format(name))
                    if parts:
                        yield {"role": "ASSISTANT", "timestamp": ts, "text": "\n".join(parts)}
                        count += 1

            if limit and count >= limit:
                return


def _grok_join_content(content):
    """Join Grok content into a single string, handling char arrays and text blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)  # char-array defense
            elif isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "").strip()
                if t:
                    parts.append(t)
        return "".join(parts)  # join without spaces for char arrays
    return ""


def grok_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """
    Extract tool calls from a Grok session.

    Grok splits tool data across two files:
    - events.jsonl: tool_started (name, id, ts) + tool_completed (outcome)
    - chat_history.jsonl: tool_result (tool_call_id, content)

    We join by tool_call_id (not proximity) for correctness.

    Yields tool call dicts: {timestamp, name, status, key_input, result_preview}.
    """
    session_dir = Path(session_dir)
    events_file = session_dir / "events.jsonl"
    chat_file = session_dir / "chat_history.jsonl"

    # Collect tool started events: id -> (ts, name)
    tool_by_id = {}  # tool_call_id -> {"ts": str, "name": str}
    if events_file.exists():
        with open(events_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                etype = event.get("type", "")
                if etype == "tool_started":
                    name = event.get("tool_name", "")
                    if tool_filter and name != tool_filter:
                        continue
                    tid = event.get("tool_call_id") or event.get("id", "")
                    ts = event.get("ts", "")
                    tool_by_id[tid] = {"ts": ts, "name": name}
                elif etype == "tool_completed":
                    name = event.get("tool_name", "")
                    outcome = event.get("outcome", "success")
                    tid = event.get("tool_call_id") or event.get("id", "")
                    if tid in tool_by_id:
                        tool_by_id[tid]["outcome"] = outcome
                    # If not in tool_by_id yet (tool_started not captured), add
                    elif name:
                        # Use name as fallback key for events.jsonl where id field name varies
                        for existing_tid, info in tool_by_id.items():
                            if info.get("name") == name and "outcome" not in info:
                                info["outcome"] = outcome
                                break

    # Collect tool results: tool_call_id -> content preview
    results_by_id = {}
    if chat_file.exists():
        with open(chat_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if record.get("type") != "tool_result":
                    continue
                tid = record.get("tool_call_id", "")
                rc = record.get("content", "")
                if isinstance(rc, str):
                    preview = rc[:150].replace("\n", " ").replace("\t", " ")
                elif isinstance(rc, list):
                    preview = " ".join(
                        b.get("text", "")[:100]
                        for b in rc if isinstance(b, dict)
                    )
                else:
                    preview = ""
                results_by_id[tid] = preview

    # Yield by ID match, fallback to name-based
    count = 0
    for tid, info in sorted(tool_by_id.items()):
        name = info.get("name", "")
        ts = info.get("ts", "")
        outcome = info.get("outcome", "success")
        status = "error" if outcome == "failure" else "ok"
        if errors_only and status != "error":
            continue

        preview = results_by_id.get(tid, "(no result)")
        if preview == "(no result)":
            # Fallback: try to find a result with matching name
            for rtid, rpreview in results_by_id.items():
                if rtid and rtid not in tool_by_id:
                    preview = rpreview
                    del results_by_id[rtid]
                    break

        if limit and count >= limit:
            return
        yield {
            "timestamp": ts,
            "name": name,
            "status": status,
            "key_input": "",
            "result_preview": preview,
        }
        count += 1


def grok_session_path(cwd, session_id=None):
    """
    Find a Grok session directory by CWD and optional session ID.

    Returns the session directory Path, or None if not found.
    """
    if not GROK_DIR.exists():
        return None

    encoded_cwd = _encode_grok_cwd(cwd)
    cwd_dir = GROK_DIR / encoded_cwd
    if not cwd_dir.exists():
        return None

    if session_id:
        session_dir = cwd_dir / session_id
        if session_dir.is_dir():
            return session_dir
        return None

    # Find most recent session
    sessions = sorted(
        [d for d in cwd_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    return sessions[0] if sessions else None


# ---------------------------------------------------------------------------
# Kimi Code adapter
# ---------------------------------------------------------------------------


def kimi_list_sessions(cwd=None, limit=50, keyword=""):
    """
    List Kimi Code sessions.

    Scans ~/.kimi/sessions/<project_hash>/<session_uuid>/ directories.
    Returns list of SessionMeta.
    """
    if not KIMI_DIR.exists():
        return []

    entries = []
    for project_hash in sorted(KIMI_DIR.iterdir()):
        if not project_hash.is_dir():
            continue
        for session_dir in sorted(project_hash.iterdir()):
            if not session_dir.is_dir():
                continue

            meta_file = session_dir / "metadata.json"
            wire_file = session_dir / "wire.jsonl"

            if not meta_file.exists():
                continue

            try:
                with open(meta_file, encoding="utf-8") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            sid = meta.get("session_id", session_dir.name)
            title = (meta.get("title") or "")[:100]
            # wire_mtime is a Unix timestamp float
            wire_mtime = meta.get("wire_mtime")
            created = _normalize_timestamp(wire_mtime) if wire_mtime else ""

            # Quick message count from wire.jsonl if available
            msg_count = 0
            first_msg = title  # fallback first message from title
            if wire_file.exists():
                try:
                    with open(wire_file, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            try:
                                rec = json.loads(line.strip())
                                msg = rec.get("message", {})
                                if msg.get("type") == "TurnBegin":
                                    msg_count += 1
                                    if first_msg == title:  # extract first real user message
                                        payload = msg.get("payload", {})
                                        user_input = payload.get("user_input", [])
                                        for ui in (user_input if isinstance(user_input, list) else []):
                                            if isinstance(ui, dict) and ui.get("type") == "text":
                                                first_msg = ui["text"][:100].replace("\n", " ")
                                                break
                            except (json.JSONDecodeError, ValueError):
                                continue
                except OSError:
                    pass

            # Filter by keyword
            if keyword and keyword.lower() not in title.lower() and keyword.lower() not in first_msg.lower():
                continue

            # Filter by project path if cwd specified
            project_path = str(project_hash)
            if cwd and cwd not in str(session_dir):
                # Rough filter — Kimi doesn't store cwd in metadata
                pass

            entries.append(SessionMeta(
                session_id=sid,
                full_path=str(session_dir),
                created=created,
                modified=created,
                message_count=msg_count,
                git_branch="",
                summary=title,
                first_prompt=first_msg,
                project_path=project_path,
            ))

    entries.sort(key=lambda e: str(e.created), reverse=True)
    return entries[:limit]


def kimi_session_stats(session_dir):
    """
    Get session statistics for a Kimi Code session.

    Reads wire.jsonl and state.json. Returns a dict compatible with session_stats().
    """
    session_dir = Path(session_dir)
    wire_file = session_dir / "wire.jsonl"
    state_file = session_dir / "state.json"
    meta_file = session_dir / "metadata.json"

    stats = {
        "slug": "",
        "model": "kimi",
        "branch": "",
        "started": "",
        "ended": "",
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
        "files_edited": "N/A",
        "errors": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "compactions": 0,
        "summary": "",
    }

    # Model from state.json
    if state_file.exists():
        try:
            with open(state_file, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("model"):
                stats["model"] = state["model"]
        except (json.JSONDecodeError, OSError):
            pass

    # Timestamps and title from metadata.json
    if meta_file.exists():
        try:
            with open(meta_file, encoding="utf-8") as f:
                meta = json.load(f)
            wire_mtime = meta.get("wire_mtime")
            if wire_mtime:
                stats["started"] = _normalize_timestamp(wire_mtime)
            stats["summary"] = (meta.get("title") or "")[:100]
        except (json.JSONDecodeError, OSError):
            pass

    # Count from wire.jsonl
    if wire_file.exists():
        try:
            with open(wire_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line.strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = rec.get("message", {})
                    mt = msg.get("type", "")
                    if mt == "TurnBegin":
                        stats["user_messages"] += 1
                    elif mt in ("ContentPart", "ToolCallPart"):
                        stats["assistant_messages"] += 1
                    elif mt == "ToolCall":
                        stats["tool_calls"] += 1
        except OSError:
            pass

    return stats


def kimi_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """
    Extract human-readable messages from a Kimi Code session.

    Reads wire.jsonl. Kimi stores user input in TurnBegin.payload.user_input[].text
    and assistant text in ContentPart.payload.text.

    Yields dicts with keys: role, timestamp, text.
    """
    session_dir = Path(session_dir)
    wire_file = session_dir / "wire.jsonl"
    if not wire_file.exists():
        return

    count = 0
    with open(wire_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            raw_ts = rec.get("timestamp") or ""
            ts = _normalize_timestamp(raw_ts) if raw_ts else ""

            msg = rec.get("message", {})
            mt = msg.get("type", "")

            if mt == "TurnBegin" and role in ("user", "both"):
                payload = msg.get("payload", {})
                user_input = payload.get("user_input", [])
                parts = []
                for ui in (user_input if isinstance(user_input, list) else []):
                    if isinstance(ui, dict) and ui.get("type") == "text":
                        t = ui.get("text", "").strip()
                        if t:
                            parts.append(t)
                if parts:
                    yield {"role": "USER", "timestamp": ts, "text": "\n".join(parts)}
                    count += 1

            elif mt == "ContentPart" and role in ("assistant", "both"):
                payload = msg.get("payload", {})
                if payload.get("type") == "text":
                    text = payload.get("text", "").strip()
                    if text:
                        yield {"role": "ASSISTANT", "timestamp": ts, "text": text}
                        count += 1
                elif payload.get("type") == "tool_call":
                    name = payload.get("function", {}).get("name", "?")
                    yield {"role": "ASSISTANT", "timestamp": ts, "text": "[TOOL: {}]".format(name)}
                    count += 1

            if limit and count >= limit:
                return


def kimi_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """
    Extract tool calls from a Kimi Code session.

    Kimi stores ToolCall (name, id) and ToolResult (tool_call_id, return_value)
    in wire.jsonl. We join by tool_call_id.

    Yields tool call dicts: {timestamp, name, status, key_input, result_preview}.
    """
    session_dir = Path(session_dir)
    wire_file = session_dir / "wire.jsonl"
    if not wire_file.exists():
        return

    # Two-pass: collect ToolCall by id, then ToolResult by id
    calls = {}  # tool_call_id -> {name, ts, input_preview}
    results = {}  # tool_call_id -> result_preview

    with open(wire_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            raw_ts = rec.get("timestamp") or ""
            ts = _normalize_timestamp(raw_ts) if raw_ts else ""
            msg = rec.get("message", {})
            mt = msg.get("type", "")

            if mt == "ToolCall":
                payload = msg.get("payload", {})
                tid = payload.get("id", "")
                name = payload.get("function", {}).get("name", "")
                if tool_filter and name != tool_filter:
                    continue
                inp = payload.get("function", {}).get("arguments", "")
                if isinstance(inp, dict):
                    inp = json.dumps(inp, ensure_ascii=False)
                calls[tid] = {
                    "name": name,
                    "ts": ts,
                    "input_preview": str(inp)[:150] if inp else "",
                }

            elif mt == "ToolResult":
                payload = msg.get("payload", {})
                tid = payload.get("tool_call_id", "")
                rv = payload.get("return_value", "")
                if isinstance(rv, str):
                    results[tid] = rv[:150].replace("\n", " ")
                elif isinstance(rv, dict):
                    results[tid] = json.dumps(rv, ensure_ascii=False)[:150]
                else:
                    results[tid] = str(rv)[:150]

    count = 0
    for tid, info in sorted(calls.items()):
        name = info["name"]
        ts = info["ts"]
        inp = info["input_preview"]
        result = results.get(tid, "(no result)")
        status = "ok"  # Kimi doesn't expose error status in ToolResult format
        if errors_only:
            continue  # No error tracking in Kimi ToolResult; skip in errors_only mode

        if limit and count >= limit:
            return
        yield {
            "timestamp": ts,
            "name": name,
            "status": status,
            "key_input": inp,
            "result_preview": result,
        }
        count += 1


def kimi_session_path(cwd, session_id=None):
    """
    Find a Kimi Code session directory.

    Kimi doesn't store per-project CWD the same way Grok/Claude do,
    so this is a simpler lookup.
    """
    if not KIMI_DIR.exists():
        return None

    if session_id:
        for project_dir in KIMI_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            session_dir = project_dir / session_id
            if session_dir.is_dir():
                return session_dir
        return None

    return KIMI_DIR if KIMI_DIR.is_dir() else None


# ---------------------------------------------------------------------------
# Kimi Code adapter ( ~/.kimi-code/sessions/ )
# ---------------------------------------------------------------------------

def kimi_code_list_sessions(cwd=None, limit=50, keyword=""):
    """
    List Kimi Code sessions from ~/.kimi-code/sessions/.

    Directory structure: ~/.kimi-code/sessions/<project_dir>/<session_uuid>/
    Each session has state.json + agents/main/wire.jsonl
    """
    if not KIMI_CODE_DIR.exists():
        return []

    entries = []
    for project_dir in sorted(KIMI_CODE_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        for session_dir in sorted(project_dir.iterdir()):
            if not session_dir.is_dir():
                continue

            state_file = session_dir / "state.json"
            wire_file = session_dir / "agents" / "main" / "wire.jsonl"

            if not state_file.exists():
                continue

            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            sid = session_dir.name.replace("session_", "")
            title = (state.get("title") or "")[:100]
            created_at = state.get("createdAt", "")
            updated_at = state.get("updatedAt", "")

            # Count messages and extract first user prompt from wire.jsonl
            msg_count = 0
            first_msg = title
            if wire_file.exists():
                try:
                    with open(wire_file, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            try:
                                rec = json.loads(line.strip())
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if rec.get("type") == "turn.prompt":
                                msg_count += 1
                                if first_msg == title:
                                    inputs = rec.get("input", [])
                                    for inp in (inputs if isinstance(inputs, list) else []):
                                        if isinstance(inp, dict) and inp.get("type") == "text":
                                            first_msg = inp["text"][:100].replace("\n", " ")
                                            break
                except OSError:
                    pass

            # Filter by keyword
            if keyword and keyword.lower() not in title.lower() and keyword.lower() not in first_msg.lower():
                continue

            entries.append(SessionMeta(
                session_id=sid,
                full_path=str(session_dir),
                created=created_at,
                modified=updated_at or created_at,
                message_count=msg_count,
                git_branch="",
                summary=title,
                first_prompt=first_msg,
                project_path=str(project_dir),
            ))

    entries.sort(key=lambda e: str(e.created), reverse=True)
    return entries[:limit]


def kimi_code_session_stats(session_dir):
    """
    Get session statistics for a Kimi Code session.

    Reads state.json and agents/main/wire.jsonl.
    """
    session_dir = Path(session_dir)
    state_file = session_dir / "state.json"
    wire_file = session_dir / "agents" / "main" / "wire.jsonl"

    stats = {
        "slug": "",
        "model": "kimi-code",
        "branch": "",
        "started": "",
        "ended": "",
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_calls": 0,
        "files_edited": "N/A",
        "errors": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "compactions": 0,
        "summary": "",
    }

    if state_file.exists():
        try:
            with open(state_file, encoding="utf-8") as f:
                state = json.load(f)
            stats["started"] = state.get("createdAt", "")
            stats["ended"] = state.get("updatedAt", "")
            stats["summary"] = (state.get("title") or "")[:100]
        except (json.JSONDecodeError, OSError):
            pass

    if wire_file.exists():
        try:
            with open(wire_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line.strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    rtype = rec.get("type", "")
                    if rtype == "turn.prompt":
                        stats["user_messages"] += 1
                    elif rtype == "text":
                        stats["assistant_messages"] += 1
                    elif rtype == "tool.call":
                        stats["tool_calls"] += 1
                    elif rtype == "tool.result":
                        # Check for errors in tool results
                        result = rec.get("result", {})
                        if isinstance(result, dict) and result.get("isError"):
                            stats["errors"] += 1
                    elif rtype == "usage.record":
                        usage = rec.get("usage", {})
                        stats["input_tokens"] += usage.get("inputTokens", 0)
                        stats["output_tokens"] += usage.get("outputTokens", 0)
        except OSError:
            pass

    return stats


def kimi_code_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """
    Extract human-readable messages from a Kimi Code session.

    Reads agents/main/wire.jsonl.
    User messages: turn.prompt.input[].text
    Assistant messages: text type records with content
    """
    session_dir = Path(session_dir)
    wire_file = session_dir / "agents" / "main" / "wire.jsonl"
    if not wire_file.exists():
        return

    count = 0
    with open(wire_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            raw_ts = rec.get("time") or rec.get("timestamp") or ""
            ts = _normalize_timestamp(raw_ts) if raw_ts else ""
            rtype = rec.get("type", "")

            if rtype == "turn.prompt" and role in ("user", "both"):
                inputs = rec.get("input", [])
                parts = []
                for inp in (inputs if isinstance(inputs, list) else []):
                    if isinstance(inp, dict) and inp.get("type") == "text":
                        t = inp.get("text", "").strip()
                        if t:
                            parts.append(t)
                if parts:
                    yield {"role": "USER", "timestamp": ts, "text": "\n".join(parts)}
                    count += 1

            elif rtype == "text" and role in ("assistant", "both"):
                content = rec.get("content", "")
                if isinstance(content, str) and content.strip():
                    yield {"role": "ASSISTANT", "timestamp": ts, "text": content.strip()}
                    count += 1
                elif isinstance(content, list):
                    texts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            texts.append(part.get("text", ""))
                    if texts:
                        yield {"role": "ASSISTANT", "timestamp": ts, "text": "\n".join(texts)}
                        count += 1

            elif rtype == "content.part" and role in ("assistant", "both"):
                content = rec.get("content", "")
                if isinstance(content, str) and content.strip():
                    yield {"role": "ASSISTANT", "timestamp": ts, "text": content.strip()}
                    count += 1

            if limit and count >= limit:
                return


def kimi_code_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """
    Extract tool calls from a Kimi Code session.

    Reads agents/main/wire.jsonl.
    Tool calls: type=tool.call with name and input
    Tool results: type=tool.result with output
    """
    session_dir = Path(session_dir)
    wire_file = session_dir / "agents" / "main" / "wire.jsonl"
    if not wire_file.exists():
        return

    # Collect tool calls and results
    calls = {}  # id -> {name, ts, input_preview}
    results = {}  # id -> {result_preview, is_error}

    with open(wire_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            raw_ts = rec.get("time") or rec.get("timestamp") or ""
            ts = _normalize_timestamp(raw_ts) if raw_ts else ""
            rtype = rec.get("type", "")

            if rtype == "tool.call":
                tid = rec.get("id", "")
                name = rec.get("name", "")
                if tool_filter and name != tool_filter:
                    continue
                inp = rec.get("input", "")
                if isinstance(inp, dict):
                    inp = json.dumps(inp, ensure_ascii=False)
                calls[tid] = {
                    "name": name,
                    "ts": ts,
                    "input_preview": str(inp)[:150] if inp else "",
                }

            elif rtype == "tool.result":
                tid = rec.get("toolCallId", rec.get("id", ""))
                output = rec.get("output", rec.get("result", ""))
                is_error = rec.get("isError", False)
                if isinstance(output, str):
                    results[tid] = {"preview": output[:150].replace("\n", " "), "is_error": is_error}
                elif isinstance(output, dict):
                    results[tid] = {"preview": json.dumps(output, ensure_ascii=False)[:150], "is_error": is_error}
                else:
                    results[tid] = {"preview": str(output)[:150], "is_error": is_error}

    count = 0
    for tid, info in sorted(calls.items()):
        name = info["name"]
        ts = info["ts"]
        inp = info["input_preview"]
        result_info = results.get(tid, {"preview": "(no result)", "is_error": False})
        result = result_info["preview"]
        is_error = result_info["is_error"]
        status = "error" if is_error else "ok"

        if errors_only and not is_error:
            continue

        if limit and count >= limit:
            return
        yield {
            "timestamp": ts,
            "name": name,
            "status": status,
            "key_input": inp,
            "result_preview": result,
        }
        count += 1


def kimi_code_session_path(cwd, session_id=None):
    """
    Find a Kimi Code session directory.
    """
    if not KIMI_CODE_DIR.exists():
        return None

    if session_id:
        for project_dir in KIMI_CODE_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            # Try with and without session_ prefix
            for sid in [session_id, f"session_{session_id}"]:
                session_dir = project_dir / sid
                if session_dir.is_dir():
                    return session_dir
        return None

    return KIMI_CODE_DIR if KIMI_CODE_DIR.is_dir() else None


# ---------------------------------------------------------------------------
# Cross-tool unified interface
# ---------------------------------------------------------------------------

def cross_tool_list_sessions(limit=50, keyword="", agent_filter=None):
    """
    跨工具列出所有会话，并行处理多个适配器以提高性能。

    Args:
        limit: 每个适配器的最大会话数
        keyword: 按标题/first_prompt过滤
        agent_filter: None（全部）、"claude"、"grok"、"kimi_code"、"codex"、"workbuddy"、"trae_cn"或列表

    Returns:
        字典列表: {agent, session_id, created, summary, first_prompt, msg_count, full_path}
    """
    all_sessions = []
    # 默认包含所有可用的适配器
    default_agents = ["claude", "grok", "kimi_code"]
    if codex_list_sessions is not None:
        default_agents.append("codex")
    if workbuddy_list_sessions is not None:
        default_agents.append("workbuddy")
    if trae_list_sessions is not None:
        default_agents.append("trae_cn")
    agents = agent_filter if isinstance(agent_filter, (list, tuple)) else [agent_filter] if agent_filter else default_agents
    
    # 适配器映射（跳过不可用的适配器）
    adapter_map = {
        "claude": lambda: list_sessions(scope="all", limit=limit, grep_pat=keyword),
        "grok": lambda: grok_list_sessions(limit=limit, keyword=keyword),
        "kimi_code": lambda: kimi_code_list_sessions(limit=limit, keyword=keyword),
    }
    # 只添加可用的env-adapter适配器
    if codex_list_sessions is not None:
        adapter_map["codex"] = lambda: codex_list_sessions(limit=limit, keyword=keyword)
    if workbuddy_list_sessions is not None:
        adapter_map["workbuddy"] = lambda: workbuddy_list_sessions(limit=limit, keyword=keyword)
    if trae_list_sessions is not None:
        adapter_map["trae_cn"] = lambda: trae_list_sessions(limit=limit, keyword=keyword)
    
    # 代理显示名称映射
    agent_display = {
        "claude": "Claude Code",
        "grok": "Grok",
        "kimi_code": "Kimi Code",
        "codex": "Codex",
        "workbuddy": "WorkBuddy",
        "trae_cn": "Trae CN",
    }
    
    # 并行执行所有适配器
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for agent in agents:
            if agent in adapter_map:
                future = executor.submit(adapter_map[agent])
                futures[future] = agent
        
        for future in concurrent.futures.as_completed(futures):
            agent_name = futures[future]
            try:
                sessions = future.result()
                display_name = agent_display.get(agent_name, agent_name)
                for s in sessions:
                    all_sessions.append({
                        "agent": display_name,
                        "session_id": s.session_id,
                        "created": s.created,
                        "summary": s.summary,
                        "first_prompt": s.first_prompt,
                        "msg_count": s.message_count,
                        "full_path": s.full_path,
                    })
            except Exception:
                # 静默处理异常，继续处理其他适配器
                pass
    
    all_sessions.sort(key=lambda s: str(s.get("created", "")), reverse=True)
    return all_sessions[:limit]


def cross_tool_session_stats(session_path):
    """
    Get statistics for a session from any supported agent.
    Auto-detects agent type and dispatches.
    """
    agent = detect_agent_type(session_path)
    if agent == "grok":
        return grok_session_stats(session_path)
    elif agent == "kimi_code":
        return kimi_code_session_stats(session_path)
    else:
        return session_stats(session_path)


# ---------------------------------------------------------------------------
# Knowledge extraction
# ---------------------------------------------------------------------------

import re as _re

_CORRECTION_PATTERNS = _re.compile(
    r"\b(no[,.]?\s+(?:don'?t|not|stop|wrong|instead))|"
    r"\b(don'?t\s+\w+)|"
    r"\b(stop\s+doing)|"
    r"\b(that'?s\s+(?:wrong|incorrect|not right))",
    _re.IGNORECASE
)
_APPROVAL_PATTERNS = _re.compile(
    r"\b(perfect|exactly|great|yes[,.]?\s+(?:that'?s|keep|do it)|works|looks good|nice)",
    _re.IGNORECASE
)
_IMPERATIVE_PATTERNS = _re.compile(
    r"\b(always|never|must|do not|don'?t ever|every time|make sure)",
    _re.IGNORECASE
)
_URL_PATTERN = _re.compile(r"https?://[^\s\)\"'>]+")
_VALUE_PATTERNS = _re.compile(
    r"\b(\w+\s+(?:is|are)\s+(?:better|more important|more valuable|preferable)\s+(?:than|over|to)\s+)|"
    r"\b(prefer\s+\w+\s+(?:over|to|instead of)\s+)|"
    r"\b(prioritize\s+\w+\s+over\s+)|"
    r"\b(\w+\s+(?:matters?|trumps?|outweighs?|beats?)\s+(?:more than\s+)?)|"
    r"\b(choose\s+\w+\s+over\s+)|"
    r"\b((?:the )?most (?:important|valuable|useful|durable)\s+(?:\w+\s+)?(?:is|are)\s+)|"
    r"\b(rather\s+\w+\s+than\s+)|"
    r"\b(\w+\s+>\s+\w+)",
    _re.IGNORECASE
)


def extract_knowledge(session_path):
    """
    Two-pass knowledge extraction from a session.

    Pass 1: Scan tool calls for decisions (AskUserQuestion) and errors.
    Pass 2: Scan messages for corrections, patterns, references, values.

    Yields dicts with keys: category, content, timestamp,
                           suggested_destination, suggested_type.
    """
    items = []

    # Pass 1: Tool calls
    for tool in extract_tools(session_path):
        if tool["name"] == "AskUserQuestion":
            items.append({
                "category": "decision",
                "content": "Question: %s | Answer: %s" % (
                    tool.get("key_input", "")[:200],
                    tool.get("result_preview", "")[:200]
                ),
                "timestamp": tool.get("timestamp", ""),
                "suggested_destination": "memory",
                "suggested_type": "project",
            })
        elif tool.get("status") == "error":
            items.append({
                "category": "lesson",
                "content": "Tool %s failed: %s" % (
                    tool["name"],
                    tool.get("result_preview", "")[:200]
                ),
                "timestamp": tool.get("timestamp", ""),
                "suggested_destination": "skip",
                "suggested_type": None,
            })

    # Pass 2: Messages
    prev_assistant_text = ""
    for msg in extract_messages(session_path, role="both"):
        text = msg.get("text", "")
        if not text or len(text) < 5:
            if msg.get("role") == "ASSISTANT":
                prev_assistant_text = text or ""
            continue

        if msg.get("role") == "ASSISTANT":
            prev_assistant_text = text[:500]
            continue

        # User messages below
        if _VALUE_PATTERNS.search(text):
            items.append({
                "category": "value",
                "content": text[:300],
                "timestamp": msg.get("timestamp", ""),
                "suggested_destination": "memory",
                "suggested_type": "value",
            })

        if _CORRECTION_PATTERNS.search(text):
            dest = "claude_md" if _IMPERATIVE_PATTERNS.search(text) else "memory"
            items.append({
                "category": "correction",
                "content": text[:300],
                "timestamp": msg.get("timestamp", ""),
                "suggested_destination": dest,
                "suggested_type": "feedback",
            })

        if _APPROVAL_PATTERNS.search(text) and prev_assistant_text:
            items.append({
                "category": "pattern",
                "content": "Approach approved: %s" % prev_assistant_text[:200],
                "timestamp": msg.get("timestamp", ""),
                "suggested_destination": "memory",
                "suggested_type": "feedback",
            })

        for url in _URL_PATTERN.findall(text):
            items.append({
                "category": "reference",
                "content": "URL mentioned: %s" % url,
                "timestamp": msg.get("timestamp", ""),
                "suggested_destination": "memory",
                "suggested_type": "reference",
            })

    # Deduplicate by content prefix
    seen = set()
    for item in items:
        key = item["content"][:80]
        if key not in seen:
            seen.add(key)
            yield item


# ---------------------------------------------------------------------------
# Analysis memoization — 分析结果存证
# ---------------------------------------------------------------------------
# 设计理念：摘要不是预先生成的索引，而是分析结果的存证。
# 分析过一次就存下来，下次直接读，不再重复全量解析+LLM分析。
# ---------------------------------------------------------------------------

import time as _time


def _summary_path_for(session_path):
    """推导摘要文件路径：原始文件同目录下 .summary.jsonl"""
    return session_path + ".summary.jsonl"


def save_analysis_result(session_path, analysis, query_intent,
                         agent_type="claude", source_mtime=None,
                         memory_tier="periodic", excluded=None):
    """
    分析完成后调用，把结果存为摘要（追加模式）。

    同一会话可能被多次分析（不同角度），每次追加一条记录。

    Args:
        session_path: 原始会话文件路径
        analysis: LLM 的分析输出文本
        query_intent: 本次查询意图（如 "投资"、"skill优化"）
        agent_type: 来源环境类型
        source_mtime: 原始文件 mtime（用于新鲜度判断）
        memory_tier: 时效等级（借鉴 taxue-save）
            permanent: 认知规律、思维模型 → 永不遗忘
            periodic:  偏好、阶段性结论 → 7天后不再注入（旧偏好不如没有偏好）
            once:      临时上下文、单次任务 → 24小时后失效
        excluded: 已否决方向列表（借鉴 dbs-save）
            如 ["Rust 不适合因为零依赖是核心优势", "方案C 成本过高"]
    """
    if source_mtime is None:
        source_mtime = os.path.getmtime(session_path)

    summary_path = _summary_path_for(session_path)
    record = {
        "schema": "session-digger-summary/v1",
        "session_id": os.path.basename(session_path).replace(".jsonl", ""),
        "source_path": session_path,
        "source_agent": agent_type,
        "source_mtime": source_mtime,
        "analyzed_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "query_intent": query_intent,
        "analysis": analysis,
        "memory_tier": memory_tier,
        "excluded": excluded or [],
    }

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return summary_path


# 时效分层阈值（秒）
_TIER_ONCE_TTL = 86400       # once: 24小时
_TIER_PERIODIC_TTL = 604800  # periodic: 7天


def load_analysis_result(session_path, query_intent=None):
    """
    读取已存储的分析结果。

    Args:
        session_path: 原始会话文件路径
        query_intent: 可选，按意图过滤（子串匹配）

    Returns:
        list[dict]: 匹配的分析记录列表。空列表表示无摘要或已过期。

    过滤规则（三重过滤）：
        1. 新鲜度：原始文件 mtime > 摘要记录的 source_mtime → 跳过
        2. 时效分层：
           permanent → 永不因时间过期
           periodic  → 超过 7 天不再注入（旧偏好不如没有偏好）
           once      → 超过 24 小时不再注入
        3. 意图过滤：query_intent 子串匹配
    """
    summary_path = _summary_path_for(session_path)
    if not os.path.exists(summary_path):
        return []

    raw_mtime = os.path.getmtime(session_path)
    now = _time.time()
    results = []

    with open(summary_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            # 过滤 1: 新鲜度检查 — 原始文件更新过则跳过
            if rec.get("source_mtime", 0) < raw_mtime:
                continue

            # 过滤 2: 时效分层 — 基于分析时间，非会话时间
            tier = rec.get("memory_tier", "periodic")
            analyzed_at = rec.get("analyzed_at", "")
            if analyzed_at:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(analyzed_at)
                    rec_age = now - dt.timestamp()
                except (ValueError, OSError):
                    rec_age = now - rec.get("source_mtime", now)
            else:
                rec_age = now - rec.get("source_mtime", now)
            if tier == "once" and rec_age > _TIER_ONCE_TTL:
                continue
            elif tier == "periodic" and rec_age > _TIER_PERIODIC_TTL:
                continue
            # permanent 不过滤

            # 过滤 3: 意图过滤
            if query_intent:
                stored_intent = rec.get("query_intent", "")
                if query_intent.lower() not in stored_intent.lower():
                    continue

            results.append(rec)

    return results


def has_fresh_summary(session_path):
    """快速判断是否存在新鲜摘要（不读取内容，仅 mtime 对比）"""
    summary_path = _summary_path_for(session_path)
    if not os.path.exists(summary_path):
        return False
    return os.path.getmtime(summary_path) >= os.path.getmtime(session_path)


def build_summary_index(scopes=None):
    """
    扫描所有环境，构建已分析会话的归档索引。

    Args:
        scopes: 可选，限定扫描的环境列表。默认全部。

    Returns:
        dict: 归档索引 JSON 结构
    """
    import glob

    index = {
        "schema": "session-digger-archive-index/v1",
        "generated_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "analyzed_sessions": [],
        "stats": {"total": 0, "by_agent": {}, "by_intent": {}},
    }

    search_paths = [
        (os.path.expanduser("~/.claude/projects"), "claude"),
        (os.path.expanduser("~/.grok/sessions"), "grok"),
        (os.path.expanduser("~/.kimi-code/sessions"), "kimi_code"),
        (os.path.expanduser("~/.codex/sessions"), "codex"),
    ]

    if scopes:
        search_paths = [(p, a) for p, a in search_paths if a in scopes]

    for base_path, agent_type in search_paths:
        if not os.path.exists(base_path):
            continue

        for summary_file in glob.glob(
            os.path.join(base_path, "**", "*.summary.jsonl"), recursive=True
        ):
            with open(summary_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    source_path = rec.get("source_path", "")
                    fresh = False
                    if source_path and os.path.exists(source_path):
                        fresh = os.path.getmtime(summary_file) >=                                 os.path.getmtime(source_path)

                    entry = {
                        "session_id": rec.get("session_id", ""),
                        "source_agent": rec.get("source_agent", agent_type),
                        "source_path": source_path,
                        "summary_path": summary_file,
                        "analyzed_at": rec.get("analyzed_at", ""),
                        "query_intent": rec.get("query_intent", ""),
                        "is_fresh": fresh,
                    }
                    index["analyzed_sessions"].append(entry)
                    index["stats"]["total"] += 1

                    agent = entry["source_agent"]
                    index["stats"]["by_agent"][agent] =                         index["stats"]["by_agent"].get(agent, 0) + 1

                    intent = entry["query_intent"]
                    if intent:
                        index["stats"]["by_intent"][intent] =                             index["stats"]["by_intent"].get(intent, 0) + 1

    return index


def save_summary_index(index, index_path=None):
    """保存归档索引到文件"""
    if index_path is None:
        index_path = os.path.expanduser(
            "~/.claude/.session-digger-archive-index.json"
        )
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return index_path
