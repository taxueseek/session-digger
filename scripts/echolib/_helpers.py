import json
import os
import re
import shutil
import subprocess
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude" / "projects"

NOISE_TYPES = frozenset({"progress", "queue-operation"})

KNOWN_TYPES = frozenset({
    "user", "assistant", "system", "summary", "progress",
    "queue-operation", "file-history-snapshot", "pr-link",
})

_NOISE_STRINGS = ('"queue-operation"', '"progress"')

# Codex rollout: rollout-YYYY-MM-DDTHH-MM-SS-<uuid>.jsonl[.zst]
CODEX_ROLLOUT_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-fA-F-]{36})\.jsonl(?:\.zst)?$"
)


def _codex_homes():
    """Return unique Codex home directories (CODEX_HOME first, then ~/.codex).

    resume-session and Codex CLI honour CODEX_HOME; older installs only use
    ~/.codex. Scanning both when they differ avoids silent data loss.
    """
    roots = []
    seen = set()
    for raw in (os.environ.get("CODEX_HOME"), str(Path.home() / ".codex")):
        if not raw:
            continue
        p = Path(raw).expanduser()
        try:
            key = str(p.resolve()) if p.exists() else str(p)
        except OSError:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        roots.append(p)
    return roots


def _codex_home():
    """Primary Codex home — matches resume-session / Codex CLI resolution."""
    homes = _codex_homes()
    return homes[0] if homes else Path.home() / ".codex"


