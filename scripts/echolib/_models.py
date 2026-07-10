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
