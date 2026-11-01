import json
import math
import os
import re
from pathlib import Path

from echolib._helpers import (
    CLAUDE_DIR,
    KNOWN_TYPES,
    NOISE_TYPES,
    _NOISE_STRINGS,
)

class Record:
    """Thin wrapper around a parsed JSONL dict with convenience accessors."""

    __slots__ = ("_d",)

    def __init__(self, d):
        self._d = d

    @property
    def raw(self):
        """The underlying raw dict."""
        return self._d

    @property
    def type(self):
        """Record type (e.g. "user", "assistant", "summary")."""
        return self._d.get("type", "")

    @property
    def timestamp(self):
        """ISO timestamp string."""
        return self._d.get("timestamp", "")

    @property
    def message(self):
        """The nested message dict (may be empty)."""
        return self._d.get("message") or {}

    @property
    def content(self):
        """Content field from the message (str, list, or dict)."""
        msg = self.message
        return msg.get("content", "") if isinstance(msg, dict) else ""

    @property
    def model(self):
        """Model name from the assistant message."""
        msg = self.message
        return msg.get("model", "") if isinstance(msg, dict) else ""

    @property
    def usage(self):
        """Token usage dict (input_tokens, output_tokens, etc.)."""
        msg = self.message
        return msg.get("usage", {}) if isinstance(msg, dict) else {}

    @property
    def uuid(self):
        """Unique record identifier."""
        return self._d.get("uuid", "")

    @property
    def session_id(self):
        """Session identifier."""
        return self._d.get("sessionId", "")

    @property
    def git_branch(self):
        """Git branch name at time of recording."""
        return self._d.get("gitBranch", "")

    @property
    def slug(self):
        """Short session slug."""
        return self._d.get("slug", "")

    @property
    def version(self):
        """Schema version string."""
        return self._d.get("version", "")

    @property
    def subtype(self):
        """Record subtype (e.g. "compact_boundary")."""
        return self._d.get("subtype", "")

    def get(self, key, default=None):
        """Dict-style key access with default."""
        return self._d.get(key, default)

    def is_noise(self):
        """True if record is a noise type (progress, queue-operation)."""
        return self.type in NOISE_TYPES

    def is_meta_user(self):
        """True if this is a meta/control message, not a real user turn."""
        return self._d.get("isMeta", False)

    def is_compact_summary(self):
        """True if this is a compacted context summary."""
        return self._d.get("isCompactSummary", False)

    def is_synthetic(self):
        """True if the model field is "<synthetic>" (non-real response)."""
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
        # "message": null（合法 JSON）会让 .get("message", {}) 拿到 None，
        # 下面的 .get("parts") 直接 AttributeError——整个会话的消息在构建期
        # 被静默丢掉。or {} 兜住 null。
        if not c and (self._d.get("message") or {}).get("parts"):
            c = (self._d.get("message") or {}).get("parts")
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
        """Serialise to a tab-separated line for sd-recall TSV output."""
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

_HALF_LIVES = {
    "project": 14, "feedback": 90, "user": 180,
    "reference": 60, "value": 365, "unknown": 30,
}


# ── 跨环境模型名归一（单一真源）──────────────────────────────────────────
# 所有消费方从此处导入，报告 / trend / builder 视图始终显示同一标签。
MODEL_ALIASES = {
    # deepseek — 不同型号不合并：deepseek-flash / deepseek-v4-flash /
    # deepseek-v4-flash-0731 / deepseek-v4.1-flash-expires-on-0910 是四个
    # 不同的模型（机主 2026-09-29 确认；旧表把 deepseek-flash 折叠进
    # deepseek-v4-flash 属于错误归并，已撤销）
    "deepseek-flash": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-v4-flash-0731": "deepseek-v4-flash-0731",
    "deepseek-v4.1-flash-expires-on-0910": "deepseek-v4.1-flash-expires-on-0910",
    "deepseek-v4-pro": "deepseek-v4-pro",
    # 空格变体折叠进同型号连字符名（变体折叠≠型号归并，与上面四型号独立不冲突）
    "deepseek v4 flash": "deepseek-v4-flash",
    "deepseek v4 pro": "deepseek-v4-pro",
    # longcat — 版本号保留：LongCat-2.0 与 LongCat-2.5-Preview 并存，
    # 不得把 2.5 折叠成 2.0（索引曾因此错标 claude 13 条 + zcode 77 条）
    "longcat": "LongCat",
    "longcat-2.0": "LongCat-2.0",
    "longcat-2": "LongCat-2",
    "longcat-2.0-preview": "LongCat-2.0-Preview",
    "longcat-2.5": "LongCat-2.5",
    "longcat-2.5-preview": "LongCat-2.5-Preview",
    # mimo — 版本号保留，2.5 / 2.6 是不同型号
    "mimo-v2.5": "MiMo-v2.5",
    "mimo-v2.5-pro": "MiMo-v2.5-Pro",
    "mimo-v2.6-flash": "MiMo-v2.6-Flash",
    # glm（只合并大小写/路由前缀变体，不合并不同型号）
    "glm-5.2": "GLM-5.2",
    "glm 5.2": "GLM-5.2",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm 5.3 flash": "GLM-5.3-Flash",
    "zai_glm-5.3-flash": "GLM-5.3-Flash",
    # gpt (codex / grok)
    "gpt-5.5": "gpt-5.5",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.2": "gpt-5.2",
    # kimi / grok
    "kimi-for-coding": "kimi-for-coding",
    "grok-4.5": "grok-4.5",
}