def _iter_jsonl(path):
    """Yield parsed JSON records from a JSONL file, skipping blank/error lines.

    Centralises the open-strip-parse-error_skip pattern repeated across 30+
    adapter functions.  Always uses errors="replace" and swallows OSError.

    Also accepts ``*.jsonl.zst`` (Codex compressed rollouts): one transparent
    path so every caller inherits zstd support without per-adapter branches.
    """
    try:
        p = Path(path)
        if str(p).endswith(".jsonl.zst") or p.suffix == ".zst":
            executable = shutil.which("zstd")
            if not executable:
                return
            try:
                completed = subprocess.run(
                    [executable, "-dc", str(p)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                return
            if completed.returncode != 0:
                return
            text = completed.stdout.decode("utf-8", errors="replace")
            for line in text.splitlines():
                if len(line) > 10_000_000:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
            return

        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if len(line) > 10_000_000:  # skip binary/gigantic lines
                    continue
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

def _extract_text_from_block(block):
    """Best-effort single-string extraction from one content block.

    Handles three shapes in priority order:
    1. Anthropic-style blocks: ``{"type": "text"|"input_text", "text": ...}``.
    2. Any caller-supplied dict ``keys`` (resolved in the outer helper).
    3. Bare string.
    Returns "" if nothing usable could be extracted.
    """
    if isinstance(block, str):
        return block.strip()
    if not isinstance(block, dict):
        return ""
    if block.get("type") in (None, "text", "output_text") and block.get("text"):
        return str(block["text"]).strip()
    if block.get("type") == "input_text" and block.get("text"):
        return str(block["text"]).strip()
    return ""


def _extract_content_text(content, keys=("text", "message"), max_len=0):
    """Extract text from a content field that may be str, list, or dict.

    - str: returned directly
    - list: each item unpacked via ``_extract_text_from_block`` (Anthropic-style
      text/input_text first), then any dict ``keys`` fallback, else bare strings
    - dict: checked for one of *keys*; list-valued keys are unpacked block-by-block
    Returns the extracted text (optionally truncated), or "" if nothing found.

    Merged from ``_text_from_message_blob`` (index_builder/_builder.py) so
    that heterogeneous message-shape handling lives in one place.
    """
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        texts = []
        for block in content:
            extracted = _extract_text_from_block(block)
            if extracted:
                texts.append(extracted)
                continue
            if isinstance(block, dict):
                for k in keys:
                    v = block.get(k, "")
                    if isinstance(v, str) and v.strip():
                        texts.append(v)
                        break
        text = " ".join(texts) if texts else ""
    elif isinstance(content, dict):
        text = ""
        for k in keys:
            v = content.get(k, "")
            if isinstance(v, str) and v.strip():
                text = v
                break
            if isinstance(v, list):
                parts = []
                for b in v:
                    ex = _extract_text_from_block(b)
                    if ex:
                        parts.append(ex)
                    elif isinstance(b, dict):
                        for kk in keys:
                            vv = b.get(kk, "")
                            if isinstance(vv, str) and vv.strip():
                                parts.append(vv)
                                break
                if parts:
                    text = " ".join(parts)
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
GROK_DIR = Path.home() / ".grok" / "sessions"
GROK_SEARCH_DB = GROK_DIR / "session_search.sqlite"

KIMI_DIR = Path.home() / ".kimi" / "sessions"
KIMI_CODE_DIR = Path.home() / ".kimi-code" / "sessions"

CODEX_DIR = _codex_home()

CURSOR_DIR = Path.home() / ".cursor"

WORKBUDDY_DIR = Path.home() / ".workbuddy"

TRAE_DIR = Path.home() / ".trae-cn"

ZCODE_DIR = Path.home() / ".zcode" / "cli" / "agents"

DIM_DIR = Path.home() / ".dim" / "memory"

DIMCODE_DB_PATH = Path.home() / ".dimcode" / "v2" / "dimcode.sqlite"

REASONIX_DIR = Path.home() / ".reasonix" / "sessions"


# Generic OS basenames that must never act as project-scope matchers.
_SCOPE_GENERIC_BASENAMES = frozenset({
    "tmp", "temp", "var", "usr", "home", "users", "private",
    "opt", "bin", "etc", "lib", "src", "dev", "mnt", "root",
    "applications", "library", "system", "volumes", "downloads",
    "documents", "desktop", "movies", "music", "pictures",
})


def normalize_session_path(path):
    """Return a concrete transcript path when an adapter yields a session directory.

    Grok (and some others) expose a session *directory*; downstream tools expect
    a JSONL file. Virtual schemes (``dimcode://``) pass through unchanged.
    """
    if path is None:
        return ""
    text = str(path)
    if not text or "://" in text:
        return text
    try:
        p = Path(text)
        if p.is_dir():
            for name in ("chat_history.jsonl", "transcript.jsonl", "conversation.jsonl"):
                candidate = p / name
                if candidate.is_file():
                    return str(candidate)
            jsonls = sorted(p.glob("*.jsonl"))
            if len(jsonls) == 1:
                return str(jsonls[0])
        return text
    except OSError:
        return text


def session_in_cwd(path, cwd, agent=None):
    """True if *path* belongs to project *cwd* (path-segment-boundary safe).

    One shared matcher for Claude dash-encoding, Grok URL-encoding, and plain
    path embeds. Never treats ``$HOME`` / ``/Users`` as a project (would match
    every session). Parent projects do not match children (``bar`` ≠ ``bar-baz``).

    *agent* is accepted for call-site compatibility and ignored — encoding is
    inferred from the path shape so every environment reuses one rule.
    """
    import urllib.parse

    if not path or not cwd:
        return False
    text = str(path)
    if "://" in text and not text.startswith("file://"):
        return False

    try:
        ps = str(Path(text).expanduser().resolve())
    except OSError:
        ps = text
    try:
        cwd_resolved = str(Path(cwd).expanduser().resolve())
    except OSError:
        cwd_resolved = str(cwd)
    try:
        home = str(Path.home().resolve())
    except OSError:
        home = str(Path.home())

    # $HOME / parent-of-home / root are not projects.
    if cwd_resolved in (home, str(Path(home).parent), "/", ""):
        return False

    # Exact project path embedded with segment boundaries.
    if (cwd_resolved + "/") in ps or ps.endswith(cwd_resolved):
        return True

    # Claude-style dash encoding: absolute project path → dash-separated marker
    dash = cwd_resolved.replace("/", "-")
    if dash in ps:
        idx = ps.index(dash)
        after = idx + len(dash)
        if after >= len(ps) or ps[after] in ("/", "."):
            return True

    # Grok-style URL encoding segment.
    encoded_cwd = urllib.parse.quote(cwd_resolved, safe="")
    if encoded_cwd in ps:
        idx = ps.index(encoded_cwd)
        after = idx + len(encoded_cwd)
        if after >= len(ps) or ps[after] in ("/", "."):
            return True

    cwd_basename = cwd_resolved.rstrip("/").split("/")[-1]
    if (
        not cwd_basename
        or cwd_basename.lower() in _SCOPE_GENERIC_BASENAMES
        or cwd_basename == Path.home().name
    ):
        return False

    # Basename fallback: only unambiguous path-separator markers.
    markers = (
        f"/{cwd_basename}/",
        f"%2F{cwd_basename}%2F",
        f"%2F{cwd_basename}/",
        f"/{cwd_basename}%2F",
    )
    return any(m in ps for m in markers)


