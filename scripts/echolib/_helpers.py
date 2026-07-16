import json
import os
import re
import shutil
import subprocess
import urllib.parse
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


def attach_cache_hit_rates(stats, *, input_includes_cache=None):
    """Add ``cache_hit_rate`` on session stats and each ``model_usage`` leg.

    Prefer an explicit ``input_includes_cache`` argument or a matching key on
    ``stats`` / each model leg. One flag at the source fixes every downstream
    consumer (index, trend, reflect, aggregates).
    """
    if not isinstance(stats, dict):
        return stats
    if input_includes_cache is not None:
        stats["input_includes_cache"] = bool(input_includes_cache)
    flag = stats.get("input_includes_cache")
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
    "", "unknown", "claude", "codex", "kimi", "zcode", "dimcode", "grok",
    "openai-custom", "workbuddy", "universal", "trae-cn", "trae_cn",
    "auto",
})


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


def build_cache_hit_tables(rows, *, min_sessions=CACHE_REPORT_MIN_SESSIONS):
    """Build main + exclusion tables from index-like rows.

    Args:
        rows: iterable of dicts or tuples with keys/fields
            ``agent``, ``model``, ``cache_hit_rate``, optional ``total_tokens``
        min_sessions: minimum eligible labeled sessions for model×env main table

    Returns:
        {
          "by_agent": [{agent, n_eligible, n_all, cache_hit_rate, ...}],
          "by_model_env": [{model, agent, n_eligible, cache_hit_rate, ...}],
          "exclusions": [{agent, model, reason, n, total_tokens}],
          "global": {n_eligible, cache_hit_rate},
          "rules": short contract string,
        }
    """
    from collections import defaultdict

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

    agent_rates = defaultdict(list)
    agent_all = defaultdict(int)
    agent_tok = defaultdict(int)
    model_rates = defaultdict(list)  # (model, agent) labeled only
    model_tok = defaultdict(int)
    excl = defaultdict(lambda: {"n": 0, "tok": 0})

    global_rates = []

    for row in rows:
        agent = _get(row, "agent") or "?"
        model = _get(row, "model")
        rate = _get(row, "cache_hit_rate")
        tok = int(_get(row, "total_tokens") or 0)
        agent_all[agent] += 1
        agent_tok[agent] += tok

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

        if kind == "eligible_unlabeled":
            key = (agent, "(未标注)", "模型未标注")
            excl[key]["n"] += 1
            excl[key]["tok"] += tok
            continue

        m = (model or "").strip()
        model_rates[(m, agent)].append(r)
        model_tok[(m, agent)] += tok

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

    return {
        "by_agent": by_agent,
        "by_model_env": by_model_env,
        "exclusions": exclusions,
        "global": {
            "n_eligible": len(global_rates),
            "cache_hit_rate": mean_cache_hit_rate(global_rates),
        },
        "rules": (
            "main=session-mean of rate>0 only; no token-weighted rate; "
            "zeros/null/unlabeled/small-n → exclusions"
        ),
    }


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