def normalize_model_name(name: str) -> str:
    """标准化模型名为可显示的短标签。

    The single canonical implementation. reflect-report used to carry its
    own copy and the two had drifted in BOTH directions: the copy knew how
    to clean a raw ``{'modelId': …}`` dict repr, filter UUID provider ids
    and agent names that landed in the model field; this one knew space
    variants (``deepseek v4 flash``) and the gpt family in its alias table.
    A model string therefore displayed differently depending on which
    module processed it. Merged here, 2026-09-29.
    """
    if not name:
        return ""
    s = str(name).strip()
    # Accidental dict repr from older zcode writes: without this the whole
    # dict became the display label.
    if s.startswith("{"):
        m = re.search(r"['\"]modelId['\"]\s*[:=]\s*['\"]([^'\"]+)", s)
        s = m.group(1) if m else ""
    if not s:
        return ""
    if "/" in s and not s.startswith("http"):
        s = s.split("/")[-1]
    # OpenRouter-style route suffixes ("LongCat-2.0:free") are billing routes
    # of the same model, not different models — strip before version parsing,
    # otherwise the suffix breaks version-preserving rules below. URLs contain
    # "://" and must survive both this and the "/" split above.
    if ":" in s and "://" not in s:
        s = s.split(":", 1)[0].strip()
    if "[" in s:
        s = s.split("[", 1)[0]
    s = s.strip()
    key = s.lower()
    if key in MODEL_ALIASES:
        return MODEL_ALIASES[key]
    # longcat — 版本号保留（2026-09-29 修复：旧规则把 LongCat-2.5-Preview
    # 折叠成 LongCat-2.0-Preview，导致索引错标 90 条会话）
    m = re.match(r"^longcat[-_ ]?v?(\d+(?:\.\d+)?)(?:[-_ ]?(preview))?$", key)
    if m:
        ver, prev = m.group(1), m.group(2)
        return f"LongCat-{ver}" + (f"-{prev.capitalize()}" if prev else "")
    if key.startswith("longcat"):
        return "LongCat"
    # mimo — 版本号保留（2026-09-29 修复：旧规则把 mimo-v2.6 折叠成
    # MiMo-v2.5；mimo-v2.5-pro 之外的裸 mimo 不再发明版本号）
    m = re.match(r"^mimo[-_ ]?v?(\d+(?:\.\d+)?)(?:[-_ ](pro|flash))?$", key)
    if m:
        ver, tag = m.group(1), m.group(2)
        return f"MiMo-v{ver}" + (f"-{tag.capitalize()}" if tag else "")
    if key.startswith("mimo-v2.5") and "pro" in key:
        return "MiMo-v2.5-Pro"
    # UUID provider ids are not model names
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", key):
        return ""
    # Environment / agent names that landed in the model field are not
    # models either. (``kimi-for-coding`` is already an alias key, listed
    # here only to keep the set identical to the copy it replaces.)
    if key in {
        "claude", "codex", "kimi", "zcode", "dimcode", "grok", "workbuddy",
        "dim", "reasonix", "trae_cn", "trae-cn (summary only)", "universal",
        "unknown", "kimi-for-coding", "grok-build",
    }:
        return ""
    return s


