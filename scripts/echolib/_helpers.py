import contextlib
import contextvars
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from echolib._registry_data import environment_model_tokens

_log = logging.getLogger("echolib")

# ── Discovery-only mode ────────────────────────────────────────────────
# ``list_sessions`` serves two callers with different needs. A UI list wants a
# preview per session, so each adapter parses the JSONL head to fill
# summary/first_prompt/message_count. The index builder wants only the
# enumeration — ``_scan_via_adapter`` reads session_id and full_path and
# discards the metadata — yet it paid for every preview.
#
# Measured on the live install (6294 sessions, 30 environments): previews were
# 4.35 s of a 5.40 s ``scan_sessions``, i.e. ~72% of the whole incremental
# build, and the discarded work was reproducible byte-for-byte.
#
# A ContextVar rather than a module global: ``cross_tool_list_sessions`` fans
# adapters out over threads, and this flag must never leak into a UI listing.
_DISCOVERY_ONLY = contextvars.ContextVar("session_digger_discovery_only",
                                        default=False)


def discovery_only_active() -> bool:
    """True while the caller only needs session_id/full_path pairs."""
    return _DISCOVERY_ONLY.get()


# ── Parse-once scope ───────────────────────────────────────────────────
# The adapter path reads one transcript three to four times per session —
# stats, tools, messages, and an identity scan — and each pass re-parses the
# whole file from the first byte. Measured 2026-09-17 over a full rebuild of
# the live install: 5,653 parse passes over 1,556 distinct files, i.e.
# 2,833 MB of the 3,862 MB parsed (73%) was a file already parsed moments
# earlier. The single-pass analyzer collapsed this for the layouts it can
# mirror; every other layout still pays it three extra times.
#
# The scope is entered once per session, where the file is known not to change
# underneath the reads (the builder took its fingerprint first), so a parsed
# record list can be replayed instead of re-derived. Bounded by construction:
# the cache is a dict that exists only for the scope, so it holds the files one
# session touches and is dropped on exit. A persistent cache would be wrong
# here — the corpus is 1.8 GB of parsed JSONL and no eviction policy makes that
# fit, while a per-session one needs no policy at all.
#
# A ContextVar rather than a module global, for the same reason as
# ``_DISCOVERY_ONLY``: a nested ``cross_tool_list_sessions`` fan-out must not
# inherit the scope. A fresh thread gets the default (None) and simply does not
# cache, which is the pre-existing behaviour.
_RECORD_CACHE = contextvars.ContextVar("session_digger_record_cache", default=None)


@contextlib.contextmanager
def parse_once():
    """Replay fully-parsed transcripts within one session's computation.

    Contract for callers: **the transcripts read inside this block must not
    change while it is open.** Replays are keyed on path alone, so an
    append-during-read would be served stale bytes for the rest of the block.
    The index builder satisfies this by fingerprinting before it computes.
    """
    token = _RECORD_CACHE.set({})
    try:
        yield
    finally:
        _RECORD_CACHE.reset(token)


def cap(items, limit):
    """Apply the ``list_sessions`` limit convention: 0 (or None) = unlimited.

    One home for a rule that eleven adapters got wrong in the same way:
    ``items[:limit]`` returns ``[]`` for ``limit=0``, so "no cap" silently
    became "no results". The rule is stated in the adapter docstrings and four
    adapters (workbuddy, dsh, zcode_v2, codex) were already fixed
    individually — the fix belongs here so the next adapter inherits it.
    """
    return items[:limit] if limit else items


