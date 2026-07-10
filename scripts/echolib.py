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
import re
import sqlite3
import sys
import concurrent.futures
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

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
# Shared helpers — used by all adapters to eliminate boilerplate
# ---------------------------------------------------------------------------

def _iter_jsonl(path):
    """Yield parsed JSON records from a JSONL file, skipping blank/error lines.

    Centralises the open-strip-parse-error_skip pattern repeated across 30+
    adapter functions.  Always uses errors="replace" and swallows OSError.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass


def _strip_system_reminder(text):
    """Remove <system-reminder ...>...</system-reminder> wrapper, return real content.

    Handles tags with attributes (e.g. <system-reminder data-role="user-context">).
    Returns None if the entire text is a system-reminder block (nothing left).
    """
    if not text:
        return text
    stripped = text.strip()
    if stripped.startswith("<system-reminder"):
        # Match both <system-reminder> and <system-reminder attr="...">
        if "</system-reminder>" in stripped:
            after = stripped.split("</system-reminder>", 1)[-1].strip()
            return after if after else None
        return None
    return text


def _extract_content_text(content, keys=("text", "message"), max_len=0):
    """Extract text from a content field that may be str, list, or dict.

    - str: returned directly
    - list: each item checked for dict with one of *keys*, or str items joined
    - dict: checked for one of *keys*
    Returns the extracted text (optionally truncated), or "" if nothing found.
    """
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                for k in keys:
                    v = block.get(k, "")
                    if isinstance(v, str) and v.strip():
                        texts.append(v)
            elif isinstance(block, str) and block.strip():
                texts.append(block)
        text = "\n".join(texts) if texts else ""
    elif isinstance(content, dict):
        text = ""
        for k in keys:
            v = content.get(k, "")
            if isinstance(v, str) and v.strip():
                text = v
                break
    else:
        text = ""
    if max_len and text:
        return text[:max_len]
    return text


def _match_call_results(calls, results, errors_only=False, limit=0):
    """Yield matched tool calls from calls/results dicts keyed by call_id.

    Shared by codex_extract_tools and workbuddy_extract_tools which both
    use a two-pass pattern: collect calls and outputs separately, then
    join them by call_id.
    """
    count = 0
    for call_id, info in sorted(calls.items(), key=lambda x: x[1].get("ts", "")):
        result_info = results.get(call_id, {"preview": "(no result)", "is_error": False})
        is_error = result_info.get("is_error", False)
        if errors_only and not is_error:
            continue
        if limit and count >= limit:
            return
        yield {
            "timestamp": info.get("ts", ""),
            "name": info.get("name", ""),
            "status": "error" if is_error else "ok",
            "key_input": info.get("input_preview", ""),
            "result_preview": result_info.get("preview", ""),
        }
        count += 1


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
        # Fallback: some Claude forks (e.g. Qwen) use message.parts instead of content
        if not c and self._d.get("message", {}).get("parts"):
            c = self._d.get("message", {}).get("parts")
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            parts = []
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        t = b.get("text", "").strip()
                        if t:
                            parts.append(t)
                    elif b.get("text"):  # parts-style: {"text": "..."}
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
CODEX_DIR = Path.home() / ".codex"
WORKBUDDY_DIR = Path.home() / ".workbuddy"
TRAE_DIR = Path.home() / ".trae-cn"
ZCODE_DIR = Path.home() / ".zcode" / "cli" / "agents"
DIM_DIR = Path.home() / ".dim" / "memory"
DIMCODE_DB_PATH = Path.home() / ".dimcode" / "v2" / "dimcode.sqlite"
REASONIX_DIR = Path.home() / ".reasonix" / "sessions"


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

    Strategy: chat_history.jsonl is the primary source — assistant messages
    contain tool_calls arrays with id/name/arguments, and tool_result messages
    contain the matching output by tool_call_id. We join these by ID.

    events.jsonl provides timestamps and outcome status as a supplement.

    Yields tool call dicts: {timestamp, name, status, key_input, result_preview}.
    """
    session_dir = Path(session_dir)
    events_file = session_dir / "events.jsonl"
    chat_file = session_dir / "chat_history.jsonl"

    # --- Phase 1: Collect timestamps and outcomes from events.jsonl ---
    # Grok events don't have tool_call_id, so we match by sequential order:
    # each tool_started is followed by a tool_completed with the same tool_name.
    event_tools = []  # list of {ts, name, outcome}
    if events_file.exists():
        started_queue = []  # pending tool_started entries
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
                    ts = event.get("ts", "")
                    started_queue.append({"ts": ts, "name": name, "outcome": "success"})
                elif etype == "tool_completed":
                    name = event.get("tool_name", "")
                    outcome = event.get("outcome", "success")
                    # Match to the oldest pending started with same name
                    for i, s in enumerate(started_queue):
                        if s["name"] == name:
                            s["outcome"] = outcome
                            event_tools.append(started_queue.pop(i))
                            break
                    else:
                        # No matching started event — add anyway
                        event_tools.append({"ts": "", "name": name, "outcome": outcome})

    # --- Phase 2: Collect tool calls and results from chat_history.jsonl ---
    # assistant messages have tool_calls arrays; tool_result messages have
    # tool_call_id + content.
    tool_calls_list = []  # [{id, name, args}]
    results_by_id = {}   # tool_call_id -> preview
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
                rtype = record.get("type", "")
                if rtype == "assistant":
                    calls = record.get("tool_calls", [])
                    if isinstance(calls, list):
                        for call in calls:
                            if not isinstance(call, dict):
                                continue
                            tid = call.get("id", "")
                            name = call.get("name", "")
                            args = call.get("arguments", "")
                            if isinstance(args, str):
                                key_input = args[:150]
                            elif isinstance(args, dict):
                                key_input = json.dumps(args, ensure_ascii=False)[:150]
                            else:
                                key_input = ""
                            tool_calls_list.append({"id": tid, "name": name, "key_input": key_input})
                elif rtype == "tool_result":
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

    # --- Phase 3: Yield tool calls, joining with results and event timestamps ---
    # Match event_tools by sequential order (same count expected)
    count = 0
    for i, tc in enumerate(tool_calls_list):
        name = tc["name"]
        if tool_filter and name != tool_filter:
            continue

        # Get timestamp and outcome from event_tools (sequential match)
        ts = ""
        outcome = "success"
        if i < len(event_tools):
            ts = event_tools[i].get("ts", "")
            outcome = event_tools[i].get("outcome", "success")

        status = "error" if outcome in ("failure", "error") else "ok"

        # Get result preview by tool_call_id
        preview = results_by_id.get(tc["id"], "(no result)")

        # Also check result content for error indicators
        if status == "ok" and preview != "(no result)":
            if "Exit Code:" in preview and "Exit Code: 0" not in preview:
                status = "error"

        if errors_only and status != "error":
            continue

        if limit and count >= limit:
            return
        yield {
            "timestamp": ts,
            "name": name,
            "status": status,
            "key_input": tc["key_input"],
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





def kimi_code_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """
    Extract tool calls from a Kimi Code session.

    Kimi stores tool calls as context.append_loop_event with event.type="tool.call".
    Tool results are in event.type="tool.result" with matching toolCallId.

    Yields tool call dicts: {timestamp, name, status, key_input, result_preview}.
    """
    resolved = _kimi_code_resolve_path(session_path)
    if not Path(resolved).exists():
        return

    # First pass: collect tool results by toolCallId
    results_by_id = {}
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
                if rec.get("type") != "context.append_loop_event":
                    continue
                event = rec.get("event", {})
                if not isinstance(event, dict) or event.get("type") != "tool.result":
                    continue
                tid = event.get("toolCallId", event.get("parentUuid", ""))
                result = event.get("result", {})
                is_error = False
                preview = ""
                if isinstance(result, dict):
                    is_error = bool(result.get("isError"))
                    output = result.get("output", "")
                    if isinstance(output, str):
                        preview = output[:150].replace("\n", " ").replace("\t", " ")
                        if "Exit Code:" in output and "Exit Code: 0" not in output:
                            is_error = True
                elif isinstance(result, str):
                    preview = result[:150].replace("\n", " ").replace("\t", " ")
                if tid:
                    results_by_id[tid] = {"preview": preview, "is_error": is_error}
    except OSError:
        pass

    # Second pass: yield tool calls
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
                if rec.get("type") != "context.append_loop_event":
                    continue
                event = rec.get("event", {})
                if not isinstance(event, dict) or event.get("type") != "tool.call":
                    continue

                name = event.get("name", "")
                if tool_filter and name != tool_filter:
                    continue

                tid = event.get("toolCallId", event.get("uuid", ""))
                ts = rec.get("time", rec.get("timestamp", ""))
                args = event.get("args", event.get("arguments", ""))
                if isinstance(args, dict):
                    key_input = json.dumps(args, ensure_ascii=False)[:150]
                elif isinstance(args, str):
                    key_input = args[:150]
                else:
                    key_input = ""

                # Get result
                result_info = results_by_id.get(tid, {"preview": "(no result)", "is_error": False})
                status = "error" if result_info["is_error"] else "ok"

                if errors_only and status != "error":
                    continue

                if limit and count >= limit:
                    return
                yield {
                    "timestamp": _normalize_timestamp(ts) if ts else "",
                    "name": name,
                    "status": status,
                    "key_input": key_input,
                    "result_preview": result_info["preview"],
                }
                count += 1
    except OSError:
        pass


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
# Cross-tool unified interface (backed by ADAPTER_REGISTRY)
# ---------------------------------------------------------------------------

def cross_tool_list_sessions(limit=50, keyword="", agent_filter=None):
    """
    跨工具列出所有会话，并行处理多个适配器以提高性能。
    """
    agents = agent_filter if isinstance(agent_filter, (list, tuple)) else \
             list(ADAPTER_REGISTRY.keys()) if agent_filter is None else [agent_filter]

    all_sessions = []
    # 每个适配器请求更多结果，确保全局排序后各环境都有代表
    per_adapter_limit = max(limit * 2, 20)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for name in agents:
            if name not in ADAPTER_REGISTRY:
                continue
            adapter = ADAPTER_REGISTRY[name]
            fn = adapter["list_sessions"]
            futures[executor.submit(fn, limit=per_adapter_limit, keyword=keyword)] = name

        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                sessions = future.result()
                display = ADAPTER_REGISTRY[name]["display_name"]
                for s in sessions:
                    # Handle both dict and object return types
                    if isinstance(s, dict):
                        all_sessions.append({
                            "agent": s.get("agent", display),
                            "session_id": s.get("session_id", s.get("id", "")),
                            "created": s.get("created", ""),
                            "summary": s.get("summary", s.get("title", "")),
                            "first_prompt": s.get("first_prompt", ""),
                            "msg_count": s.get("msg_count", s.get("message_count", 0)),
                            "full_path": s.get("full_path", s.get("path", "")),
                        })
                    else:
                        all_sessions.append({
                            "agent": display,
                            "session_id": s.session_id,
                            "created": s.created,
                            "summary": s.summary,
                            "first_prompt": s.first_prompt,
                            "msg_count": s.message_count,
                            "full_path": s.full_path,
                        })
            except Exception as exc:
                # Adapter error — skip silently; this adapter's sessions are omitted
                pass

    all_sessions.sort(key=lambda s: str(s.get("created", "") or ""), reverse=True)
    # 将空时间戳的会话挪到后面，让有时间戳的会话优先展示
    with_time = sorted([s for s in all_sessions if s.get("created")],
                       key=lambda s: str(s["created"]), reverse=True)
    without_time = [s for s in all_sessions if not s.get("created")]
    all_sessions = with_time + without_time
    return all_sessions[:limit]


def cross_tool_session_stats(session_path):
    """
    Get statistics for a session from any supported agent.
    Auto-detects agent type and dispatches via registry.
    """
    agent = detect_agent_type(session_path)
    if agent in ADAPTER_REGISTRY:
        return ADAPTER_REGISTRY[agent]["session_stats"](session_path)
    return session_stats(session_path)


def dispatch_resolve_agent(path):
    """Detect agent type from path and map to a registered adapter name.

    Resolution order:
    1. Path-based detection (detect_agent_type) → registered adapter
    2. Content-based detection (format-detector signatures) → registered adapter
    3. Fall back to "universal" (SchemaProbe auto-discovery)

    The universal adapter uses SchemaProbe to sample records and infer field
    mappings, so it can handle any JSONL format without dedicated adapters.
    """
    atype = detect_agent_type(path)
    if atype in ADAPTER_REGISTRY:
        return atype
    # Try content-based detection for unknown paths
    detected = _detect_format_from_content(path)
    if detected and detected in ADAPTER_REGISTRY:
        return detected
    return "universal"


def _detect_format_from_content(path):
    """Quick content-based format detection by sampling first 20 lines.

    Returns adapter name or None. This is a lightweight version of
    format-detector.py that can be called without subprocess overhead.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= 20:
                    break
                lines.append(line)
    except OSError:
        return None

    # Score each known format
    best_format = None
    best_score = 0

    # Claude Code: sessionId/uuid, cwd/gitBranch, toolUseResult, message.model
    # Use unique indicator set — not per-record scoring — to avoid false positives
    # from environments that share some fields (e.g. deepcode has sessionId but no type/cwd)
    claude_indicators = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            score = 0
            break
        if isinstance(obj, dict):
            if "sessionId" in obj or "uuid" in obj:
                claude_indicators.add("session_id")
            if obj.get("type") in ("user", "assistant", "system", "tool_use", "tool_result"):
                claude_indicators.add("type_field")
            if "cwd" in obj or "gitBranch" in obj:
                claude_indicators.add("cwd")
            if "toolUseResult" in obj:
                claude_indicators.add("toolUseResult")
            msg = obj.get("message", {})
            if isinstance(msg, dict) and msg.get("model", "").startswith("claude"):
                claude_indicators.add("claude_model")
    # Score: type_field is required, plus at least one strong indicator
    score = 0
    if "type_field" in claude_indicators:
        score += 2
    if "cwd" in claude_indicators:
        score += 4
    if "toolUseResult" in claude_indicators:
        score += 4
    if "claude_model" in claude_indicators:
        score += 3
    if "session_id" in claude_indicators:
        score += 1
    if score > best_score:
        best_score = score
        best_format = "claude"

    # Grok: type=reasoning, assistant+tool_calls, model_id, synthetic_reason
    # Use unique indicator set — tool_result type alone is not Grok-specific
    # (Claude Code also uses tool_result)
    grok_indicators = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            grok_indicators = set()
            break
        if isinstance(obj, dict):
            rtype = obj.get("type", "")
            if rtype == "reasoning":
                grok_indicators.add("reasoning")
            if rtype == "assistant" and "tool_calls" in obj:
                grok_indicators.add("assistant_tool_calls")
            if "model_id" in obj:
                grok_indicators.add("model_id")
            if "synthetic_reason" in obj:
                grok_indicators.add("synthetic_reason")
    score = 0
    if "reasoning" in grok_indicators:
        score += 4
    if "assistant_tool_calls" in grok_indicators:
        score += 4
    if "model_id" in grok_indicators:
        score += 3
    if "synthetic_reason" in grok_indicators:
        score += 2
    if score > best_score:
        best_score = score
        best_format = "grok"

    # Kimi Code: protocol_version, context.append_loop_event, turn.prompt
    # But NOT kimi non-code (which has protocol_version but uses TurnBegin/StepBegin/ContentPart)
    # Note: 'metadata' type is common to both formats (protocol-level), so it's not a kimi_code indicator
    score = 0
    has_kimi_code_types = False
    has_kimi_noncode_types = False
    kimi_code_specific = {"config.update", "turn.prompt", "context.append_loop_event",
                          "context.append_message", "tools.set_active_tools", "usage.record"}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            score = 0
            break
        if isinstance(obj, dict):
            rtype = obj.get("type", "")
            if rtype in kimi_code_specific:
                score += 2
                has_kimi_code_types = True
            if "protocol_version" in obj:
                score += 5
            # Detect kimi non-code message types (TurnBegin/StepBegin/ContentPart)
            msg = obj.get("message", {})
            if isinstance(msg, dict):
                msg_type = msg.get("type", "")
                if msg_type in ("TurnBegin", "StepBegin", "ContentPart", "ToolCallBegin", "ToolCallEnd"):
                    has_kimi_noncode_types = True
    # If kimi non-code types are present but kimi code types are not,
    # this is kimi non-code format — don't classify as kimi_code
    if has_kimi_noncode_types and not has_kimi_code_types:
        score = 0
    if score > best_score:
        best_score = score
        best_format = "kimi_code"

    # Codex: payload.type = function_call/message, timestamp
    score = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            score = 0
            break
        if isinstance(obj, dict):
            payload = obj.get("payload", {})
            if isinstance(payload, dict):
                ptype = payload.get("type", "")
                if ptype in ("message", "function_call", "reasoning"):
                    score += 2
            if obj.get("type") == "response_item" and "payload" in obj:
                score += 3
    if score > best_score:
        best_score = score
        best_format = "codex"

    return best_format if best_score >= 4 else None


def dispatch_session_stats(path):
    """Get session stats via the correct adapter for this session's agent."""
    agent = dispatch_resolve_agent(path)
    fn = ADAPTER_REGISTRY.get(agent, {}).get("session_stats")
    if fn:
        return fn(path)
    return session_stats(path)


def dispatch_extract_messages(path, role="both", no_tools=False, limit=0, thinking_limit=0):
    """Extract messages via the correct adapter for this session's agent.

    Handles per-adapter signature differences (e.g. grok has no no_tools arg).
    """
    agent = dispatch_resolve_agent(path)
    fn = ADAPTER_REGISTRY.get(agent, {}).get("extract_messages")
    if fn:
        return fn(path, role=role, limit=limit, thinking_limit=thinking_limit)
    return extract_messages(path, role=role, no_tools=no_tools, limit=limit, thinking_limit=thinking_limit)


def dispatch_extract_tools(path, errors_only=False, limit=0, tool_filter=""):
    """Extract tool calls via the correct adapter for this session's agent.

    Handles per-adapter path differences (e.g. grok expects session_dir, not
    the .jsonl file path).
    """
    agent = dispatch_resolve_agent(path)
    fn = ADAPTER_REGISTRY.get(agent, {}).get("extract_tools")
    if fn:
        if agent == "grok":
            # grok_extract_tools expects session_dir, not the .jsonl file path
            session_dir = str(Path(path).parent)
            return fn(session_dir, tool_filter=tool_filter, errors_only=errors_only, limit=limit)
        return fn(path, tool_filter=tool_filter, errors_only=errors_only, limit=limit)
    return extract_tools(path, tool_filter=tool_filter, errors_only=errors_only, limit=limit)


# ---------------------------------------------------------------------------
# Knowledge extraction
# ---------------------------------------------------------------------------

_CORRECTION_PATTERNS = re.compile(
    r"\b(no[,.]?\s+(?:don'?t|not|stop|wrong|instead))|"
    r"\b(don'?t\s+\w+)|"
    r"\b(stop\s+doing)|"
    r"\b(that'?s\s+(?:wrong|incorrect|not right))",
    re.IGNORECASE
)
_APPROVAL_PATTERNS = re.compile(
    r"\b(perfect|exactly|great|yes[,.]?\s+(?:that'?s|keep|do it)|works|looks good|nice)",
    re.IGNORECASE
)
_IMPERATIVE_PATTERNS = re.compile(
    r"\b(always|never|must|do not|don'?t ever|every time|make sure)",
    re.IGNORECASE
)
_URL_PATTERN = re.compile(r"https?://[^\s\)\"'>]+")
_VALUE_PATTERNS = re.compile(
    r"\b(\w+\s+(?:is|are)\s+(?:better|more important|more valuable|preferable)\s+(?:than|over|to)\s+)|"
    r"\b(prefer\s+\w+\s+(?:over|to|instead of)\s+)|"
    r"\b(prioritize\s+\w+\s+over\s+)|"
    r"\b(\w+\s+(?:matters?|trumps?|outweighs?|beats?)\s+(?:more than\s+)?)|"
    r"\b(choose\s+\w+\s+over\s+)|"
    r"\b((?:the )?most (?:important|valuable|useful|durable)\s+(?:\w+\s+)?(?:is|are)\s+)|"
    r"\b(rather\s+\w+\s+than\s+)|"
    r"\b(\w+\s+>\s+\w+)",
    re.IGNORECASE
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


# ═══════════════════════════════════════════════════════════════════════════
# Codex (OpenAI) Adapter
# ═══════════════════════════════════════════════════════════════════════════

def codex_list_sessions(cwd=None, limit=50, keyword=""):
    """List Codex sessions from ~/.codex/sessions/ using session_index.jsonl."""
    index_path = CODEX_DIR / "session_index.jsonl"
    if not index_path.exists():
        return codex_list_sessions_fallback(cwd, limit, keyword)

    sessions = []
    with open(index_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            sid = entry.get("id", "")
            title = entry.get("thread_name", "")
            updated = entry.get("updated_at", "")

            if keyword and keyword.lower() not in title.lower():
                continue

            rollout_path = _find_codex_rollout(sid)
            msg_count = 0
            first_prompt = ""
            if rollout_path:
                msg_count, first_prompt = _codex_quick_scan(rollout_path)

            sessions.append(SessionMeta(
                session_id=sid,
                full_path=str(rollout_path) if rollout_path else "",
                created=updated,
                modified=updated,
                message_count=msg_count,
                git_branch="",
                summary=title[:100] if title else "",
                first_prompt=first_prompt[:200] if first_prompt else "",
                project_path="",
            ))

    sessions.sort(key=lambda s: str(s.created or ""), reverse=True)
    return sessions[:limit]


def codex_list_sessions_fallback(cwd=None, limit=50, keyword=""):
    """Fallback: scan rollout files directly when no index exists."""
    sessions_dir = CODEX_DIR / "sessions"
    if not sessions_dir.exists():
        return []

    sessions = []
    for rollout in sorted(sessions_dir.rglob("rollout-*.jsonl"), reverse=True):
        name = rollout.stem
        parts = name.split("-")
        sid = parts[-1] if len(parts) >= 2 else name
        msg_count, first_prompt = _codex_quick_scan(rollout)
        mtime = _normalize_timestamp(rollout.stat().st_mtime)
        if keyword and keyword.lower() not in first_prompt.lower():
            continue
        sessions.append(SessionMeta(
            session_id=sid, full_path=str(rollout),
            created=mtime, modified=mtime,
            message_count=msg_count, git_branch="", summary="",
            first_prompt=first_prompt[:200] if first_prompt else "",
            project_path="",
        ))
    return sessions[:limit]


def _find_codex_rollout(session_id):
    """Find a rollout file by session UUID."""
    sessions_dir = CODEX_DIR / "sessions"
    if not sessions_dir.exists():
        return None
    for p in sessions_dir.rglob(f"*{session_id}*.jsonl"):
        return p
    return None


def _codex_quick_scan(rollout_path):
    """Quick scan: count user messages and extract first prompt."""
    user_count = 0
    first_prompt = ""
    for rec in _iter_jsonl(rollout_path):
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if rtype == "event_msg" and payload.get("type") == "user_message":
            user_count += 1
            if not first_prompt:
                first_prompt = payload.get("message", "")[:200]
    return user_count, first_prompt




def codex_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from a Codex rollout session."""
    path = Path(session_dir)
    if not path.exists():
        return
    calls = {}
    outputs = {}
    for rec in _iter_jsonl(path):
        if rec.get("type") != "response_item":
            continue
        payload = rec.get("payload", {})
        ptype = payload.get("type", "")
        ts = rec.get("timestamp", "")
        if ptype in ("function_call", "custom_tool_call"):
            name = payload.get("name", "")
            if tool_filter and name != tool_filter:
                continue
            call_id = payload.get("call_id", "")
            args = payload.get("arguments", payload.get("input", ""))
            calls[call_id] = {"name": name, "ts": ts, "input_preview": str(args)[:150] if args else ""}
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id", "")
            output = payload.get("output", "")
            is_error = False
            if isinstance(output, str):
                if "Exit Code:" in output and "Exit Code: 0" not in output:
                    is_error = True
                if "Failed" in output:
                    is_error = True
            outputs[call_id] = {"preview": str(output)[:150].replace("\\n", " ") if output else "", "is_error": is_error}
    yield from _match_call_results(calls, outputs, errors_only, limit)


def codex_session_path(cwd, session_id=None):
    """Find a Codex session file."""
    sessions_dir = CODEX_DIR / "sessions"
    if not sessions_dir.exists():
        return None
    if session_id:
        for p in sessions_dir.rglob(f"*{session_id}*.jsonl"):
            return p
    return None


def workbuddy_list_sessions(cwd=None, limit=50, keyword=""):
    """List WorkBuddy sessions from ~/.workbuddy/projects/."""
    projects_dir = WORKBUDDY_DIR / "projects"
    if not projects_dir.exists():
        return []
    sessions = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        project_slug = project_dir.name
        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            sid = jsonl_file.stem
            if sid.startswith("agent-"):
                continue
            msg_count, first_prompt, ai_title = _workbuddy_quick_scan(jsonl_file)
            mtime = _normalize_timestamp(jsonl_file.stat().st_mtime)
            if keyword:
                searchable = (ai_title + " " + first_prompt).lower()
                if keyword.lower() not in searchable:
                    continue
            sessions.append(SessionMeta(
                session_id=sid, full_path=str(jsonl_file),
                created=mtime, modified=mtime,
                message_count=msg_count, git_branch="",
                summary=ai_title[:100] if ai_title else "",
                first_prompt=first_prompt[:200] if first_prompt else "",
                project_path=project_slug,
            ))
    sessions.sort(key=lambda s: str(s.created or ""), reverse=True)
    return sessions[:limit]


def _workbuddy_quick_scan(jsonl_path):
    """Quick scan: count messages, extract first prompt and title."""
    user_count = 0
    first_prompt = ""
    ai_title = ""
    for rec in _iter_jsonl(jsonl_path):
        rtype = rec.get("type", "")
        if rtype == "message" and rec.get("role") == "user":
            user_count += 1
            if not first_prompt:
                content = rec.get("content", [])
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "input_text":
                        text = c.get("text", "")
                        match = re.search(r'<system-reminder[^>]*>(.*?)</system-reminder>', text, re.DOTALL)
                        if match:
                            after = text[match.end():].strip()
                            first_prompt = after[:200] if after else text[:200]
                        else:
                            first_prompt = text[:200]
                        break
        elif rtype == "ai-title":
            ai_title = rec.get("aiTitle", "")
    return user_count, first_prompt, ai_title


def workbuddy_session_stats(session_dir):
    """Get stats for a WorkBuddy session."""
    path = Path(session_dir)
    if not path.exists():
        return _empty_stats("workbuddy")
    stats = _empty_stats("workbuddy")
    stats["slug"] = path.stem
    for rec in _iter_jsonl(path):
        rtype = rec.get("type", "")
        ts_ms = rec.get("timestamp", 0)
        ts = _normalize_timestamp(ts_ms) if ts_ms else ""
        if ts:
            if not stats["started"] or ts < stats["started"]:
                stats["started"] = ts
            if ts > stats["ended"]:
                stats["ended"] = ts
        if rtype == "message":
            role = rec.get("role", "")
            if role == "user":
                stats["user_messages"] += 1
            elif role == "assistant":
                stats["assistant_messages"] += 1
                pd = rec.get("providerData", {})
                model = pd.get("requestModelName", "") or pd.get("model", "")
                if model and not stats["model"]:
                    stats["model"] = model
                usage = rec.get("message", {}).get("usage", {})
                if usage:
                    stats["input_tokens"] += usage.get("input_tokens", 0)
                    stats["output_tokens"] += usage.get("output_tokens", 0)
                    stats["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
        elif rtype == "function_call":
            stats["tool_calls"] += 1
            pd = rec.get("providerData", {})
            usage = pd.get("usage", {})
            if usage:
                stats["input_tokens"] += usage.get("inputTokens", 0)
                stats["output_tokens"] += usage.get("outputTokens", 0)
        elif rtype == "function_call_result":
            output = rec.get("output", "")
            if isinstance(output, dict) and output.get("type") == "text":
                text = output.get("text", "")
                code_match = re.search(r'Exit Code:\\s*(\\d+)', text)
                if code_match and int(code_match.group(1)) != 0:
                    stats["errors"] += 1
        elif rtype == "file-history-snapshot":
            backups = rec.get("snapshot", {}).get("trackedFileBackups", {})
            fc = len(backups) if isinstance(backups, dict) else 0
            if fc > stats.get("files_edited", 0):
                stats["files_edited"] = fc
        elif rtype == "summary":
            stats["summary"] = rec.get("summary", "")[:100]
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


def workbuddy_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from a WorkBuddy session."""
    path = Path(session_dir)
    if not path.exists():
        return
    calls = {}
    results = {}
    for rec in _iter_jsonl(path):
        rtype = rec.get("type", "")
        ts_ms = rec.get("timestamp", 0)
        ts = _normalize_timestamp(ts_ms) if ts_ms else ""
        if rtype == "function_call":
            name = rec.get("name", "")
            if tool_filter and name != tool_filter:
                continue
            call_id = rec.get("callId", "")
            args = rec.get("arguments", "")
            calls[call_id] = {"name": name, "ts": ts, "input_preview": str(args)[:150] if args else ""}
        elif rtype == "function_call_result":
            call_id = rec.get("callId", "")
            output = rec.get("output", "")
            is_error = False
            if isinstance(output, dict) and output.get("type") == "text":
                text = output.get("text", "")
                code_match = re.search(r'Exit Code:\\s*(\\d+)', text)
                if code_match and int(code_match.group(1)) != 0:
                    is_error = True
                results[call_id] = {"preview": text[:150].replace("\\n", " ") if text else "", "is_error": is_error}
            elif isinstance(output, list):
                texts = [c.get("text", "") for c in output if isinstance(c, dict)]
                results[call_id] = {"preview": " ".join(texts)[:150].replace("\\n", " "), "is_error": False}
    yield from _match_call_results(calls, results, errors_only, limit)


def workbuddy_session_path(cwd, session_id=None):
    """Find a WorkBuddy session file."""
    projects_dir = WORKBUDDY_DIR / "projects"
    if not projects_dir.exists():
        return None
    if session_id:
        for p in projects_dir.rglob(f"{session_id}.jsonl"):
            return p
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Trae CN (ByteDance) Adapter
# ═══════════════════════════════════════════════════════════════════════════

def trae_list_sessions(cwd=None, limit=50, keyword=""):
    """List Trae CN sessions from ~/.trae-cn/memory/projects/."""
    memory_dir = TRAE_DIR / "memory" / "projects"
    if not memory_dir.exists():
        return []
    sessions = {}
    for project_dir in sorted(memory_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        for date_dir in sorted(project_dir.iterdir()):
            if not date_dir.is_dir() or not date_dir.name.isdigit():
                continue
            for jsonl_file in sorted(date_dir.glob("session_memory_*.jsonl")):
                sid = jsonl_file.stem.replace("session_memory_", "")
                if sid not in sessions:
                    sessions[sid] = {
                        "path": str(jsonl_file), "project": project_dir.name,
                        "intents": [], "date": date_dir.name,
                        "mtime": _normalize_timestamp(jsonl_file.stat().st_mtime),
                    }
                sessions[sid]["intents"].extend(_trae_extract_intents(jsonl_file))
    result = []
    for sid, info in sessions.items():
        first_intent = info["intents"][0] if info["intents"] else ""
        summary = " | ".join(info["intents"][:3])
        if keyword and keyword.lower() not in summary.lower():
            continue
        result.append(SessionMeta(
            session_id=sid, full_path=info["path"],
            created=info["mtime"], modified=info["mtime"],
            message_count=len(info["intents"]), git_branch="",
            summary=summary[:100], first_prompt=first_intent[:200],
            project_path=info["project"],
        ))
    result.sort(key=lambda s: str(s.created or ""), reverse=True)
    return result[:limit]


def _trae_extract_intents(jsonl_path):
    """Extract intent strings from a Trae CN memory JSONL."""
    return [rec.get("intent", "") for rec in _iter_jsonl(jsonl_path) if rec.get("intent")]


def trae_session_stats(session_dir):
    """Get stats for a Trae CN session (summary-level only)."""
    path = Path(session_dir)
    if not path.exists():
        return _empty_stats("trae-cn")
    stats = _empty_stats("trae-cn")
    stats["model"] = "trae-cn (summary only)"
    stats["slug"] = path.stem
    for rec in _iter_jsonl(path):
        ts = rec.get("message_summary_time", "")
        if ts:
            if not stats["started"] or ts < stats["started"]:
                stats["started"] = ts
            if ts > stats["ended"]:
                stats["ended"] = ts
        intent = rec.get("intent", "")
        if intent:
            stats["user_messages"] += 1
        outcome = rec.get("outcome", "")
        if outcome:
            stats["assistant_messages"] += 1
        actions = rec.get("actions", [])
        if isinstance(actions, list):
            stats["tool_calls"] += len(actions)
    stats["summary"] = f"Trae CN summary: {stats['user_messages']} turns"
    return stats


def trae_extract_messages(session_dir, role="both", limit=0, thinking_limit=0):
    """Extract summarized messages from a Trae CN session."""
    path = Path(session_dir)
    if not path.exists():
        return
    count = 0
    for rec in _iter_jsonl(path):
        ts = rec.get("message_summary_time", "")
        if role in ("user", "both"):
            intent = rec.get("intent", "")
            if intent:
                yield {"role": "USER", "timestamp": ts, "text": f"[意图] {intent}"}
                count += 1
        if role in ("assistant", "both"):
            parts = []
            actions = rec.get("actions", [])
            if actions:
                parts.append("[动作] " + " | ".join(actions))
            outcome = rec.get("outcome", "")
            if outcome:
                parts.append(f"[结果] {outcome}")
            learned = rec.get("learned", [])
            if learned:
                parts.append("[收获] " + " | ".join(learned))
            if parts:
                yield {"role": "ASSISTANT", "timestamp": ts, "text": "\\n".join(parts)}
                count += 1
        if limit and count >= limit:
            return


def trae_extract_tools(session_dir, tool_filter="", errors_only=False, limit=0):
    """Extract action summaries from a Trae CN session."""
    path = Path(session_dir)
    if not path.exists():
        return
    count = 0
    for rec in _iter_jsonl(path):
        ts = rec.get("message_summary_time", "")
        actions = rec.get("actions", [])
        if not isinstance(actions, list):
            continue
        for action in actions:
            if tool_filter and tool_filter.lower() not in action.lower():
                continue
            if limit and count >= limit:
                return
            yield {"timestamp": ts, "name": f"[trae-action] {action[:50]}", "status": "ok", "key_input": action[:150], "result_preview": ""}
            count += 1


def trae_session_path(cwd, session_id=None):
    """Find a Trae CN session file."""
    memory_dir = TRAE_DIR / "memory" / "projects"
    if not memory_dir.exists():
        return None
    if session_id:
        for p in memory_dir.rglob(f"session_memory_{session_id}.jsonl"):
            return p
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Universal Fallback Adapter
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# SchemaProbe — 自动发现未知 JSONL 格式的字段结构
# 核心思想：采样一次 → 推断字段映射 → 定向提取。
# 不猜测，不穷举，效率高且准确率远高于全盘硬编码试探。
# 对所有未知环境即开即用，无需逐个适配。

def universal_session_path(cwd, session_id=None):
    """Universal path finder: search for any JSONL file matching session_id."""
    home = Path.home()
    if session_id:
        for p in home.rglob(f"*{session_id}*.jsonl"):
            return p
    return None


_SCHEMA_PROBE_CACHE = {}  # path → schema dict

def _probe_schema(jsonl_path, force=False):
    """
    探测 JSONL 文件的字段结构，返回 schema 描述字典。
    结果会缓存，重复调用直接命中。

    Returns:
        dict with keys:
        - style: "nested_message" | "nested_payload" | "flat" | "unknown"
        - type_path: list of keys to find role (e.g. ["type"] or ["message", "type"])
        - content_path: list of keys to navigate to text content
        - timestamp_field: which field holds timestamps
        - model_field: which field holds model name (if any)
        - tool_style: "type_based" if tool calls have a distinct type value
    """
    path_str = str(jsonl_path)
    if not force and path_str in _SCHEMA_PROBE_CACHE:
        return _SCHEMA_PROBE_CACHE[path_str]

    # 采样前 30 条记录
    samples = []
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 30:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    samples.append(rec)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    schema = {
        "style": "unknown",
        "type_path": ["type"],        # where to find role indicator
        "content_path": ["content"],  # where to find text content
        "timestamp_field": "timestamp",
        "model_field": None,
        "tool_style": None,
    }

    if not samples:
        _SCHEMA_PROBE_CACHE[path_str] = schema
        return schema

    # ---------- 1. 检测嵌套结构 ----------
    has_message_nest = any(isinstance(r.get("message"), dict) for r in samples)
    has_payload_nest = any(isinstance(r.get("payload"), dict) for r in samples)
    # Grok/Cline style: flat structure with type/content at top level
    has_flat_type = any(r.get("type") in ("user", "assistant", "system", "tool_result",
                                            "reasoning", "tool_use", "tool_call")
                        for r in samples)

    if has_message_nest:
        schema["style"] = "nested_message"
        # 内容可能在 message.content 或 message.payload.user_input
        # 检查 message 内部更深层的结构
        msg_content_found = False
        for r in samples:
            msg = r.get("message", {})
            if not isinstance(msg, dict):
                continue
            # Kimi style: message.payload.user_input[].text
            payload = msg.get("payload", {})
            if isinstance(payload, dict) and payload.get("user_input"):
                schema["content_path"] = ["message", "payload", "user_input"]
                msg_content_found = True
                break
            # Claude style: message.content (string or list of blocks)
            content = msg.get("content")
            if content:
                # Check if content has text-like data
                if isinstance(content, (str, list)):
                    schema["content_path"] = ["message", "content"]
                    msg_content_found = True
                    break
        if not msg_content_found:
            schema["content_path"] = ["message", "content"]
        # Model field
        for r in samples:
            msg = r.get("message", {})
            if isinstance(msg, dict) and msg.get("model"):
                schema["model_field"] = ["message", "model"]
                break
    elif has_payload_nest:
        schema["style"] = "nested_payload"
        # Codex style: payload.content[] where blocks have type "input_text"/"output_text"
        # 以及 payload.role 存放角色信息
        schema["content_path"] = ["payload", "content"]
    elif has_flat_type:
        schema["style"] = "flat"
        # Grok/Cline style: content is string or list of text blocks at top level
        schema["content_path"] = ["content"]
        # Model field for flat formats (Grok uses model_id)
        for r in samples:
            if r.get("type") == "assistant" and r.get("model_id"):
                schema["model_field"] = "model_id"
                break

    # ---------- 2. 确定 type/role 字段路径 ----------
    # 常见的 user/assistant 指示值
    # 尽可能覆盖已知格式的角色指示值
    user_vals = frozenset({
        "user", "human", "turn.prompt", "user_message", "turnbegin",
        "user_msg", "prompt",
        # zcode trace: turn_started contains user input
        "turn_started",
    })
    assistant_vals = frozenset({
        "assistant", "ai", "bot", "agent", "text", "content.part",
        "agent_message", "contentpart", "assistant_msg",
        "reasoning",
        # Kimi Code: context.append_loop_event contains assistant-generated content
        "context.append_loop_event",
        # zcode trace: model_complete has the final response content
        "model_complete",
    })
    tool_vals = frozenset({
        "tool_call", "function_call", "tool.call", "toolcall",
        "toolcall", "functioncall",
        # zcode trace: tool_call_scheduled has toolName and input
        "tool_call_scheduled",
    })

    # 候选字段路径：从最外层到最内层
    candidate_paths = []
    # 顶层字段
    for r in samples:
        for key in ("type", "role"):
            if r.get(key):
                candidate_paths.append([key])
                break
        break
    # 嵌套字段
    if has_message_nest:
        candidate_paths.append(["message", "type"])
        candidate_paths.append(["message", "role"])
    if has_payload_nest:
        candidate_paths.append(["payload", "type"])
        candidate_paths.append(["payload", "role"])

    # 测试每条路径，找能区分 user/assistant 的最佳路径
    # 评分策略：有 user 匹配 AND assistant 匹配 > 只有一类匹配 > 无匹配
    best_path = ["type"]
    best_score = -1
    for path in candidate_paths:
        n_user = 0
        n_ass = 0
        for r in samples:
            cur = r
            for key in path:
                if isinstance(cur, dict):
                    cur = cur.get(key, {})
                else:
                    cur = {}
                    break
            val = str(cur).lower() if not isinstance(cur, dict) else ""
            if val in user_vals:
                n_user += 1
            elif val in assistant_vals:
                n_ass += 1
        # 评分：有区分度（user + ass > 0）> 只有一类 > 无匹配
        if n_user > 0 and n_ass > 0:
            score = 100 + n_user + n_ass
        elif n_user > 0 or n_ass > 0:
            score = n_user + n_ass
        else:
            score = 0
        # Tiebreaker: prefer shorter paths (top-level > nested) when scores are equal.
        # This prevents ['message', 'role'] from beating ['type'] in Kimi Code where
        # both paths can find user/assistant values, but ['type'] is the canonical
        # discriminator (turn.prompt vs context.append_loop_event).
        if score > best_score or (score == best_score and len(path) < len(best_path)):
            best_score = score
            best_path = path

    schema["type_path"] = best_path

    # ---------- 3. 检测工具调用模式 ----------
    for r in samples:
        # 顶层类型检测
        t = str(r.get("type", "")).lower()
        if t in tool_vals:
            schema["tool_style"] = "type_based"
            break
        # 嵌套 payload.type 检测（如 Codex payload.type = "function_call"）
        for nest_key in ("message", "payload"):
            nest = r.get(nest_key, {})
            if isinstance(nest, dict):
                nt = str(nest.get("type", "")).lower()
                if nt in tool_vals or "function_call" in nt:
                    schema["tool_style"] = "nested"
                    break
        # Kimi Code 风格：context.append_loop_event 中的 event.type=tool.call
        if r.get("type") == "context.append_loop_event":
            event = r.get("event", {})
            if isinstance(event, dict):
                et = str(event.get("type", "")).lower()
                if et in tool_vals or "tool.call" in et:
                    schema["tool_style"] = "nested_event"
                    break
        # Grok 风格：assistant 消息中有 tool_calls 数组
        if r.get("type") == "assistant" and isinstance(r.get("tool_calls"), list):
            schema["tool_style"] = "embedded_array"
            break
        if schema["tool_style"]:
            break

    # ---------- 4. 检测关键字段 ----------
    for key in ("timestamp", "time", "ts", "createTime", "created_at", "date", "updated_at", "updateTime"):
        if any(r.get(key) for r in samples):
            schema["timestamp_field"] = key
            break

    if not schema["model_field"]:
        # Check nested model field first (e.g. message.model for Claude)
        for key in ("model", "model_name", "model_id", "engine"):
            if any(r.get(key) for r in samples):
                schema["model_field"] = key
                break
        # Also check model_id inside assistant messages (Grok style)
        if not schema["model_field"]:
            for r in samples:
                if r.get("type") == "assistant" and r.get("model_id"):
                    schema["model_field"] = "model_id"
                    break

    _SCHEMA_PROBE_CACHE[path_str] = schema
    return schema


def _schema_get_text(schema, rec):
    """用 schema 从单条记录中提取文本内容。"""
    path = schema["content_path"]
    current = rec
    for key in path:
        if isinstance(current, dict):
            current = current.get(key, {})
        else:
            current = {}
            break

    if isinstance(current, str) and current.strip():
        return current[:500]
    if isinstance(current, list):
        texts = []
        for block in current:
            if isinstance(block, dict):
                bt = block.get("text", "")
                if bt:
                    texts.append(str(bt))
            elif isinstance(block, str):
                texts.append(block)
        if texts:
            return "\n".join(texts)[:500]

    # Kimi 风格回退：message.payload.text
    if len(path) >= 2 and path[:2] == ["message", "payload"]:
        payload = rec.get("message", {}).get("payload", {})
        if isinstance(payload, dict):
            text = payload.get("text", "")
            if isinstance(text, str) and text.strip():
                return text[:500]
            user_input = payload.get("user_input", [])
            if isinstance(user_input, list):
                texts = [u.get("text", "") for u in user_input if isinstance(u, dict) and u.get("text")]
                if texts:
                    return "\n".join(texts)[:500]

    # Codex 风格回退：payload.content
    if schema.get("style") == "nested_payload":
        payload = rec.get("payload", {})
        if isinstance(payload, dict):
            content = payload.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        for k in ("text", "message"):
                            v = block.get(k, "")
                            if isinstance(v, str) and v.strip():
                                return v[:500]
                    elif isinstance(block, str) and block.strip():
                        return block[:500]
            elif isinstance(content, str) and content.strip():
                return content[:500]

    # 回退：直接搜常见文本字段
    for key in ("text", "input", "prompt", "query", "message_text"):
        val = rec.get(key, "")
        if isinstance(val, str) and val.strip():
            return val[:500]
    return ""


def _schema_get_timestamp(schema, rec):
    """用 schema 从单条记录中提取时间戳。"""
    ts = rec.get(schema["timestamp_field"], "")
    if not ts:
        # Fallback: check common timestamp field variants
        for key in ("timestamp", "time", "ts", "createTime", "created_at"):
            if key != schema["timestamp_field"]:
                val = rec.get(key, "")
                if val:
                    return val
    return ts


def _schema_get_model(schema, rec):
    """用 schema 从单条记录中提取模型名。"""
    path = schema.get("model_field")
    if not path:
        return ""
    if isinstance(path, list):
        current = rec
        for key in path:
            if isinstance(current, dict):
                current = current.get(key, "")
            else:
                return ""
        return str(current)
    return str(rec.get(path, ""))


def _schema_is_role(schema, rec, target):
    """判断记录是否匹配目标角色（支持嵌套路径）。"""
    type_path = schema.get("type_path", ["type"])
    cur = rec
    for key in type_path:
        if isinstance(cur, dict):
            cur = cur.get(key, "")
        else:
            cur = ""
            break
    val = str(cur).lower() if not isinstance(cur, dict) else ""
    if target == "user":
        return val in ("user", "human", "turn.prompt", "user_message", "turnbegin", "user_msg", "prompt",
                       "turn_started")
    elif target == "assistant":
        return val in ("assistant", "ai", "bot", "agent", "text", "content.part", "contentpart",
                       "agent_message", "assistant_msg", "reasoning", "response_item",
                       "context.append_loop_event", "model_complete")
    elif target == "tool_call":
        return val in ("tool_call", "function_call", "tool.call", "toolcall", "functioncall",
                       "toolcall", "tool_call_scheduled") or "function_call" in val
    return False


def _schema_is_user(schema, rec):
    return _schema_is_role(schema, rec, "user")


def _schema_is_assistant(schema, rec):
    return _schema_is_role(schema, rec, "assistant")


def _schema_is_tool_call(schema, rec):
    return _schema_is_role(schema, rec, "tool_call")


# ---------- 改造后的 universal 适配器 ----------

def universal_list_sessions(home_dir=None, env_name="unknown", limit=50, keyword=""):
    """Universal session discovery: find JSONL files in any environment directory."""
    if home_dir is None:
        home_dir = Path.home()
    search_dirs = []
    for pattern in ["sessions", "projects", "memory", "data"]:
        candidate = home_dir / pattern
        if candidate.exists():
            search_dirs.append(candidate)
    jsonl_files = []
    for search_dir in search_dirs:
        jsonl_files.extend(search_dir.rglob("*.jsonl"))
    if not jsonl_files:
        jsonl_files = list(home_dir.rglob("*.jsonl"))
    sessions = []
    for jf in sorted(jsonl_files, key=lambda p: p.stat().st_mtime, reverse=True):
        if jf.stat().st_size < 100:
            continue
        sid = jf.stem
        mtime = _normalize_timestamp(jf.stat().st_mtime)
        first_prompt = _universal_quick_scan(jf)
        if keyword and keyword.lower() not in first_prompt.lower():
            continue
        sessions.append(SessionMeta(
            session_id=sid, full_path=str(jf),
            created=mtime, modified=mtime,
            message_count=0, git_branch="",
            summary=f"[{env_name}] {jf.parent.name}",
            first_prompt=first_prompt[:200],
            project_path=str(jf.parent),
        ))
    return sessions[:limit]


def _universal_quick_scan(jsonl_path):
    """快速扫描：用 SchemaProbe 提取第一条用户消息。"""
    schema = _probe_schema(jsonl_path)
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if _schema_is_user(schema, rec):
                    text = _schema_get_text(schema, rec)
                    if text:
                        return text[:200]
                # 也尝试直接提取任何文本
                text = _schema_get_text(schema, rec)
                if text:
                    return text[:200]
    except OSError:
        pass
    return ""


def universal_session_stats(session_path):
    """Universal stats: 用 SchemaProbe 识别消息类型后统计。"""
    path = Path(session_path)
    stats = _empty_stats("unknown")
    stats["slug"] = path.stem
    if not path.exists():
        return stats
    schema = _probe_schema(session_path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                # 时间戳
                ts = _schema_get_timestamp(schema, rec)
                if ts:
                    nt = _normalize_timestamp(ts)
                    if nt:
                        if not stats["started"] or nt < stats["started"]:
                            stats["started"] = nt
                        if nt > stats["ended"]:
                            stats["ended"] = nt
                # 模型
                if not stats["model"]:
                    model = _schema_get_model(schema, rec)
                    if model:
                        stats["model"] = model
                # 角色计数 — 用 schema 判定，不再靠硬编码关键词
                if _schema_is_user(schema, rec):
                    stats["user_messages"] += 1
                elif _schema_is_assistant(schema, rec):
                    stats["assistant_messages"] += 1
                elif _schema_is_tool_call(schema, rec):
                    stats["tool_calls"] += 1
    except OSError:
        pass
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


def universal_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Universal message extraction: 用 SchemaProbe 精准提取。"""
    path = Path(session_path)
    if not path.exists():
        return
    schema = _probe_schema(session_path)
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = _schema_get_timestamp(schema, rec)
                nt = _normalize_timestamp(ts) if ts else ""

                if role in ("user", "both") and _schema_is_user(schema, rec):
                    text = _schema_get_text(schema, rec)
                    # zcode trace: turn_started has payload.input
                    if not text and rec.get("type") == "turn_started":
                        payload = rec.get("payload", {})
                        if isinstance(payload, dict):
                            inp = payload.get("input", "")
                            if isinstance(inp, str) and inp.strip():
                                text = inp[:500]
                            elif isinstance(inp, list):
                                texts = [str(i.get("text", "")) for i in inp if isinstance(i, dict) and i.get("text")]
                                if texts:
                                    text = "\n".join(texts)[:500]
                    if text:
                        # Filter system-reminder noise (qoder and similar)
                        stripped = text.strip()
                        if stripped.startswith("<system-reminder>"):
                            # Strip the system-reminder block, keep real content after it
                            if "</system-reminder>" in stripped:
                                after = stripped.split("</system-reminder>", 1)[-1].strip()
                                if after:
                                    text = after
                                else:
                                    continue
                            else:
                                continue
                        if stripped.startswith("<runtime_context>"):
                            continue
                        yield {"role": "USER", "timestamp": nt, "text": text}
                        count += 1
                        continue
                if role in ("assistant", "both") and _schema_is_assistant(schema, rec):
                    text = _schema_get_text(schema, rec)
                    # zcode trace: model_complete has payload.content
                    if not text and rec.get("type") == "model_complete":
                        payload = rec.get("payload", {})
                        if isinstance(payload, dict):
                            content = payload.get("content", [])
                            if isinstance(content, list):
                                texts = []
                                for block in content:
                                    if isinstance(block, dict):
                                        for k in ("text", "message"):
                                            v = block.get(k, "")
                                            if isinstance(v, str) and v.strip():
                                                texts.append(v)
                                    elif isinstance(block, str) and block.strip():
                                        texts.append(block)
                                if texts:
                                    text = "\n".join(texts)[:500]
                            elif isinstance(content, str) and content.strip():
                                text = content[:500]
                    if text:
                        yield {"role": "ASSISTANT", "timestamp": nt, "text": text}
                        count += 1
                        continue
                if limit and count >= limit:
                    return
    except OSError:
        pass


def universal_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Universal tool extraction: 用 SchemaProbe 检测工具调用模式。"""
    path = Path(session_path)
    if not path.exists():
        return
    schema = _probe_schema(session_path)
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not _schema_is_tool_call(schema, rec):
                    continue
                ts = _schema_get_timestamp(schema, rec)
                nt = _normalize_timestamp(ts) if ts else ""
                name = rec.get("name", rec.get("tool_name", ""))
                if tool_filter and name and name != tool_filter:
                    continue
                if errors_only:
                    # Check for error indicators in the record
                    is_err = rec.get("isError", False) or rec.get("status") in ("error", "failed") or bool(rec.get("error"))
                    if not is_err:
                        continue
                    status = "error"
                else:
                    status = rec.get("status", "ok")
                if limit and count >= limit:
                    return
                args = rec.get("arguments", rec.get("input", rec.get("args", "")))
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                yield {"timestamp": nt, "name": name or f"[tool]", "status": status, "key_input": str(args)[:150] if args else "", "result_preview": ""}
                count += 1
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Environment Registry
# ═══════════════════════════════════════════════════════════════════════════

ENV_REGISTRY = {
    "claude": {"name": "Claude Code", "root": "~/.claude/projects/", "format": "jsonl", "adapter": "claude"},
    "grok": {"name": "Grok Build", "root": "~/.grok/sessions/", "format": "jsonl", "adapter": "grok"},
    "kimi_code": {"name": "Kimi Code", "root": "~/.kimi-code/sessions/", "format": "jsonl", "adapter": "kimi_code"},
    "codex": {"name": "Codex (OpenAI)", "root": "~/.codex/sessions/", "format": "jsonl", "adapter": "codex"},
    "workbuddy": {"name": "WorkBuddy", "root": "~/.workbuddy/projects/", "format": "jsonl", "adapter": "workbuddy"},
    "trae_cn": {"name": "Trae CN (ByteDance)", "root": "~/.trae-cn/memory/projects/", "format": "jsonl-summary", "adapter": "trae_cn"},
    "zcode": {"name": "ZCode (Z-AI)", "root": "~/.zcode/cli/agents/", "format": "jsonl-trace", "adapter": "zcode"},
    "dim": {"name": "DIM (Memory)", "root": "~/.dim/memory/", "format": "jsonl-summary", "adapter": "dim"},
    "dimcode": {"name": "DimCode (SQLite)", "root": "~/.dimcode/v2/dimcode.sqlite", "format": "sqlite", "adapter": "dimcode"},
    "reasonix": {"name": "Reasonix", "root": "~/.reasonix/sessions/", "format": "jsonl", "adapter": "reasonix"},
}

KNOWN_UNADAPTED = {
    "mimo": {"name": "MiMo", "root": "~/.mimo/projects/"},
    "qwen": {"name": "Qwen Code", "root": "~/.qwen/projects/"},
    "qoder": {"name": "Qoder", "root": "~/.qoder/cache/projects/"},
    "openclaw-autoclaw": {"name": "OpenClaw AutoClaw", "root": "~/.openclaw-autoclaw/agents/"},
    "gstack": {"name": "GStack", "root": "~/.gstack/sessions/"},
    "codebuddy": {"name": "CodeBuddy", "root": "~/.codebuddy/sessions/"},
    "cc-switch": {"name": "CC-Switch", "root": "~/.cc-switch/"},
}


def scan_all_environments_parallel():
    """Parallel scan of all known and unknown environments."""
    results = []

    def _scan_env(env_id, env_info, is_registered=True):
        root = Path(os.path.expanduser(env_info["root"]))
        exists = root.exists()
        session_count = 0
        if exists:
            jsonl_files = _fast_find_jsonl(root)
            session_count = len(jsonl_files)
        return {
            "name": env_info["name"], "env_id": env_id,
            "path": str(root), "exists": exists,
            "session_count": session_count,
            "format": env_info.get("format", "unknown"),
            "adapter": env_info.get("adapter", "universal"),
            "status": "adapted" if is_registered else ("unadapted" if exists else "missing"),
        }

    env_tasks = []
    for env_id, env_info in ENV_REGISTRY.items():
        env_tasks.append((env_id, env_info, True))
    for env_id, env_info in KNOWN_UNADAPTED.items():
        env_tasks.append((env_id, env_info, False))

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_scan_env, eid, info, reg): eid for eid, info, reg in env_tasks}
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception:
                pass

    # Scan home directory for unknown environments
    home = Path.home()
    known_dirs = set()
    for e in list(ENV_REGISTRY.values()) + list(KNOWN_UNADAPTED.values()):
        known_dirs.add(os.path.expanduser(e["root"]).split("/")[0])
    known_dirs.update(str(home / d) for d in (".claude", ".zcode", ".agents", ".config", ".cache", ".npm", ".cargo", ".ssh", ".local"))

    dotdirs = []
    try:
        for dotdir in home.iterdir():
            if not dotdir.is_dir() or not dotdir.name.startswith("."):
                continue
            if str(dotdir) in known_dirs:
                continue
            dotdirs.append(dotdir)
    except OSError:
        pass

    def _scan_unknown_dir(dotdir):
        try:
            jsonl_files = _fast_find_jsonl(dotdir)
            if jsonl_files:
                return {"name": dotdir.name, "env_id": dotdir.name.lstrip("."), "path": str(dotdir), "exists": True, "session_count": len(jsonl_files), "format": "unknown", "adapter": "universal", "status": "discovered"}
        except OSError:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_scan_unknown_dir, d) for d in dotdirs]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception:
                pass

    return results


# ═══════════════════════════════════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════════════════════════════════

def _empty_stats(agent_name):
    """Return the standard stats dict with empty values."""
    return {
        "slug": "", "model": agent_name, "branch": "",
        "started": "", "ended": "",
        "user_messages": 0, "assistant_messages": 0,
        "tool_calls": 0, "files_edited": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "compactions": 0, "summary": "",
        "total_tokens": 0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Path resolution wrappers — resolve session dirs to JSONL file paths
# ═══════════════════════════════════════════════════════════════════════════

def _grok_resolve_path(path):
    """Resolve Grok session dir to chat_history.jsonl file path."""
    p = Path(path)
    if p.is_dir():
        chat = p / "chat_history.jsonl"
        if chat.exists():
            return str(chat)
    return str(p)


def _kimi_code_resolve_path(path):
    """Resolve Kimi Code session dir to agents/main/wire.jsonl file path."""
    p = Path(path)
    if p.is_dir():
        wire = p / "agents" / "main" / "wire.jsonl"
        if wire.exists():
            return str(wire)
        wire = p / "wire.jsonl"  # old kimi format
        if wire.exists():
            return str(wire)
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
    import re

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


def _grok_session_stats(path):
    """Dedicated stats for Grok sessions.

    Grok's chat_history.jsonl has no timestamps — we read summary.json for
    created_at/updated_at. Tool calls are embedded in assistant messages'
    tool_calls array, not as separate records.
    """
    resolved = _grok_resolve_path(path)
    stats = _empty_stats("unknown")
    stats["slug"] = Path(resolved).stem

    # Timestamps, model, and summary from summary.json
    session_dir = Path(resolved).parent
    summary_file = session_dir / "summary.json"
    if summary_file.exists():
        try:
            with open(summary_file, encoding="utf-8") as f:
                summary = json.load(f)
            info = summary.get("info", {})
            created = summary.get("created_at", "")
            updated = summary.get("updated_at", "")
            if created:
                stats["started"] = _normalize_timestamp(created)
            if updated:
                stats["ended"] = _normalize_timestamp(updated)
            stats["summary"] = (summary.get("session_summary") or "")[:100]
            model = summary.get("current_model_id", "")
            if model:
                stats["model"] = model
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: use file mtime if no timestamps from summary
    if not stats["started"] or not stats["ended"]:
        try:
            import os
            mtime = os.path.getmtime(resolved)
            nt = _normalize_timestamp(mtime)
            if nt:
                if not stats["started"]:
                    stats["started"] = nt
                if not stats["ended"]:
                    stats["ended"] = nt
        except OSError:
            pass

    # Count errors from events.jsonl (authoritative source)
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
                    if event.get("type") == "tool_completed" and event.get("outcome") == "error":
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
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


# ---------------------------------------------------------------------------
# Kimi Code dedicated extractor — handles context.append_loop_event content.part
# ---------------------------------------------------------------------------

def kimi_code_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """
    Extract messages from Kimi Code wire.jsonl.

    Kimi Code format:
      - turn.prompt: user input (input[].text)
      - context.append_message: echoed messages (role=user|assistant, content[].text)
      - context.append_loop_event: assistant content (event.type=content.part, event.part.type=text|think)
    """
    p = Path(session_path)
    if p.is_dir():
        wire = p / "agents" / "main" / "wire.jsonl"
        if wire.exists():
            p = wire
        else:
            return

    if not p.exists() or not p.is_file():
        return

    count = 0
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                rtype = rec.get("type", "")
                ts = rec.get("time", rec.get("timestamp", ""))

                # User input: turn.prompt
                if rtype == "turn.prompt" and role in ("user", "both"):
                    inputs = rec.get("input", [])
                    parts = []
                    for inp in (inputs if isinstance(inputs, list) else []):
                        if isinstance(inp, dict) and inp.get("type") == "text":
                            t = inp.get("text", "").strip()
                            if t:
                                parts.append(t)
                    if parts:
                        yield {"role": "USER", "timestamp": _normalize_timestamp(ts) if ts else "", "text": "\n".join(parts)}
                        count += 1
                        if limit and count >= limit:
                            return

                # context.append_message is an echo of turn.prompt — skip to
                # avoid duplicate user messages. turn.prompt is the authoritative
                # source for user input.

                # Assistant content: context.append_loop_event with event.type=content.part
                elif rtype == "context.append_loop_event" and role in ("assistant", "both"):
                    event = rec.get("event", {})
                    if isinstance(event, dict) and event.get("type") == "content.part":
                        part = event.get("part", {})
                        if isinstance(part, dict):
                            pt = part.get("type", "")
                            text = part.get("text", "").strip()
                            if pt == "text" and text:
                                yield {"role": "ASSISTANT", "timestamp": _normalize_timestamp(ts) if ts else "", "text": text[:500]}
                                count += 1
                                if limit and count >= limit:
                                    return
                            elif pt == "think" and text and thinking_limit != -1:
                                if thinking_limit > 0:
                                    text = text[:thinking_limit]
                                yield {"role": "ASSISTANT", "timestamp": _normalize_timestamp(ts) if ts else "", "text": "[THINKING] " + text}
                                count += 1
                                if limit and count >= limit:
                                    return
    except OSError:
        pass


def kimi_code_session_stats(session_path):
    """Stats for Kimi Code session using dedicated extractor."""
    resolved = _kimi_code_resolve_path(session_path)
    stats = _empty_stats("kimi")
    stats["slug"] = Path(resolved).stem

    # Get title/model from state.json if available
    p = Path(session_path)
    if p.is_dir():
        state_file = p / "state.json"
        if state_file.exists():
            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)
                stats["summary"] = (state.get("title") or "")[:100]
                if state.get("model"):
                    stats["model"] = state["model"]
            except (json.JSONDecodeError, OSError):
                pass
    elif p.is_file() and p.parent.parent.parent.name:
        # If given a wire.jsonl path, look for state.json in session dir
        state_file = p.parent.parent.parent / "state.json"
        if state_file.exists():
            try:
                with open(state_file, encoding="utf-8") as f:
                    state = json.load(f)
                stats["summary"] = (state.get("title") or "")[:100]
                if state.get("model"):
                    stats["model"] = state["model"]
            except (json.JSONDecodeError, OSError):
                pass

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
                ts = rec.get("time", rec.get("timestamp", ""))
                if ts:
                    nts = _normalize_timestamp(ts)
                    if nts:
                        if not stats["started"] or nts < stats["started"]:
                            stats["started"] = nts
                        if nts > stats["ended"]:
                            stats["ended"] = nts
                if rtype == "turn.prompt":
                    stats["user_messages"] += 1
                elif rtype == "context.append_loop_event":
                    event = rec.get("event", {})
                    if not isinstance(event, dict):
                        continue
                    etype = event.get("type", "")
                    if etype == "content.part":
                        stats["assistant_messages"] += 1
                    elif etype == "tool.call":
                        stats["tool_calls"] += 1
                    elif etype == "tool.result":
                        # Detect errors: isError flag or error in result
                        result = event.get("result", {})
                        if isinstance(result, dict):
                            if result.get("isError"):
                                stats["errors"] += 1
                            elif isinstance(result.get("output", ""), str) and \
                                    "Exit Code:" in result.get("output", "") and \
                                    "Exit Code: 0" not in result.get("output", ""):
                                stats["errors"] += 1
                elif rtype == "usage.record":
                    usage = rec.get("usage", {})
                    if isinstance(usage, dict):
                        stats["input_tokens"] += int(usage.get("inputOther", usage.get("inputTokens", 0)))
                        stats["output_tokens"] += int(usage.get("output", usage.get("outputTokens", 0)))
                        stats["cache_read_tokens"] += int(usage.get("inputCacheRead", usage.get("cacheReadTokens", 0)))
                        stats["cache_create_tokens"] += int(usage.get("inputCacheCreation", usage.get("cacheCreationTokens", 0)))
                elif rtype == "full_compaction.begin":
                    stats["compactions"] += 1
    except OSError:
        pass
    stats["total_tokens"] = stats["input_tokens"] + stats["output_tokens"]
    return stats


# ---------------------------------------------------------------------------
# WorkBuddy dedicated extractor — handles message|user and message|assistant
# ---------------------------------------------------------------------------

def workbuddy_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """
    Extract messages from WorkBuddy session.

    WorkBuddy format:
      - message with role=user: content[].type=input_text, content[].text
      - message with role=assistant: content[].type=output_text, content[].text
      - reasoning: thinking blocks
    """
    p = Path(session_path)
    if not p.exists() or not p.is_file():
        return

    count = 0
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        ts_ms = rec.get("timestamp", 0)
        ts = _normalize_timestamp(ts_ms) if ts_ms else ""

        if rtype != "message":
            continue

        msg_role = rec.get("role", "")
        content = rec.get("content", [])

        if msg_role == "user" and role in ("user", "both"):
            if isinstance(content, list):
                texts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "input_text":
                        t = c.get("text", "").strip()
                        if t:
                            cleaned = _strip_system_reminder(t)
                            if cleaned:
                                texts.append(cleaned)
                if texts:
                    yield {"role": "USER", "timestamp": ts, "text": "\n".join(texts)[:500]}
                    count += 1
                    if limit and count >= limit:
                        return

        elif msg_role == "assistant" and role in ("assistant", "both"):
            if isinstance(content, list):
                texts = []
                for c in content:
                    if isinstance(c, dict):
                        ct = c.get("type", "")
                        if ct in ("output_text", "text"):
                            t = c.get("text", "").strip()
                            if t:
                                texts.append(t)
                if texts:
                    yield {"role": "ASSISTANT", "timestamp": ts, "text": "\n".join(texts)[:500]}
                    count += 1
                    if limit and count >= limit:
                        return


# ---------------------------------------------------------------------------
# Codex dedicated extractor — handles response_item and event_msg formats
# ---------------------------------------------------------------------------

def codex_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """
    Extract messages from Codex rollout session.

    Codex format:
      - event_msg with payload.type=user_message: payload.message (user text)
      - event_msg with payload.type=agent_message: payload.message (assistant text)
      - response_item with payload.type=message: payload.content[].text (assistant)
      - response_item with payload.type=reasoning: thinking blocks
    """
    p = Path(session_path)
    if not p.exists() or not p.is_file():
        return

    count = 0
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")
        ts = rec.get("timestamp", payload.get("ts", ""))

        if rtype == "event_msg" and ptype == "user_message" and role in ("user", "both"):
            text = payload.get("message", "").strip()
            if text:
                yield {"role": "USER", "timestamp": str(ts), "text": text[:500]}
                count += 1
                if limit and count >= limit:
                    return

        elif rtype == "event_msg" and ptype == "agent_message" and role in ("assistant", "both"):
            text = payload.get("message", "").strip()
            if text:
                yield {"role": "ASSISTANT", "timestamp": str(ts), "text": text[:500]}
                count += 1
                if limit and count >= limit:
                    return

        elif rtype == "response_item" and ptype == "message" and role in ("assistant", "both"):
            content = payload.get("content", [])
            if isinstance(content, list):
                texts = []
                for c in content:
                    if isinstance(c, dict):
                        ct = c.get("type", "")
                        if ct in ("output_text", "text"):
                            t = c.get("text", "").strip()
                            if t:
                                texts.append(t)
                if texts:
                    yield {"role": "ASSISTANT", "timestamp": str(ts), "text": "\n".join(texts)[:500]}
                    count += 1
                    if limit and count >= limit:
                        return

        elif rtype == "response_item" and ptype == "reasoning" and role in ("assistant", "both") and thinking_limit != -1:
            summary = payload.get("summary", "")
            if isinstance(summary, list):
                texts = [s.get("text", "") for s in summary if isinstance(s, dict) and s.get("text")]
                text = " ".join(texts)
            elif isinstance(summary, str):
                text = summary
            else:
                content = payload.get("content", [])
                text = ""
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("text"):
                            text = c["text"]
                            break
            text = text.strip()
            if text:
                if thinking_limit > 0:
                    text = text[:thinking_limit]
                yield {"role": "ASSISTANT", "timestamp": str(ts), "text": "[THINKING] " + text[:300]}
                count += 1
                if limit and count >= limit:
                    return


def codex_session_stats_dedicated(session_path):
    """Stats for Codex using dedicated extractor."""
    p = Path(session_path)
    stats = _empty_stats("codex")
    stats["slug"] = p.stem
    if not p.exists() or not p.is_file():
        return stats
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")
        ts = rec.get("timestamp", "")
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not stats["started"] or nts < stats["started"]:
                    stats["started"] = nts
                if nts > stats["ended"]:
                    stats["ended"] = nts
        if rtype == "event_msg" and ptype == "user_message":
            stats["user_messages"] += 1
        elif rtype == "event_msg" and ptype == "agent_message":
            stats["assistant_messages"] += 1
        elif rtype == "response_item" and ptype == "message":
            stats["assistant_messages"] += 1
        elif rtype == "response_item" and ptype in ("function_call", "custom_tool_call", "tool_search_call"):
            stats["tool_calls"] += 1
        elif rtype == "response_item" and ptype in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
            output = payload.get("output", "")
            if isinstance(output, str) and ("Exit Code:" in output and "Exit Code: 0" not in output):
                stats["errors"] += 1
    return stats


# ═══════════════════════════════════════════════════════════════════════════
# ZCode adapter — trace-format JSONL (turn_started/model_complete/tool_call)
# ═══════════════════════════════════════════════════════════════════════════

def zcode_list_sessions(cwd=None, limit=50, keyword=""):
    """List ZCode sessions from ~/.zcode/cli/agents/."""
    sessions = []
    if not ZCODE_DIR.exists():
        return sessions
    for sess_dir in sorted(ZCODE_DIR.iterdir(), reverse=True):
        if not sess_dir.is_dir() or not sess_dir.name.startswith("sess_"):
            continue
        for agent_dir in sess_dir.iterdir():
            if not agent_dir.is_dir() or not agent_dir.name.startswith("agent_"):
                continue
            transcript = agent_dir / "transcript.jsonl"
            if not transcript.exists():
                continue
            try:
                sid = agent_dir.name
                started = ""
                model = ""
                for rec in _iter_jsonl(transcript):
                    ts = rec.get("timestamp", "")
                    if ts and not started:
                        started = _normalize_timestamp(ts)
                    rtype = rec.get("type", "")
                    if rtype in ("model_network_status", "model_request"):
                        payload = rec.get("payload", {})
                        if isinstance(payload, dict) and payload.get("model"):
                            model = payload.get("modelRef", payload.get("model", ""))
                            break
                    if started and model:
                        break
                sessions.append({
                    "id": sid, "title": f"ZCode {sess_dir.name[:20]}",
                    "created": started, "modified": "",
                    "message_count": 0, "path": str(transcript),
                    "agent": "ZCode", "model": model,
                })
            except OSError:
                continue
    if keyword:
        keyword_lower = keyword.lower()
        sessions = [s for s in sessions if keyword_lower in s.get("title", "").lower()]
    return sessions[:limit]


def zcode_session_stats(session_path):
    """Stats for ZCode trace-format transcript.jsonl."""
    p = Path(session_path)
    stats = _empty_stats("zcode")
    stats["slug"] = p.stem
    if not p.exists() or not p.is_file():
        return stats
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
            inp = payload.get("input", "")
            if (isinstance(inp, str) and inp.strip()) or (isinstance(inp, list) and any(isinstance(i, dict) and i.get("text") for i in inp)):
                stats["user_messages"] += 1
        elif rtype == "model_complete":
            content = payload.get("content", [])
            if (isinstance(content, list) and content) or (isinstance(content, str) and content.strip()):
                stats["assistant_messages"] += 1
        elif rtype == "tool_call_scheduled":
            stats["tool_calls"] += 1
            tool_name = payload.get("toolName", "")
            if tool_name and not stats["model"]:
                stats["model"] = tool_name
        elif rtype in ("model_network_status", "model_request"):
            if not stats["model"]:
                model = payload.get("model", "")
                if model:
                    stats["model"] = model
    return stats


def zcode_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from ZCode trace format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        rtype = rec.get("type", "")
        ts = rec.get("timestamp", "")
        nt = _normalize_timestamp(ts) if ts else ""
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue

        if role in ("user", "both") and rtype == "turn_started":
            inp = payload.get("input", "")
            if isinstance(inp, str) and inp.strip():
                text = inp[:500]
            elif isinstance(inp, list):
                texts = [str(i.get("text", "")) for i in inp if isinstance(i, dict) and i.get("text")]
                text = "\n".join(texts)[:500] if texts else ""
            else:
                text = ""
            if text:
                cleaned = _strip_system_reminder(text)
                if cleaned:
                    yield {"role": "USER", "timestamp": nt, "text": cleaned}
                    count += 1
                    if limit and count >= limit:
                        return
                    continue

        if role in ("assistant", "both") and rtype == "model_complete":
            text = _extract_content_text(payload.get("content", []), max_len=500)
            if text:
                yield {"role": "ASSISTANT", "timestamp": nt, "text": text}
                count += 1
                if limit and count >= limit:
                    return


def zcode_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from ZCode trace format."""
    p = Path(session_path)
    if not p.exists():
        return

    # Pre-scan for error call IDs when errors_only mode
    error_call_ids = set()
    if errors_only:
        for rec in _iter_jsonl(p):
            if rec.get("type") == "tool_batch_complete":
                pl = rec.get("payload", {})
                if isinstance(pl, dict) and pl.get("errorCount", 0) > 0:
                    for tid in pl.get("toolCallIds", []):
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
        if errors_only and payload.get("toolCallId", "") not in error_call_ids:
            continue
        ts = rec.get("timestamp", "")
        nt = _normalize_timestamp(ts) if ts else ""
        args = payload.get("input", "")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        yield {"timestamp": nt, "name": name or "[tool]", "status": "ok",
               "key_input": str(args)[:150] if args else "", "result_preview": ""}
        count += 1
        if limit and count >= limit:
            return


def zcode_session_path(cwd, session_id=None):
    """Resolve ZCode session path."""
    if session_id:
        for sess_dir in ZCODE_DIR.iterdir():
            if not sess_dir.is_dir():
                continue
            for agent_dir in sess_dir.iterdir():
                if agent_dir.name == session_id:
                    return str(agent_dir / "transcript.jsonl")
    return str(ZCODE_DIR)


# ═══════════════════════════════════════════════════════════════════════════
# ZCode DB adapter — SQLite-based, used by topic-scan and zcode-adapter.py
# ═══════════════════════════════════════════════════════════════════════════

_ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


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
    """Parse the JSON `data` field of a message row."""
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

        # Token usage
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


# ═══════════════════════════════════════════════════════════════════════════
# DIM adapter — memory/summary JSONL (intent/actions/learned/outcome)
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
# DimCode adapter — SQLite-based (dimcode.sqlite), same pattern as ZCode DB
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
# Reasonix adapter — flat role/content JSONL + events JSONL
# ═══════════════════════════════════════════════════════════════════════════

def reasonix_list_sessions(cwd=None, limit=50, keyword=""):
    """List Reasonix sessions from ~/.reasonix/sessions/."""
    sessions = []
    if not REASONIX_DIR.exists():
        return sessions
    for jf in sorted(REASONIX_DIR.glob("*.jsonl"), reverse=True):
        if jf.name.endswith(".events.jsonl"):
            continue
        try:
            started = ""
            model = ""
            for rec in _iter_jsonl(jf):
                ts = rec.get("timestamp", rec.get("ts", ""))
                if ts and not started:
                    started = _normalize_timestamp(ts)
                if rec.get("model") and not model:
                    model = rec["model"]
                if started and model:
                    break
            sessions.append({
                "id": jf.stem, "title": f"Reasonix {jf.stem[:20]}",
                "created": started, "modified": "",
                "message_count": 0, "path": str(jf),
                "agent": "Reasonix", "model": model,
            })
        except OSError:
            continue
    if keyword:
        keyword_lower = keyword.lower()
        sessions = [s for s in sessions if keyword_lower in s.get("title", "").lower()]
    return sessions[:limit]


def reasonix_session_stats(session_path):
    """Stats for Reasonix flat role/content format."""
    p = Path(session_path)
    stats = _empty_stats("reasonix")
    stats["slug"] = p.stem
    if not p.exists() or not p.is_file():
        return stats
    for rec in _iter_jsonl(p):
        role = rec.get("role", rec.get("type", ""))
        ts = rec.get("timestamp", rec.get("ts", ""))
        if ts:
            nts = _normalize_timestamp(ts)
            if nts:
                if not stats["started"] or nts < stats["started"]:
                    stats["started"] = nts
                if nts > stats["ended"]:
                    stats["ended"] = nts
        if not stats["model"] and rec.get("model"):
            stats["model"] = rec["model"]
        if role == "user":
            content = rec.get("content", "")
            if (isinstance(content, str) and content.strip()) or (isinstance(content, list) and content):
                stats["user_messages"] += 1
        elif role in ("assistant", "model"):
            stats["assistant_messages"] += 1
        elif role == "tool":
            stats["tool_calls"] += 1
        if role in ("assistant", "model") and isinstance(rec.get("tool_calls"), list):
            stats["tool_calls"] += len(rec["tool_calls"])
        if role == "tool":
            content = rec.get("content", "")
            if isinstance(content, str) and ("error" in content.lower() or "Error" in content):
                stats["errors"] += 1
    return stats


def reasonix_extract_messages(session_path, role="both", limit=0, thinking_limit=0):
    """Extract messages from Reasonix flat format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        role_val = rec.get("role", rec.get("type", ""))
        ts = rec.get("timestamp", rec.get("ts", ""))
        nt = _normalize_timestamp(ts) if ts else ""
        text = _extract_content_text(rec.get("content", ""), max_len=500)
        if not text:
            continue
        if role in ("user", "both") and role_val == "user":
            cleaned = _strip_system_reminder(text)
            if cleaned:
                yield {"role": "USER", "timestamp": nt, "text": cleaned}
                count += 1
                if limit and count >= limit:
                    return
            continue
        if role in ("assistant", "both") and role_val in ("assistant", "model"):
            yield {"role": "ASSISTANT", "timestamp": nt, "text": text}
            count += 1
            if limit and count >= limit:
                return


def reasonix_extract_tools(session_path, tool_filter="", errors_only=False, limit=0):
    """Extract tool calls from Reasonix format."""
    p = Path(session_path)
    if not p.exists():
        return
    count = 0
    for rec in _iter_jsonl(p):
        role = rec.get("role", rec.get("type", ""))
        ts = rec.get("timestamp", rec.get("ts", ""))
        nt = _normalize_timestamp(ts) if ts else ""

        if role in ("assistant", "model") and isinstance(rec.get("tool_calls"), list):
            for tc in rec["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name", tc.get("function", {}).get("name", ""))
                if tool_filter and name != tool_filter:
                    continue
                args = tc.get("arguments", tc.get("function", {}).get("arguments", ""))
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                yield {"timestamp": nt, "name": str(name), "status": "ok",
                       "key_input": str(args)[:150] if args else "", "result_preview": ""}
                count += 1
                if limit and count >= limit:
                    return

        if role == "tool":
            name = rec.get("name", rec.get("tool_name", "tool"))
            if tool_filter and name != tool_filter:
                continue
            content = rec.get("content", "")
            is_error = isinstance(content, str) and ("error" in content.lower())
            if errors_only and not is_error:
                continue
            yield {"timestamp": nt, "name": str(name), "status": "error" if is_error else "ok",
                   "key_input": "", "result_preview": str(content)[:80]}
            count += 1
            if limit and count >= limit:
                return


def reasonix_session_path(cwd, session_id=None):
    """Resolve Reasonix session path."""
    if session_id:
        candidate = REASONIX_DIR / f"{session_id}.jsonl"
        if candidate.exists():
            return str(candidate)
    return str(REASONIX_DIR)


# ═══════════════════════════════════════════════════════════════════════════
# Adapter Registry — lightweight dict-based dispatch
# ═══════════════════════════════════════════════════════════════════════════

ADAPTER_REGISTRY = {}

def register_adapter(name, display_name, **fns):
    """注册一个环境适配器。

    Args:
        name: 唯一标识符（如 "grok", "codex"）
        display_name: 显示名称（如 "Grok Build"）
        **fns: 必须包含 list_sessions, session_stats, extract_messages, extract_tools, session_path
    """
    ADAPTER_REGISTRY[name] = {"name": name, "display_name": display_name, **fns}

# 注册所有内置适配器
register_adapter("claude", "Claude Code",
    list_sessions=broad_list_claude_sessions,
    session_stats=session_stats,
    extract_messages=extract_messages,
    extract_tools=extract_tools,
    session_path=lambda cwd, sid=None: find_project_dir(cwd or os.getcwd()),
)
register_adapter("grok", "Grok Build",
    list_sessions=grok_list_sessions,
    session_stats=_grok_session_stats,
    extract_messages=_grok_extract_messages,
    extract_tools=grok_extract_tools,  # 保留：双文件 events+chat_history 关联
    session_path=grok_session_path,
)
register_adapter("kimi_code", "Kimi Code",
    list_sessions=kimi_code_list_sessions,
    session_stats=kimi_code_session_stats,
    extract_messages=kimi_code_extract_messages,
    extract_tools=kimi_code_extract_tools,  # 保留：处理嵌套 event.tool.call 结构
    session_path=kimi_code_session_path,
)
register_adapter("codex", "Codex (OpenAI)",
    list_sessions=codex_list_sessions,
    session_stats=codex_session_stats_dedicated,
    extract_messages=codex_extract_messages,
    extract_tools=codex_extract_tools,  # 保留：exit code 错误检测
    session_path=codex_session_path,
)
register_adapter("workbuddy", "WorkBuddy",
    list_sessions=workbuddy_list_sessions,
    session_stats=workbuddy_session_stats,  # 保留：providerData 含 model/token
    extract_messages=workbuddy_extract_messages,
    extract_tools=workbuddy_extract_tools,  # 保留：exit code 错误检测
    session_path=workbuddy_session_path,
)
register_adapter("trae_cn", "Trae CN (ByteDance)",
    list_sessions=trae_list_sessions,
    session_stats=trae_session_stats,  # 保留：summary-only 非标准格式
    extract_messages=trae_extract_messages,
    extract_tools=trae_extract_tools,
    session_path=trae_session_path,
)
register_adapter("zcode", "ZCode (Z-AI)",
    list_sessions=zcode_list_sessions,
    session_stats=zcode_session_stats,
    extract_messages=zcode_extract_messages,
    extract_tools=zcode_extract_tools,
    session_path=zcode_session_path,
)
register_adapter("dim", "DIM (Memory)",
    list_sessions=dim_list_sessions,
    session_stats=dim_session_stats,
    extract_messages=dim_extract_messages,
    extract_tools=dim_extract_tools,
    session_path=dim_session_path,
)
register_adapter("dimcode", "DimCode (SQLite)",
    list_sessions=dimcode_list_sessions,
    session_stats=dimcode_session_stats,
    extract_messages=dimcode_extract_messages,
    extract_tools=dimcode_extract_tools,
    session_path=dimcode_session_path,
)
register_adapter("reasonix", "Reasonix",
    list_sessions=reasonix_list_sessions,
    session_stats=reasonix_session_stats,
    extract_messages=reasonix_extract_messages,
    extract_tools=reasonix_extract_tools,
    session_path=reasonix_session_path,
)
register_adapter("universal", "Universal",
    list_sessions=universal_list_sessions,
    session_stats=universal_session_stats,
    extract_messages=universal_extract_messages,
    extract_tools=universal_extract_tools,
    session_path=universal_session_path,
)