@contextlib.contextmanager
def discovery_only():
    """Enter discovery-only mode: enumerate sessions, skip per-file previews.

    Contract for adapters inside the block — ``list_sessions`` must still
    return the same ``session_id`` and ``full_path`` for the same filesystem
    state (no phantom, no missing, no reordering-by-content). In exchange it
    may degrade anything derived from parsing the transcript:

    * ``summary`` / ``first_prompt`` fall back to a cheap label (dirname, stem)
      or ``""``;
    * ``message_count`` is 0;
    * ``created`` / ``modified`` still come from the filesystem mtime;
    * ``keyword`` filtering degrades, because the haystack it matched on is the
      preview. Callers must pass ``keyword=""`` — the index builder does;
    * ``cwd`` filtering may return a superset, for the same reason.

    Callers that render previews must not use this. The builder is the only
    production entry point, and it re-derives all of the above from the
    transcript when it actually indexes a changed session.
    """
    token = _DISCOVERY_ONLY.set(True)
    try:
        yield
    finally:
        _DISCOVERY_ONLY.reset(token)


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

    Also accepts compressed rollouts (``*.jsonl.zst`` Codex, ``*.jsonl.zstd``
    DSH): one transparent path so every caller inherits zstd support without
    per-adapter branches.  Prefers the Python 3.14+ stdlib ``compression.zstd``
    (PEP 784, no subprocess) and falls back to the ``zstd`` CLI.

    Inside a :func:`parse_once` scope a *complete* pass is remembered and
    replayed, so the adapter path's stats/tools/messages reads parse once. Two
    details carry the correctness of that replay:

    * a pass commits only when the underlying reader runs to completion. A
      caller that stops early — ``_probe_schema`` deliberately reads 40 records
      and breaks — closes the generator, ``GeneratorExit`` skips the commit, and
      the next caller still gets the whole file. Caching a partial head would
      turn "probe the head cheaply" into "every later read sees 40 records";
    * an unreadable file commits nothing. The reader used to swallow OSError
      itself, which would have made a transient failure indistinguishable from a
      complete empty file and cached that emptiness for the rest of the session.
      The swallow now happens here, above an inner reader that lets OSError out.
    """
    cache = _RECORD_CACHE.get()
    if cache is None:
        try:
            yield from _read_jsonl(path)
        except OSError:
            pass
        return

    key = str(path)
    hit = cache.get(key)
    if hit is not None:
        yield from hit
        return

    records = []
    try:
        for record in _read_jsonl(path):
            records.append(record)
            yield record
    except OSError:
        return
    cache[key] = records


def _read_jsonl(path):
    """Parse every JSON record in ``path``. ``OSError`` propagates by design.

    Lazy, so a caller that only needs the head pays only for the head — the
    cost model ``_probe_schema`` and the discovery paths were built around.
    """
    p = Path(path)
    if str(p).endswith(".jsonl.zst") or p.suffix in (".zst", ".zstd"):
        yield from _iter_compressed_jsonl(p)
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


def _iter_compressed_jsonl(p):
    """Yield parsed records from a zstd-compressed JSONL file.

    stdlib ``compression.zstd`` first (no process spawn); ``zstd`` CLI fallback
    for interpreters without PEP 784.  Both feed the shared line-parse loop.

    Every failure path logs before giving up. It used to ``return`` silently, so
    a truncated or half-written store produced zero records with zero signal —
    indistinguishable from an empty session, and in DSH's case from "the
    transcript is gone" (which the index then treats as a deleted session).
    A corrupt archive is a data problem the user can fix; it must not be quiet.
    """
    lines = None
    try:
        from compression import zstd as _zstd  # Python 3.14+, PEP 784

        with _zstd.open(p, "rb") as fh:
            raw = fh.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
    except ImportError:
        lines = None
    except Exception as exc:  # corrupt frame → fall through to CLI, then give up
        _log.warning("zstd stdlib read failed for %s: %s", p, exc)
        lines = None
    if lines is None:
        executable = shutil.which("zstd")
        if not executable:
            _log.warning("cannot read %s: no zstd support (no stdlib compression, "
                         "no zstd CLI on PATH)", p)
            return
        try:
            completed = subprocess.run(
                [executable, "-dc", str(p)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            _log.warning("zstd CLI failed to start for %s: %s", p, exc)
            return
        if completed.returncode != 0:
            _log.warning("zstd decompression failed for %s (rc=%d): %s", p,
                         completed.returncode,
                         (completed.stderr or b"").decode("utf-8", "replace")[:200].strip())
            return
        lines = completed.stdout.decode("utf-8", errors="replace").splitlines()
    for line in lines:
        if len(line) > 10_000_000:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

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
def _grok_home():
    """Grok Build data root — honour GROK_HOME (official CLI), else ~/.grok.

    Evaluated once at import (same pattern as CODEX_HOME / SESSION_DIGGER_DATA_DIR).
    """
    raw = os.environ.get("GROK_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".grok"


GROK_DIR = _grok_home() / "sessions"
GROK_SEARCH_DB = GROK_DIR / "session_search.sqlite"

KIMI_DIR = Path.home() / ".kimi" / "sessions"
# Kimix data home（单一真源）。会话在 KIMIX_DIR / "sessions"；
# detect 用此前缀匹配，adapter 在其下拼 logs/metrics/sessions。
KIMIX_DIR = Path.home() / ".kimix"
KIMI_CODE_DIR = Path.home() / ".kimi-code" / "sessions"

CODEX_DIR = _codex_home()

CURSOR_DIR = Path.home() / ".cursor"

WORKBUDDY_DIR = Path.home() / ".workbuddy"

TRAE_DIR = Path.home() / ".trae-cn"

ZCODE_DIR = Path.home() / ".zcode" / "cli" / "agents"

# ZCode v2 主会话库（agent-config + acp-config 两个子系统共用此根）。
# 布局: v2/<子系统>/claude/<userhash>/projects/<project-slug>/<uuid>.jsonl
ZCODE_V2_DIR = Path.home() / ".zcode" / "v2"

# DSH (DeepSeek) 会话库。布局: ~/.dsh/sessions/<project-slug>/session-<uuid>/
# 内含 zstd 压缩事件流 session.jsonl.zstd（v0）或 session.v2.jsonl.zstd（v2）。
DSH_DIR = Path.home() / ".dsh" / "sessions"

DIM_DIR = Path.home() / ".dim" / "memory"

DIMCODE_DB_PATH = Path.home() / ".dimcode" / "v2" / "dimcode.sqlite"

REASONIX_DIR = Path.home() / ".reasonix" / "sessions"


def _empty_stats(agent_name):
    """Return the standard stats dict with empty values.

    Leaf helper so provider modules never lazy-import ``echolib._adapters``.
    ``model`` is seeded with *agent_name* as a display fallback until the
    adapter fills a real model id.
    """
    return {
        "slug": "", "model": agent_name, "branch": "",
        "started": "", "ended": "",
        "user_messages": 0, "assistant_messages": 0,
        "tool_calls": 0, "files_edited": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "compactions": 0, "summary": "",
        "total_tokens": 0,
        "cache_hit_rate": None,
    }



# Generic OS basenames that must never act as project-scope matchers.
_SCOPE_GENERIC_BASENAMES = frozenset({
    "tmp", "temp", "var", "usr", "home", "users", "private",
    "opt", "bin", "etc", "lib", "src", "dev", "mnt", "root",
    "applications", "library", "system", "volumes", "downloads",
    "documents", "desktop", "movies", "music", "pictures",
})


def compute_cache_hit_rate(input_tokens, cache_read_tokens=0, *, input_includes_cache=None):
    """Compute cache hit rate from token counters.

    Two semantics exist across providers — **adapters should set
    ``input_includes_cache`` explicitly** via ``attach_cache_hit_rates``;
    auto-detect is only a last-resort fallback:

    * **Non-cached input** (Claude Code, Kimi Code ``inputOther``):
      ``input_tokens`` = new tokens not in cache;
      ``cache_read_tokens`` = tokens read from cache.
      Rate = ``cache_read / (input + cache_read)``.
      Pass ``input_includes_cache=False``.

    * **Total input** (Grok billable, ZCode model_usage, DimCode, Codex):
      ``input_tokens`` already includes cached tokens;
      ``cache_read_tokens`` = cached portion (≤ input).
      Rate = ``cache_read / input``.
      Pass ``input_includes_cache=True``.

    Fallback auto-detect (when flag is None): ``cache_read > input`` can only
    happen under non-cached semantics → use additive denominator; otherwise
    assume total-input. Prefer explicit flags to avoid edge-case misclassification
    (e.g. Claude moderate hit rate where cache ≤ uncached input).

    Returns ``None`` when there is no token base (undefined).  Clamps to [0, 1].
    """
    try:
        inp = int(input_tokens or 0)
        cache = int(cache_read_tokens or 0)
    except (TypeError, ValueError):
        return None
    if inp < 0 or cache < 0:
        return None

    if input_includes_cache is None:
        # Last resort: cache_read can exceed input only when input is uncached-only.
        input_includes_cache = cache <= inp

    if input_includes_cache:
        if inp <= 0:
            return None
        rate = cache / float(inp)
    else:
        total = inp + cache
        if total <= 0:
            return None
        rate = cache / float(total)

    if rate < 0:
        return 0.0
    if rate > 1:
        return 1.0
    return round(rate, 4)


def attach_cache_hit_rates(stats, *, input_includes_cache=None, agent=None):
    """Add ``cache_hit_rate`` on session stats and each ``model_usage`` leg.

    Prefer an explicit ``input_includes_cache`` argument or a matching key on
    ``stats`` / each model leg. One flag at the source fixes every downstream
    consumer (index, trend, reflect, aggregates).

    When *input_includes_cache* is omitted, resolve from ``echolib._policy``
    using *agent* or ``stats["agent"]`` so adapters stay aligned with
    ``PROVIDER_POLICY``.
    """
    if not isinstance(stats, dict):
        return stats
    if input_includes_cache is not None:
        stats["input_includes_cache"] = bool(input_includes_cache)
    flag = stats.get("input_includes_cache")
    if flag is None and input_includes_cache is None:
        agent_key = agent or stats.get("agent")
        if agent_key:
            try:
                from echolib._policy import get_token_policy
                pol = get_token_policy(str(agent_key))
                if pol.get("input_includes_cache") is not None:
                    flag = bool(pol["input_includes_cache"])
                    stats["input_includes_cache"] = flag
            except Exception as exc:
                import logging as _logging
                _logging.getLogger(__name__).debug(
                    "policy resolve failed for agent=%s: %s", agent_key, exc
                )
    if flag is not None:
        flag = bool(flag)
    stats["cache_hit_rate"] = compute_cache_hit_rate(
        stats.get("input_tokens"),
        stats.get("cache_read_tokens"),
        input_includes_cache=flag,
    )
    mu = stats.get("model_usage")
    if isinstance(mu, dict):
        for leg in mu.values():
            if isinstance(leg, dict):
                leg_flag = leg.get("input_includes_cache", flag)
                if leg_flag is not None:
                    leg_flag = bool(leg_flag)
                    leg["input_includes_cache"] = leg_flag
                leg["cache_hit_rate"] = compute_cache_hit_rate(
                    leg.get("input_tokens"),
                    leg.get("cache_read_tokens"),
                    input_includes_cache=leg_flag,
                )
    return stats


def cache_rate_eligible(rate, *, drop_zero=True):
    """Whether a session ``cache_hit_rate`` should enter cache rankings.

    Ranking tables must not be polluted by:
    * missing data (``None``)
    * all-zero cache models/sessions (``rate == 0``) — e.g. LongCat on Kimi Code
      reports huge input but never writes ``cache_read``; including them makes a
      whole environment look broken when the real issue is "no cache signal".

    Returns False for those; True only when there is a positive hit rate.
    """
    if rate is None:
        return False
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return False
    if r < 0 or r > 1:
        return False
    if drop_zero and r <= 0:
        return False
    return True


def filter_cache_models(model_stats, min_sessions=1, require_cache=True):
    """Filter model cache stats: exclude all-zero-cache / no-signal models.

    Args:
        model_stats: dict of {model: {"input": int, "cr": int, "sess": int, ...}}
            Also accepts index-style keys: ``rate`` / ``rates`` (list) / ``tok``.
        min_sessions: minimum sessions to include a model
        require_cache: if True, drop models with no observed cache:
            * ``cr == 0`` (and input > 0), or
            * all session rates are 0 / missing

    Returns:
        Filtered dict with same structure.
    """
    result = {}
    for model, d in model_stats.items():
        sess = d.get("sess", d.get("n", 0)) or 0
        if sess < min_sessions:
            continue
        if require_cache:
            cr = d.get("cr", d.get("cache_read", d.get("cache_read_tokens")))
            inp = d.get("input", d.get("input_tokens", 0)) or 0
            rates = d.get("rates")
            if rates is not None:
                if not any(cache_rate_eligible(r) for r in rates):
                    continue
            elif cr is not None:
                if int(cr or 0) == 0 and inp > 0:
                    continue  # zero cache across all sessions
            else:
                rate = d.get("rate", d.get("avg_rate", d.get("cache_hit_rate")))
                if not cache_rate_eligible(rate):
                    continue
        result[model] = d
    return result


# ---------------------------------------------------------------------------
# Cache ranking report contract (human-facing tables)
# ---------------------------------------------------------------------------
# DO:
#   * Main tables = simple session-mean hit rate among eligible sessions only
#   * Put zero/null/unlabeled/small-sample rows in an exclusions appendix
# DO NOT:
#   * Lead with token-weighted hit rate (confuses readers with "the bill")
#   * Mix zero-cache models into environment averages used for ranking
# Full text: references/cache-report-rules.md
CACHE_REPORT_MIN_SESSIONS = 3
CACHE_REPORT_PLACEHOLDER_MODELS = frozenset({
    "", "unknown", "auto", "openai-custom",
    "trae-cn", "trae-cn (summary only)",
}) | environment_model_tokens()

# Internal role tokens (index/code only). Never show these strings to end users.
SESSION_ROLE_MAIN = "main"
SESSION_ROLE_SUBAGENT = "subagent"
SESSION_ROLE_UNKNOWN = "unknown"

# User-facing labels only — reports/tables/chat.
SESSION_ROLE_LABELS = {
    SESSION_ROLE_MAIN: "主对话",
    SESSION_ROLE_SUBAGENT: "子代理",
    SESSION_ROLE_UNKNOWN: "未分类",
}


def session_role_label(role):
    """Map internal role token → Chinese label for display."""
    if not role:
        return SESSION_ROLE_LABELS[SESSION_ROLE_UNKNOWN]
    s = str(role).strip()
    if s in SESSION_ROLE_LABELS:
        return SESSION_ROLE_LABELS[s]
    # Already localized
    if s in SESSION_ROLE_LABELS.values():
        return s
    return SESSION_ROLE_LABELS[SESSION_ROLE_UNKNOWN]


def classify_session_role(agent=None, session_id=None, jsonl_path=None, *, summary=None):
    """Classify main conversation vs subagent (returns internal tokens only).

    Used so multi-agent fan-out is not mixed into the parent thread for rankings.

    **Display:** never put return values or filesystem paths into user tables —
    use ``session_role_label()``. Path/id markers are internal classification
    signals (like reading usage fields), not report columns.

    Returns one of: ``main``, ``subagent``, ``unknown``.
    """
    agent = (agent or "").lower()
    sid = str(session_id or "")
    path = str(jsonl_path or "").replace("\\", "/")
    raw = sid.split(":", 1)[-1] if ":" in sid else sid

    # --- path markers ---
    if "/subagents/" in path or path.rstrip("/").endswith("/subagents"):
        return SESSION_ROLE_SUBAGENT
    if "/agents/" in path and "/agents/main/" not in path:
        # Kimi Code: agents/agent-0/wire.jsonl
        if path.endswith("wire.jsonl") or "/wire.jsonl" in path:
            return SESSION_ROLE_SUBAGENT

    # --- id prefixes ---
    if "subagent" in raw.lower() or raw.startswith("subagent_"):
        return SESSION_ROLE_SUBAGENT
    if agent == "dimcode":
        if raw.startswith("sess_"):
            return SESSION_ROLE_MAIN
        if raw.startswith("subagent_"):
            return SESSION_ROLE_SUBAGENT

    # --- Grok summary ---
    if isinstance(summary, dict):
        kind = str(summary.get("session_kind") or summary.get("kind") or "").lower()
        if kind == "subagent":
            return SESSION_ROLE_SUBAGENT

    # --- ZCode ---
    if "sess_subagent" in path or "sess_subagent" in raw:
        return SESSION_ROLE_SUBAGENT
    if agent == "zcode" and "agent_" in path:
        # Default zcode transcript under sess_*/agent_* without parent markers
        # is often the primary agent for that sess — leave unknown unless
        # parentSessionId was folded into path/id (handled above).
        pass

    # Claude main jsonl is project/<uuid>.jsonl (no subagents in path)
    if agent == "claude" and path.endswith(".jsonl") and "/subagents/" not in path:
        return SESSION_ROLE_MAIN

    # Kimix events.jsonl without /subagents/ → main session
    if agent == "kimix" and "events.jsonl" in path and "/subagents/" not in path:
        return SESSION_ROLE_MAIN

    # Grok chat_history without subagent kind → treat as main/standalone
    if agent == "grok" and path.endswith("chat_history.jsonl"):
        return SESSION_ROLE_MAIN

    # Kimi Code main wire
    if agent in ("kimi_code", "kimi") and "/agents/main/" in path:
        return SESSION_ROLE_MAIN

    # ZCode main session
    if agent == "zcode" and "sess_" in raw and "subagent" not in raw:
        return SESSION_ROLE_MAIN

    if agent == "dimcode" and raw.startswith("sess_"):
        return SESSION_ROLE_MAIN

    return SESSION_ROLE_UNKNOWN


def mean_cache_hit_rate(rates, *, drop_zero=True):
    """Simple mean of eligible session hit rates. No token weighting.

    Returns ``None`` when fewer than one eligible rate remains.
    """
    elig = [float(r) for r in rates if cache_rate_eligible(r, drop_zero=drop_zero)]
    if not elig:
        return None
    return round(sum(elig) / len(elig), 4)


def classify_cache_session(rate, model=None, *, min_sessions_context=None):
    """Classify one session for cache tables.

    Returns:
        ("eligible", rate) or ("exclude", reason_str)
    """
    if rate is None:
        return "exclude", "无命中率字段"
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return "exclude", "无命中率字段"
    if r <= 0:
        return "exclude", "命中率=0(无缓存信号)"
    if not cache_rate_eligible(r):
        return "exclude", "命中率无效"
    m = (model or "").strip()
    if not m or m.lower() in CACHE_REPORT_PLACEHOLDER_MODELS:
        # Still eligible for *environment* averages; model tables drop these.
        return "eligible_unlabeled", r
    return "eligible", r


def build_cache_hit_tables(rows, *, min_sessions=CACHE_REPORT_MIN_SESSIONS,
                           split_role=False, role_filter=None,
                           enforce_usage_tier=True):
    """Build main + exclusion tables from index-like rows.

    Args:
        rows: iterable of dicts or tuples with keys/fields
            ``agent``, ``model``, ``cache_hit_rate``, optional ``total_tokens``,
            optional ``session_role`` / ``id`` / ``jsonl_path`` (for role split)
        min_sessions: minimum eligible labeled sessions for model×env main table
        split_role: if True, also emit role-split tables with **Chinese** role labels
        role_filter: ``main`` / ``subagent`` or 中文「主对话」/「子代理」；只保留该角色
        enforce_usage_tier: if True, agents below usage tier (T2+) are excluded
            from main tables with reason「能力层不足」 (honest half-adapter gate)

    Returns:
        User-facing role field is always Chinese (``角色``), never raw ``main``.
        Internal path/id used only for classification, never emitted in rows.
    """
    from collections import defaultdict

    try:
        from echolib._policy import tier_supports
    except Exception:
        def tier_supports(agent, capability):  # type: ignore
            return True

    def _get(row, key, default=None):
        if isinstance(row, dict):
            return row.get(key, default)
        # tuple order: agent, model, cache_hit_rate, total_tokens
        idx = {"agent": 0, "model": 1, "cache_hit_rate": 2, "total_tokens": 3}
        if key not in idx:
            return default
        try:
            return row[idx[key]]
        except (IndexError, TypeError):
            return default

    def _normalize_role_filter(rf):
        if rf is None:
            return None
        s = str(rf).strip()
        rev = {v: k for k, v in SESSION_ROLE_LABELS.items()}
        if s in rev:
            return rev[s]
        if s in (SESSION_ROLE_MAIN, SESSION_ROLE_SUBAGENT):
            return s
        return None

    role_filter = _normalize_role_filter(role_filter)

    def _role_of(row):
        role = _get(row, "session_role")
        if role in (SESSION_ROLE_MAIN, SESSION_ROLE_SUBAGENT, SESSION_ROLE_UNKNOWN):
            # Accept Chinese labels if a caller already localized
            if role in SESSION_ROLE_LABELS.values():
                rev = {v: k for k, v in SESSION_ROLE_LABELS.items()}
                return rev.get(role, SESSION_ROLE_UNKNOWN)
            return role
        return classify_session_role(
            _get(row, "agent"),
            _get(row, "id") or _get(row, "session_id"),
            _get(row, "jsonl_path") or _get(row, "path"),
        )

    agent_rates = defaultdict(list)
    agent_all = defaultdict(int)
    agent_tok = defaultdict(int)
    model_rates = defaultdict(list)  # (model, agent) labeled only
    model_tok = defaultdict(int)
    excl = defaultdict(lambda: {"n": 0, "tok": 0})
    # role-split buckets: (agent, role) -> rates
    role_rates = defaultdict(list)
    role_all = defaultdict(int)
    role_tok = defaultdict(int)
    model_role_rates = defaultdict(list)
    model_role_tok = defaultdict(int)

    global_rates = []

    for row in rows:
        agent = _get(row, "agent") or "?"
        model = _get(row, "model")
        rate = _get(row, "cache_hit_rate")
        tok = int(_get(row, "total_tokens") or 0)
        role = _role_of(row)
        agent_all[agent] += 1
        agent_tok[agent] += tok
        role_all[(agent, role)] += 1
        role_tok[(agent, role)] += tok

        # Honest half-adapter gate: thin/probe adapters never pad main usage tables.
        if enforce_usage_tier and not tier_supports(agent, "usage"):
            label = (model or "").strip() or "(empty)"
            key = (agent, label, "能力层不足(非账单级token)")
            excl[key]["n"] += 1
            excl[key]["tok"] += tok
            continue

        if role_filter in (SESSION_ROLE_MAIN, SESSION_ROLE_SUBAGENT) and role != role_filter:
            # Exclusion reasons are user-facing: Chinese role only, no paths/tokens.
            excl[(agent, (model or "").strip() or "(empty)",
                  f"非{session_role_label(role_filter)}")]["n"] += 1
            excl[(agent, (model or "").strip() or "(empty)",
                  f"非{session_role_label(role_filter)}")]["tok"] += tok
            # still collect role buckets for split tables
            if cache_rate_eligible(rate):
                role_rates[(agent, role)].append(float(rate))
            continue

        kind, payload = classify_cache_session(rate, model)
        if kind == "exclude":
            label = (model or "").strip() or "(empty)"
            key = (agent, label, payload)
            excl[key]["n"] += 1
            excl[key]["tok"] += tok
            continue

        r = float(payload)
        agent_rates[agent].append(r)
        global_rates.append(r)
        role_rates[(agent, role)].append(r)

        if kind == "eligible_unlabeled":
            key = (agent, "(未标注)", "模型未标注")
            excl[key]["n"] += 1
            excl[key]["tok"] += tok
            continue

        m = (model or "").strip()
        model_rates[(m, agent)].append(r)
        model_tok[(m, agent)] += tok
        model_role_rates[(m, agent, role)].append(r)
        model_role_tok[(m, agent, role)] += tok

    by_agent = []
    for agent, rates in sorted(agent_rates.items(), key=lambda x: -len(x[1])):
        by_agent.append({
            "agent": agent,
            "n_eligible": len(rates),
            "n_all": agent_all[agent],
            "cache_hit_rate": mean_cache_hit_rate(rates),
            "total_tokens": agent_tok[agent],
        })

    by_model_env = []
    for (model, agent), rates in model_rates.items():
        if len(rates) < min_sessions:
            excl[(agent, model, f"有效会话<{min_sessions}")]["n"] = len(rates)
            excl[(agent, model, f"有效会话<{min_sessions}")]["tok"] = model_tok[(model, agent)]
            continue
        by_model_env.append({
            "model": model,
            "agent": agent,
            "n_eligible": len(rates),
            "cache_hit_rate": mean_cache_hit_rate(rates),
            "total_tokens": model_tok[(model, agent)],
        })
    by_model_env.sort(key=lambda x: (-(x["cache_hit_rate"] or 0), -x["n_eligible"]))

    exclusions = [
        {
            "agent": a,
            "model": m,
            "reason": reason,
            "n": v["n"],
            "total_tokens": v["tok"],
        }
        for (a, m, reason), v in excl.items()
        if v["n"] > 0
    ]
    exclusions.sort(key=lambda x: -x["total_tokens"])

    out = {
        "by_agent": by_agent,
        "by_model_env": by_model_env,
        "exclusions": exclusions,
        "global": {
            "n_eligible": len(global_rates),
            "cache_hit_rate": mean_cache_hit_rate(global_rates),
        },
        "rules": (
            "会话均命中率(rate>0)；不默认加权；"
            "零缓存/未标注/样本过少→排除；"
            "可选按角色分列：主对话/子代理（不对外暴露路径与内部标记）"
        ),
    }

    if split_role:
        by_agent_role = []
        for (agent, role), rates in sorted(
            role_rates.items(), key=lambda x: (x[0][0], x[0][1])
        ):
            if not rates:
                continue
            # Skip "未分类" in default split tables — not a product concept for users
            if role == SESSION_ROLE_UNKNOWN:
                continue
            by_agent_role.append({
                "环境": agent,
                "角色": session_role_label(role),
                "有效会话": len(rates),
                "总会话": role_all[(agent, role)],
                "缓存命中率": mean_cache_hit_rate(rates),
                # keep english keys for code callers, Chinese for display
                "agent": agent,
                "role": session_role_label(role),
                "n_eligible": len(rates),
                "n_all": role_all[(agent, role)],
                "cache_hit_rate": mean_cache_hit_rate(rates),
                "total_tokens": role_tok[(agent, role)],
            })
        by_model_role = []
        for (model, agent, role), rates in model_role_rates.items():
            if len(rates) < min_sessions:
                continue
            if role == SESSION_ROLE_UNKNOWN:
                continue
            by_model_role.append({
                "环境": agent,
                "角色": session_role_label(role),
                "模型": model,
                "有效会话": len(rates),
                "缓存命中率": mean_cache_hit_rate(rates),
                "agent": agent,
                "role": session_role_label(role),
                "model": model,
                "n_eligible": len(rates),
                "cache_hit_rate": mean_cache_hit_rate(rates),
                "total_tokens": model_role_tok[(model, agent, role)],
            })
        by_model_role.sort(
            key=lambda x: (x["agent"], x["role"], -(x["cache_hit_rate"] or 0))
        )
        out["by_agent_role"] = by_agent_role
        out["by_model_env_role"] = by_model_role

    return out


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
            # Prefer well-known transcript locations across agents.
            for candidate in (
                p / "chat_history.jsonl",          # Grok
                p / "agents" / "main" / "wire.jsonl",  # Kimi Code
                p / "wire.jsonl",                  # older Kimi
                p / "transcript.jsonl",            # ZCode / generic
                p / "conversation.jsonl",
            ):
                if candidate.is_file():
                    return str(candidate)
            jsonls = sorted(p.glob("*.jsonl"))
            # Prefer non-events sidecars when several jsonl files exist.
            preferred = [j for j in jsonls if j.name not in {"events.jsonl", "updates.jsonl"}]
            if len(preferred) == 1:
                return str(preferred[0])
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




def aggregate_cache_by_session(records):
    """Aggregate per-API-call cache records into per-session averages.

    Corrects the issue where API call counts were reported as session counts.
    Also classifies each session as main conversation or sub-agent.

    Args:
        records: iterable of dicts with keys:
            session_id (or id), model, cache_hit_rate, agent, jsonl_path, ...

    Returns:
        dict: {session_id: {'model': str, 'rate': float, 'role': str, 'calls': int}}
    """
    sessions = {}
    for r in records:
        sid = r.get('session_id') or r.get('id') or 'unknown'
        if sid not in sessions:
            sessions[sid] = {
                'model': r.get('model', 'unknown'),
                'rates': [],
                'agent': r.get('agent'),
                'jsonl_path': r.get('jsonl_path', ''),
            }
        rate = r.get('cache_hit_rate')
        if rate is not None:
            try:
                sessions[sid]['rates'].append(float(rate))
            except (TypeError, ValueError):
                pass

    # Compute per-session average and classify role
    for sid, data in sessions.items():
        if data['rates']:
            data['rate'] = round(sum(data['rates']) / len(data['rates']), 4)
            data['calls'] = len(data['rates'])
        else:
            data['rate'] = None
            data['calls'] = 0
        # Classify main vs subagent
        data['role'] = classify_session_role(
            agent=data.get('agent'),
            session_id=sid,
            jsonl_path=data.get('jsonl_path'),
        )
        del data['rates']  # Clean up intermediate data

    return sessions
