#!/usr/bin/env python3
"""
sd-recall.py — Unified recall engine. Single-process, zero subprocess spawns.

Replaces the old subprocess chain of multiple bash scripts with one Python process.

Search strategy (fastest first):
  1. SQLite FTS5 index (if built) → <50ms for keyword search
  2. File scan fallback (index missing) → parse JSONL on the fly

Usage:
  sd-recall.py search <keyword> [--scope current|all] [--limit N] [--decisions] [--deep]
  sd-recall.py sessions [--scope current|all] [--limit N]
  sd-recall.py stats

v1.0: Core speed tier for session-digger v0.7.
     Typical: 5-10x faster than bash pipeline, 1 Python process vs 5+.
"""

import argparse
import json
import logging as _log
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import echolib

from index_builder._schema import DB_PATH  # 单一真源：~/.claude/.session-digger/index.db 或 $SESSION_DIGGER_DATA_DIR
from index_builder._cjk import build_match_query
from index_builder._evidence import DECISION_PATTERNS, evidence_from_row, projection_available
from index_builder._reader import (  # canonical index read layer
    quick_stats_from_index as _quick_stats_from_index,
    schema_status as _schema_status,
    session_stats_by_path as _session_stats_by_path,
)

# CLI aliases → adapter registry names (registry is the single source of truth)
_AGENT_MAP = {
    "claude": "claude",
    "grok": "grok",
    "kimi": "kimi_code",
    "kimi_code": "kimi_code",
    "codex": "codex",
    "cursor": "cursor",
    "zcode": "zcode",
    "workbuddy": "workbuddy",
    "trae": "trae_cn",
    "trae_cn": "trae_cn",
    "dim": "dim",
    "dimcode": "dimcode",
    "reasonix": "reasonix",
    "universal": "universal",
    "cross": "cross",
    "all": "cross",
    # recall-lite.sh 等调用方的默认值是 "auto"（自动=跨代理）；不在这里做别名，
    # argparse 会直接拒掉整条 lite 召回链（实测 --scope all 时 100% 失败）。
    "auto": "cross",
}


def _cli_agent_choices():
    """Dynamic --agent choices from registry + aliases (never hardcode a closed set)."""
    names = set(_AGENT_MAP.keys()) | set(echolib.ADAPTER_REGISTRY.keys())
    names.add("cross")
    names.add("all")
    return sorted(names)


def _resolve_cli_agent(agent):
    """Map CLI agent token to registry name or 'cross'."""
    if not agent or agent in ("cross", "all"):
        return "cross"
    return _AGENT_MAP.get(agent, agent)

# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def _session_filter(scope, agent):
    """Predicate for "may this query return this session at all".

    It has to be part of the *selection*, not a sweep over its output. The FTS
    path ranks by BM25 and keeps a page of ``limit`` rows, so a filter applied
    afterwards discards that page and reports the survivors — which is how a
    term with plenty of in-project history reads as "no match". Measured
    2026-09-17 in this project: ``记忆`` has 51 in-project sessions and **none**
    of them in the top 20, and ``agent`` has 174 with none in the top 20, so
    ``--scope current`` (the documented default) answered empty for both while
    ``--scope all`` returned 584 and 3567. The agent narrow has the same shape:
    it could return fewer than ``limit`` rows even with more available.

    Returns ``None`` — meaning "no rule to apply" — when the scope is not
    ``current`` and the agent is not narrowed, so callers can tell "no filter"
    apart from "a filter that happens to pass everything".
    """
    cwd = os.getcwd() if scope == "current" else None
    reg = _resolve_cli_agent(agent)
    if cwd is None and reg == "cross":
        # Nothing to filter on. This has to be ``None`` rather than an
        # always-true predicate: ``_fts_search`` widens its window to the whole
        # ranked set whenever a filter is present, and a no-op filter paid that
        # for nothing — measured 2026-09-17 on the live index, 71.8 ms against
        # 24.9 ms for ``agent`` (15,210 ranked rows), identical answer. The
        # common ``--scope all`` (the default is ``cross``) path is exactly
        # this case, so the always-true predicate also silently falsified
        # ``_fts_search``'s promise that the unfiltered path keeps ``limit * 5``.
        return None

    def keep(entry):
        if cwd is not None and not _session_in_cwd(entry, cwd):
            return False
        return reg == "cross" or entry[2] in (reg, agent)

    return keep


def find_sessions(scope="current", limit=50, keyword=None, agent="cross", tag=None,
                  outcome=None, include_tools=False):
    """Find session JSONL files. Returns list of (session_id, path, agent).

    Discovery is fully registry-driven (``ADAPTER_REGISTRY`` / ``cross_tool_list_sessions``).
    New adapters appear here automatically — no per-env path scan in this file.
    Explicit *tag* / *outcome* keep the function free of global argparse state.
    """
    del tag, outcome  # reserved for future FTS filters; explicit > globals

    keep = _session_filter(scope, agent)

    # Keyword filter: FTS first (paths from index — no filesystem scan).
    # The window stays wider than ``limit`` because BM25 returns *rows*, and one
    # session contributes many of them — a window of ``limit`` rows can carry
    # fewer than ``limit`` distinct sessions.
    if keyword:
        fts_results = _fts_search(keyword, limit=max(limit * 3, 20),
                                  include_tools=include_tools, keep=keep)
        if fts_results is not None:
            return fts_results[:limit]

    entries = _list_from_registry(agent=agent, limit=limit, keyword=keyword or "", scope=scope)
    if keep is not None:
        entries = [e for e in entries if keep(e)]

    # Keyword fallback: file scan when FTS miss / index absent.
    # stream_contains keeps the 50KB head fast path but streams the rest
    # (bounded per file) — evidence beyond the head window was a measured
    # recall-0 hole (corpus s2-long, tests/test_quality_baseline.py).
    # collapse_near_dups folds byte-identical copies only, keeping the first in
    # candidate order. It deliberately does NOT prefer the longest file: the
    # keep-longest heuristic was measured on the dup corpus and rejected, since
    # the evidence-bearing original there is *smaller* than its padding copies.
    # Pseudo-duplicates (same prefix, each with its own tail) are not folded.
    if keyword:
        from retrieval_utils import collapse_near_dups, stream_contains

        matched = []
        for sid, path, at in entries:
            if stream_contains(path, keyword):
                matched.append((sid, path, at))
                if len(matched) >= limit:
                    break
        kept, _collapsed = collapse_near_dups(matched)
        return kept[:limit]

    return echolib.cap(entries, limit)


def _list_from_registry(agent="cross", limit=50, keyword="", scope="all"):
    """List sessions via echolib adapters; return (session_id, path, agent_id)."""
    reg = _resolve_cli_agent(agent)
    fetch_n = max(limit * 5, 40)
    cwd = os.getcwd() if scope == "current" else None
    rows = []

    if reg == "cross":
        try:
            # 这里的调用者只要 (sid, path, agent)——cross_tool 返回的
            # summary/first_prompt/msg_count 全部被丢弃。keyword 为空时适配器
            # 本来也不做关键词过滤，所以关掉 preview 是可证中性的；带 keyword
            # 时保留 preview，因为适配器的关键词分支读的就是它。
            if keyword:
                rows = echolib.cross_tool_list_sessions(limit=fetch_n, keyword=keyword)
            else:
                with echolib.discovery_only():
                    rows = echolib.cross_tool_list_sessions(limit=fetch_n, keyword=keyword)
        except Exception:
            rows = []
    else:
        adapter = echolib.ADAPTER_REGISTRY.get(reg)
        if not adapter:
            return []
        fn = adapter.get("list_sessions")
        if not fn:
            return []
        try:
            if cwd is not None:
                try:
                    rows = fn(cwd=cwd, limit=fetch_n, keyword=keyword)
                except TypeError:
                    rows = fn(limit=fetch_n, keyword=keyword)
            else:
                try:
                    rows = fn(limit=fetch_n, keyword=keyword)
                except TypeError:
                    rows = fn(limit=fetch_n)
        except Exception:
            rows = []

    entries = []
    seen = set()
    for s in rows or []:
        if isinstance(s, dict):
            sid = s.get("session_id") or s.get("id") or ""
            path = s.get("full_path") or s.get("path") or ""
            agent_id = s.get("agent") or reg
        else:
            sid = getattr(s, "session_id", "") or ""
            path = getattr(s, "full_path", "") or ""
            agent_id = reg if reg != "cross" else (getattr(s, "agent", None) or "unknown")

        path = echolib.normalize_session_path(path)
        if not path or "://" in str(path):
            # Virtual schemes (dimcode://) are not file-backed for recall CLI.
            continue
        try:
            key = str(Path(path).resolve()) if Path(path).exists() else str(path)
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not sid:
            sid = Path(path).stem
        # Prefer registry id over display strings.
        if agent_id not in echolib.ADAPTER_REGISTRY and reg != "cross":
            agent_id = reg
        entries.append((sid, path, agent_id))

    # Preserve cross_tool fair round-robin order (do not re-sort by mtime —
    # that re-hides quieter environments under one hot agent).
    if reg != "cross":
        def _mtime(item):
            p = item[1]
            try:
                return os.path.getmtime(p) if os.path.exists(p) else 0
            except OSError:
                return 0

        entries.sort(key=_mtime, reverse=True)
    return entries


def _session_in_cwd(entry, cwd):
    """Delegate to shared echolib.session_in_cwd (one rule for every agent)."""
    _sid, path, agent = entry
    return echolib.session_in_cwd(path, cwd, agent=agent)


def _ts19(ts):
    """Timestamp → first 19 chars, tolerant of None/empty."""
    return ts[:19] if isinstance(ts, str) and ts else "?"




def _fts_search(keyword, limit=10, include_tools=False, keep=None):
    """FTS5 search. Returns list of (session_id, jsonl_path, agent) tuples.

    Semantics: None = index missing or query failed (caller may fall back to
    file scan); [] = index answered, genuinely no hits (never fall back —
    a rescan of file heads cannot find what the full-text index did not).

    Resolves paths directly from the sessions table (jsonl_path column),
    avoiding the need for a full file-system scan to build a path_map.

    Tool result digests (facet TOOL) are excluded unless ``include_tools``:
    measured 2026-09-17 they recovered 25 of 250 tool-only needles (10%) while
    taking 35% of the top-20 page for a term that appears in command output.

    ``keep`` is a predicate the *selection* must satisfy (see
    :func:`_session_filter`). When one is supplied the window widens to the whole
    ranked set, because a page chosen by BM25 and then filtered returns a
    silently short answer — the ranking knows nothing about the predicate. The
    unfiltered path keeps the cheap ``limit * 5`` window.
    """
    if not DB_PATH.exists():
        return None
    match_q = build_match_query(keyword)
    if not match_q:
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        # 重建窗口闸门：rebuild 的 DELETE→灌入之间索引是空的，此时返回 []
        # 会被当成「真没有」——读 index_meta 标记，重建中则响亮回退文件扫描
        # （契约：None=调用方应回退，[]=索引确认真空）。缺 index_meta 的老
        # 索引当无标记处理，不改变原有失败语义。
        try:
            rebuilding = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'rebuild_in_progress'"
            ).fetchone()
        except sqlite3.OperationalError:
            rebuilding = None
        if rebuilding:
            conn.close()
            print(f"[sd-recall] index rebuild in progress; results would be "
                  f"partial — falling back to file scan (rebuild agent scope)",
                  file=sys.stderr)
            return None
        # FTS5 returns message-level rows; join to sessions for path/agent.
        # Get distinct session_ids in BM25 score order, then resolve paths.
        rows = conn.execute(f"""
            SELECT m.session_id, s.jsonl_path, s.agent
            FROM messages_fts m
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
              {"AND m.role != 'TOOL'" if not include_tools else ""}
            ORDER BY bm25(messages_fts)
            LIMIT ?
        """, (match_q, -1 if keep is not None else limit * 5)).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        # Fail loud: a silent fallback here looks like "no results" to the
        # user, indistinguishable from a genuinely empty index.
        print(f"[sd-recall] FTS query failed ({exc}); falling back to file scan. "
              f"Rebuild the index if this persists: index-builder.py build --rebuild",
              file=sys.stderr)
        return None
    # Deduplicate by session_id, preserving score order
    seen = set()
    results = []
    for sid, path, agent in rows:
        if sid in seen:
            continue
        seen.add(sid)
        if not path:
            continue
        entry = (sid, path, agent or "claude")
        if keep is not None and not keep(entry):
            continue
        results.append(entry)
        if len(results) >= limit:
            break
    return results


def extract_evidence(session_path, decisions=False, deep=False, limit_msgs=15):
    """
    Single-pass evidence extraction from a session.
    Returns dict with user_messages, tool_errors, decisions(optional).
    Uses adapter registry dispatch for multi-environment support.
    """
    result = {
        "user_messages": [],
        "tool_errors": [],
        "decisions": [] if decisions else None,
    }

    import re
    decision_res = [re.compile(p) for p in DECISION_PATTERNS] if decisions else []

    try:
        msg_count = 0
        for msg in echolib.dispatch_extract_messages(session_path, role="both"):
            entry = {"role": msg.get("role"), "timestamp": msg.get("timestamp"), "text": msg.get("text", "")[:300]}

            if msg.get("role") == "USER":
                result["user_messages"].append(entry)
                msg_count += 1
                if msg_count >= limit_msgs:
                    break

                if decisions:
                    for pat in decision_res:
                        if pat.search(msg.get("text", "")):
                            result["decisions"].append(entry)
                            break
    except Exception as exc:
        _log.warning("extract_evidence decisions failed for %s: %s", session_path, exc)

    # Tool errors via dispatch
    try:
        for t in echolib.dispatch_extract_tools(session_path, errors_only=True, limit=20):
            result["tool_errors"].append(t)
    except Exception as exc:
        _log.warning("extract_evidence tool_errors failed for %s: %s", session_path, exc)

    # Deep mode: full excerpt
    if deep:
        try:
            all_msgs = list(echolib.dispatch_extract_messages(session_path, role="both", limit=30))
            result["full_excerpt"] = all_msgs
        except Exception as exc:
            _log.warning("extract_evidence full_excerpt failed for %s: %s", session_path, exc)

    return result


def _index_status_line():
    """One-line index state, naming what is missing rather than just HIT/MISS.

    A readable index is not necessarily a *current* one: rows written before the
    evidence projection existed carry ``user_evidence_json = ''`` and force the
    file-scan fallback, so search is correct but slower until one build runs.
    Saying so turns a silent quality/speed regression into an instruction.
    """
    if not DB_PATH.exists():
        return "MISS (run: index-builder.py build)"
    stored, code = _schema_status()
    if stored is not None and stored != code:
        return (f"HIT, schema v{stored} < code v{code}"
                f" — evidence falls back to file scan (run: index-builder.py build)")
    return "HIT"


def _facet_counts(keyword):
    """Hit rows per evidence facet for ``keyword``, restricted to reachable rows.

    Joined to ``sessions`` on purpose. ``_fts_search`` resolves a hit's path
    through that join, so an FTS row whose session is gone can never be
    returned by any scope — counting it here would tell the user to widen a
    search that widening cannot fix. Measured 2026-09-17 before the orphan pass
    cleaned up: 4,542 such rows, which is the whole difference between "the
    index has it, try a wider scope" and "it is not there".
    """
    match_q = build_match_query(keyword)
    if not match_q:
        return {}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            return dict(conn.execute("""
                SELECT f.role, COUNT(*) FROM messages_fts f
                JOIN sessions s ON s.id = f.session_id
                WHERE messages_fts MATCH ?
                GROUP BY f.role
            """, (match_q,)).fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def report_empty_search(keyword, scope):
    """An empty result must read as a state, never as "it never happened".

    A scope filter can empty a result set the index would happily answer:
    measured on the live index 2026-09-17, ``第一性原理`` has 168 hit rows while
    ``--scope current`` returns none, and the old one-liner printed only
    "No matching sessions found" — indistinguishable from a genuine absence.
    Report where the search actually looked, whether the index is current, and
    what a wider scope (or the tool facet) would return, so the next move is a
    command instead of a guess.
    """
    print(f"No match for '{keyword}' in scope '{scope}'.")
    print(f"  Index: {_index_status_line()}")
    if not build_match_query(keyword):
        # Nothing was ever asked. The tokenizer drops punctuation, whitespace
        # and FTS5 operators, so ``!!!`` produces an empty MATCH expression and
        # ``_facet_counts`` answers 0 for a reason that has nothing to do with
        # the index. Reporting the generic "no row in any scope" sent the reader
        # looking for missing data (narrow the term / check it was indexed) when
        # the only fix is to type something with letters or CJK in it.
        print("  This term has no searchable content (punctuation or whitespace "
              "only) — try a word.")
        return
    counts = _facet_counts(keyword)
    total = sum(counts.values())
    if not total:
        print("  The index has no row for this term in any scope — narrow the "
              "term or check that the session was indexed after it was written.")
        return
    where = ", ".join(f"{facet}={n}" for facet, n in sorted(counts.items()))
    print(f"  Index does hold {total} row(s) elsewhere ({where}).")
    if scope != "all":
        print("  Widen: rerun with --scope all")
    if counts.get("TOOL"):
        print(f"  {counts['TOOL']} of them are tool result digests, not prose "
              f"— read them with: index-builder.py search '{keyword}' --tools-only")


def cmd_search(args):
    """Main search command."""
    t_start = time.time()

    sessions = find_sessions(
        scope=args.scope,
        limit=args.limit,
        keyword=args.keyword,
        agent=args.agent,
        include_tools=getattr(args, "include_tools", False),
    )

    if not sessions:
        report_empty_search(args.keyword, args.scope)
        return

    # Header
    mode_flags = []
    if args.decisions:
        mode_flags.append("decisions")
    if args.deep:
        mode_flags.append("deep")
    mode_str = ",".join(mode_flags) if mode_flags else "standard"

    print(f"=== sd-recall: query='{args.keyword}' scope={args.scope} limit={args.limit} mode={mode_str} ===")
    print(f"  Found {len(sessions)} session(s). Index: {_index_status_line()}")
    print()

    # One batched index read for all hits; per-session file parsing only for
    # sessions the index does not know (or deep mode).
    index_stats = _quick_stats_from_index([sid for sid, _p, _a in sessions])

    for i, (sid, path, agent) in enumerate(sessions, 1):
        try:
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
        except OSError:
            mtime = "?"

        quick_stats = {}
        cached = index_stats.get(sid)
        if cached is not None:
            quick_stats = {
                "msgs": cached["msgs"],
                "tools": cached["tools"],
                "errors": cached["errors"],
                "branch": cached["branch"],
            }
        else:
            try:
                s = echolib.dispatch_session_stats(path)
                quick_stats = {
                    "msgs": s.get("user_messages", 0),
                    "tools": s.get("tool_calls", 0),
                    "errors": s.get("errors", 0),
                    "branch": s.get("branch", ""),
                }
            except Exception as exc:
                _log.warning("quick_stats failed for %s: %s", path, exc)

        print(f"--- [{i}/{len(sessions)}] {sid} ({agent}, {mtime}) ---")
        if quick_stats:
            print(f"  Messages: {quick_stats.get('msgs', '?')} | Tools: {quick_stats.get('tools', '?')} | Errors: {quick_stats.get('errors', '?')} | Branch: {quick_stats.get('branch', '')}")
        if cached is not None:
            # Summary/first_prompt are precomputed — surface for relevance.
            snippet = (cached["summary"] or cached["first_prompt"])[:150]
            if snippet:
                print(f"  Summary: {snippet}".replace("\n", " "))

        if cached is not None and not args.deep and projection_available(cached):
            # Evidence rides in the batched row read above — no per-session
            # query. Reading it from messages_fts instead was a full scan of
            # the FTS content table (85% of a 20-result search).
            #
            # An index row is not the same thing as a computed projection: the
            # column defaults to '' and only a build populates it. Gating on
            # "row exists" alone rendered zero user messages (and an empty
            # --decisions list) for every row the build had not revisited.
            evidence = evidence_from_row(cached, decisions=args.decisions)
        else:
            evidence = extract_evidence(path, decisions=args.decisions, deep=args.deep)

        if evidence["user_messages"]:
            print(f"\n  User messages ({len(evidence['user_messages'])}):")
            for m in evidence["user_messages"][:8]:
                ts = _ts19(m.get("timestamp"))
                text = m["text"][:150].replace("\n", " ")
                print(f"    [{ts}] {text}")

        if evidence["tool_errors"]:
            print(f"\n  Tool errors ({len(evidence['tool_errors'])}):")
            for t in evidence["tool_errors"][:5]:
                print(f"    [{_ts19(t.get('timestamp'))}] {t['name']}: {t['result_preview'][:80]}")

        if args.decisions and evidence.get("decisions"):
            print(f"\n  Decision points ({len(evidence['decisions'])}):")
            for d in evidence["decisions"][:5]:
                ts = _ts19(d.get("timestamp"))
                role = d.get("role", "?")
                print(f"    [{ts}] {role}: {d['text'][:120]}")

        if args.deep and evidence.get("full_excerpt"):
            print(f"\n  Full excerpt ({len(evidence['full_excerpt'])} msgs):")
            for m in evidence["full_excerpt"][:20]:
                ts = _ts19(m.get("timestamp"))
                role = m.get("role", "?")
                text = m.get("text", "")[:100].replace("\n", " ")
                print(f"    [{ts}] {role}: {text}")

        print()

    elapsed = time.time() - t_start
    print(f"=== done in {elapsed:.2f}s ===")


def cmd_sessions(args):
    """List sessions.

    Row fields come from the index when it has the session (keyed by path — see
    ``session_stats_by_path``; the two layers' session ids differ by an env
    prefix). Only sessions the index has never seen, or that grew after the
    build, fall back to parsing the transcript. Measured at ``--limit 200``:
    1.36 s of per-file parsing becomes 0.002 s of index reads.
    """
    sessions = find_sessions(scope=args.scope, limit=args.limit, agent=args.agent)
    known = _session_stats_by_path([path for _sid, path, _agent in sessions])
    print("SESSION_ID\tCREATED\tMODIFIED\tMSGS\tBRANCH\tAGENT\tPATH")
    for sid, path, agent in sessions:
        cached = known.get(path)
        try:
            file_mtime = os.path.getmtime(path)
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(file_mtime))
        except OSError:
            file_mtime = None
            mtime = "?"
        # Trust the index only while it is known to be current for this file;
        # a transcript that grew since the build is re-parsed so the row shows
        # live numbers instead of numbers from an hour ago.
        fresh = (cached is not None and file_mtime is not None
                 and cached.get("jsonl_mtime")
                 and abs(file_mtime - cached["jsonl_mtime"]) < 1)
        if fresh:
            created = cached["created"][:10]
            msgs = cached["msgs"]
            branch = cached["branch"]
        else:
            try:
                stats = echolib.dispatch_session_stats(path)
                created = stats.get("started", "")[:10]
                msgs = stats.get("user_messages", 0) + stats.get("assistant_messages", 0)
                branch = stats.get("branch", "")
            except Exception:
                created = "?"
                msgs = 0
                branch = ""
        print(f"{sid}\t{created}\t{mtime}\t{msgs}\t{branch}\t{agent}\t{path}")
    print(f"--- {len(sessions)} session(s) ---")
    if not sessions and args.scope == "current":
        print(
            "hint: no sessions matched this cwd; try --scope all "
            "(or cd into a project that has history)",
            file=sys.stderr,
        )


def cmd_stats(args):
    """Show aggregate stats. Reads the prebuilt index (one SQL pass); falls
    back to file parsing only when the index is missing."""
    reg = _resolve_cli_agent(args.agent)
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            if reg == "cross":
                row = conn.execute(
                    """SELECT COUNT(*), COALESCE(SUM(user_messages+assistant_messages),0),
                              COALESCE(SUM(tool_calls),0), COALESCE(SUM(errors),0),
                              COALESCE(SUM(total_tokens),0) FROM sessions"""
                ).fetchone()
                n_sessions, total_msgs, total_tools, total_errors, total_tokens = row
                env_breakdown = conn.execute(
                    """SELECT agent, COUNT(*), SUM(tool_calls), SUM(errors)
                       FROM sessions GROUP BY agent ORDER BY COUNT(*) DESC"""
                ).fetchall()
            else:
                row = conn.execute(
                    """SELECT COUNT(*), COALESCE(SUM(user_messages+assistant_messages),0),
                              COALESCE(SUM(tool_calls),0), COALESCE(SUM(errors),0),
                              COALESCE(SUM(total_tokens),0) FROM sessions
                       WHERE agent = ?""",
                    (reg,),
                ).fetchone()
                n_sessions, total_msgs, total_tools, total_errors, total_tokens = row
                env_breakdown = []
            fts_n = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
            conn.close()
            print(f"Total sessions: {n_sessions}")
            print(f"Total messages: {total_msgs}")
            print(f"Total tool calls: {total_tools}")
            print(f"Tool errors: {total_errors} (failed tool calls, not index parse errors)")
            print(f"Total tokens: {total_tokens}")
            print(f"Index: {n_sessions} sessions, {fts_n} messages indexed")
            for agent_id, n, tools, errs in env_breakdown[:8]:
                print(f"  {agent_id}: {n} sessions, {tools or 0} tools, {errs or 0} errors")
            return
        except sqlite3.Error as exc:
            print(f"[sd-recall] index read failed ({exc}); falling back to file scan.",
                  file=sys.stderr)

    sessions = find_sessions(scope="all", limit=1000, agent=args.agent)
    total_msgs = 0
    total_tools = 0
    total_errors = 0
    total_tokens = 0

    for sid, path, agent in sessions:
        try:
            s = echolib.dispatch_session_stats(path)
            total_msgs += s.get("user_messages", 0) + s.get("assistant_messages", 0)
            total_tools += s.get("tool_calls", 0)
            total_errors += s.get("errors", 0)
            total_tokens += s.get("total_tokens", 0)
        except Exception:
            continue

    print(f"Total sessions: {len(sessions)}")
    print(f"Total messages: {total_msgs}")
    print(f"Total tool calls: {total_tools}")
    print(f"Tool errors: {total_errors} (failed tool calls, not index parse errors)")
    print(f"Total tokens: {total_tokens}")


def cmd_session_stats(args):
    """Show stats for one session."""
    path = args.path
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)
    stats = echolib.dispatch_session_stats(path)
    for k, v in stats.items():
        print(f"{k}={v}")


def cmd_messages(args):
    """Extract messages from a session."""
    path = args.path
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)
    for m in echolib.dispatch_extract_messages(path, role=args.role, no_tools=args.no_tools, limit=args.limit):
        print(f"[{m['role']}] {m['timestamp']}")
        print(f"  {m['text'][:200]}")
        print()


def cmd_tools(args):
    """Extract tool calls from a session."""
    path = args.path
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)
    for t in echolib.dispatch_extract_tools(path, errors_only=args.errors_only, limit=args.limit):
        print(f"[{t['status']}] {t['name']} at {t['timestamp'][:19]}")
        print(f"  input: {t['key_input'][:100]}")
        print(f"  output: {t['result_preview'][:100]}")
        print()


def cmd_files(args):
    """Show files changed in a session."""
    path = args.path
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)
    files = echolib.extract_files_changed(path, with_versions=args.with_versions)
    for f in files:
        if len(f) > 1:
            print(f"{f[0]} (v{f[1]})")
        else:
            print(f[0])
    if not files:
        print("(no file snapshots found)")


def cmd_schema(args):
    """Detect schema of a JSONL file."""
    path = args.path
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)
    schema = echolib.detect_schema(path)
    print(f"File: {schema['file']}")
    print(f"Lines: {schema['lines']}, Bytes: {schema['bytes']}")
    print(f"Time range: {schema.get('first_timestamp', '?')} → {schema.get('last_timestamp', '?')}")
    if schema.get('versions'):
        print(f"Versions: {', '.join(schema['versions'])}")
    if schema.get('models'):
        print(f"Models: {', '.join(schema['models'])}")
    if schema.get('unknown_types'):
        print(f"[UNKNOWN] types: {', '.join(schema['unknown_types'])}")
    print(f"\nRecord types ({len(schema.get('record_types', {}))}):")
    for rtype, info in sorted(schema.get('record_types', {}).items()):
        print(f"  {rtype}: {info['count']} records, fields={info['fields']}")


def cmd_save_summary(args):
    """Save analysis result as cached summary."""
    path = args.path
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)
    if args.stdin:
        analysis = sys.stdin.read()
    else:
        analysis = args.text
    if not analysis:
        print("ERROR: Analysis text is empty")
        sys.exit(1)
    excluded = [s.strip() for s in args.excluded.split(";") if s.strip()] if args.excluded else []
    summary_path = echolib.save_analysis_result(
        path, analysis, args.query, args.agent,
        memory_tier=args.tier, excluded=excluded
    )
    print(f"Saved analysis summary for {os.path.basename(path)}")
    print(f"  Intent: {args.query}")
    print(f"  Agent:  {args.agent}")
    print(f"  Tier:   {args.tier}")
    if excluded:
        print(f"  Excluded: {excluded}")
    print(f"  Path:   {summary_path}")


def cmd_extract_knowledge(args):
    """Extract knowledge from a session."""
    path = args.path
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)
    items = list(echolib.extract_knowledge(path))
    print(json.dumps(items, indent=2, ensure_ascii=False))


# Patterns for recall-lite's --decisions mode. Deliberately kept apart from
# index_builder._evidence.DECISION_PATTERNS: this list matches wider English
# variants (decide/deciding) and lacks 尝试 — converging them would silently
# change what --decisions surfaces. Both are pinned by tests instead.
_LITE_DECISION_PATTERNS = [
    r"(?i)\b(decided|decide|deciding)\s+to\b",
    r"(?i)\b(chose|choose|choosing)\s+(to|instead)\b",
    r"(?i)\bgoing\s+to\s+(use|switch|try|migrate)\b",
    r"(?i)\bwill\s+(use|switch|try|migrate|go\s+with)\b",
    r"(?i)\binstead\s+of\b",
    r"(?i)\bswitch(ed|ing)?\s+to\b",
    r"(?i)\buse\s+\w+\s+over\b",
    r"(?i)\bmoving\s+to\b",
    r"(?i)决定",
    r"(?i)选择",
    r"(?i)改用",
    r"(?i)还是",
    r"(?i)换成",
    r"(?i)放弃",
]


def _lite_format_messages(msgs):
    """cmd_messages display format, as one string (also fed to save-summary)."""
    return "".join(
        f"[{m['role']}] {m['timestamp']}\n  {m['text'][:200]}\n\n" for m in msgs)


def _lite_format_tools(tools):
    """cmd_tools display format, as one string."""
    return "".join(
        f"[{t['status']}] {t['name']} at {t['timestamp'][:19]}\n"
        f"  input: {t['key_input'][:100]}\n"
        f"  output: {t['result_preview'][:100]}\n\n" for t in tools)


def _lite_print_cached(path, query):
    """Summary-cache block (the old per-session heredoc #1). True on hit."""
    results = echolib.load_analysis_result(path, query_intent=query)
    if not results:
        return False
    for rec in results:
        tier = rec.get("memory_tier", "periodic")
        tier_label = {"permanent": "永久", "periodic": "7天", "once": "24h"}.get(tier, tier)
        print("=== [CACHED] 分析意图: %s ===" % rec.get("query_intent", ""))
        print("  分析时间: %s" % rec.get("analyzed_at", ""))
        print("  时效等级: %s (%s)" % (tier, tier_label))
        excluded = rec.get("excluded", [])
        if excluded:
            print("  已否决方向:")
            for ex in excluded:
                print("    - %s" % ex)
        print("  ---")
        print(rec.get("analysis", ""))
        print("---")
    return True


def _lite_print_decisions(path):
    """Decision-point block (the old --decisions heredoc).

    The heredoc called the claude-only ``extract_messages``, so decision points
    silently vanished for every grok/kimi/… session the same command listed.
    ``dispatch_extract_messages`` keeps the claude behaviour and fixes the rest.
    """
    count = 0
    for rec in echolib.dispatch_extract_messages(path, role="both"):
        text = rec["text"]
        if len(text) < 10:
            continue
        for pat in _LITE_DECISION_PATTERNS:
            if re.search(pat, text):
                ts = rec.get("timestamp", "")[:19]
                role = rec.get("role", "?")
                print(f"  [{ts}] {role}: {text[:200].replace(chr(10), ' ')}")
                count += 1
                break
        if count >= 15:
            break
    if count == 0:
        print("  (no decision points found)")


def cmd_lite_report(args):
    """Render recall-lite evidence blocks for session rows read on stdin.

    The bash loop used to spawn one Python process per session just to look up
    the summary cache, plus up to four more per cache miss (messages, tools,
    decisions, save-summary) — each re-importing echolib from scratch. This
    command reproduces the exact block format in a single process, which is
    what lets recall-lite.sh stay a thin front-end.
    """
    inspected = cached = parsed = 0
    for raw in sys.stdin:
        row = raw.rstrip("\n")
        if not row:
            continue
        parts = row.split("\t")
        if len(parts) != 7:
            continue  # header/trailer rows ("--- N session(s) ---") carry no tabs
        _sid, created, modified, msg_count, _branch, agent, path = parts
        if not path or not os.path.exists(path):
            continue
        inspected += 1
        bar = "=" * 60
        print(bar)
        print(f"Session {inspected}/{args.limit}")
        print(f"  Summary : {agent}")
        print(f"  Created : {created}")
        print(f"  Modified: {modified}")
        print(f"  Agent   : {agent}")
        print(f"  Messages: {msg_count}")
        print(f"  Path    : {path}")
        print(bar)
        print()

        if not args.no_summary:
            try:
                hit = _lite_print_cached(path, args.query)
            except Exception:
                hit = False
            if hit:
                cached += 1
                print()
                continue

        parsed += 1
        print("--- User messages (intent) ---")
        msgs_text = ""
        msg_ok = True
        try:
            msgs_text = _lite_format_messages(
                echolib.dispatch_extract_messages(path, role="user", limit=15))
            print(msgs_text, end="")
        except Exception:
            msg_ok = False
            print("(sd-recall messages failed)", file=sys.stderr)
        print()
        print("--- Tool errors (if any) ---")
        tools_text = ""
        try:
            tools_text = _lite_format_tools(
                echolib.dispatch_extract_tools(path, errors_only=True, limit=20))
            print(tools_text, end="")
        except Exception:
            print("(sd-recall tools failed)", file=sys.stderr)
        print()

        if args.deep:
            print("--- Full excerpt (both roles, up to 30 messages) ---")
            try:
                print(_lite_format_messages(
                    echolib.dispatch_extract_messages(path, role="both", limit=30)), end="")
            except Exception:
                print("(sd-recall messages failed)")
            print()

        if args.decisions:
            print("--- Decision points ---")
            try:
                _lite_print_decisions(path)
            except Exception:
                print("  (no decision points found)")
            print()

        # Auto-cache the parsed evidence (same 2000-byte cap the shell pipe
        # applied); --no-summary skips cache *reads*, never writes.
        if msg_ok:
            analysis = (msgs_text + "\n---\n" + tools_text)
            analysis = analysis.encode("utf-8")[:2000].decode("utf-8", "ignore")
            try:
                echolib.save_analysis_result(
                    path, analysis, args.query, "auto", memory_tier="periodic")
            except Exception:
                pass

    print(f"=== recall-lite done. {inspected} session(s) inspected: "
          f"{cached} cached, {parsed} parsed. ===")
    if parsed:
        print("  (本次解析结果已自动缓存，下次 recall 同主题将命中 [CACHED])")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="session-digger unified recall engine")
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Search sessions by keyword")
    p_search.add_argument("keyword")
    p_search.add_argument("--scope", default="current", choices=["current", "all"])
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument("--decisions", action="store_true")
    p_search.add_argument("--deep", action="store_true")
    p_search.add_argument("--include-tools", action="store_true",
                          help="Also match tool result digests (default: prose only)")
    _agent_choices = _cli_agent_choices()
    p_search.add_argument("--agent", default="cross", choices=_agent_choices)

    p_list = sub.add_parser("sessions", help="List sessions")
    p_list.add_argument("--scope", default="current", choices=["current", "all"])
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--agent", default="cross", choices=_agent_choices)

    p_stats = sub.add_parser("stats", help="Aggregate statistics")
    p_stats.add_argument("--agent", default="cross", choices=_agent_choices)

    p_ss = sub.add_parser("session-stats", help="Show stats for one session file")
    p_ss.add_argument("path")

    p_msgs = sub.add_parser("messages", help="Extract messages from a session")
    p_msgs.add_argument("path")
    p_msgs.add_argument("--role", default="both", choices=["user", "assistant", "both"])
    p_msgs.add_argument("--no-tools", action="store_true")
    p_msgs.add_argument("--limit", type=int, default=20)

    p_tools = sub.add_parser("tools", help="Extract tool calls from a session")
    p_tools.add_argument("path")
    p_tools.add_argument("--errors-only", action="store_true")
    p_tools.add_argument("--limit", type=int, default=20)

    p_files = sub.add_parser("files", help="Show files changed in a session")
    p_files.add_argument("path")
    p_files.add_argument("--with-versions", action="store_true")

    p_schema = sub.add_parser("schema", help="Detect JSONL schema of a session file")
    p_schema.add_argument("path")

    p_ssave = sub.add_parser("save-summary", help="Save analysis result as cached summary")
    p_ssave.add_argument("path")
    p_ssave.add_argument("text", nargs="?", default="",
        help="Analysis text (omit and use --stdin to read from pipe)")
    p_ssave.add_argument("--stdin", action="store_true", help="Read analysis text from stdin")
    p_ssave.add_argument("--query", default="general", help="Query intent label")
    p_ssave.add_argument("--agent", default="auto", help="Source agent type")
    p_ssave.add_argument("--tier", default="periodic",
        choices=["permanent", "periodic", "once"], help="Memory tier")
    p_ssave.add_argument("--excluded", default="",
        help="Semicolon-separated excluded directions")

    p_know = sub.add_parser("extract-knowledge",
        help="Extract decisions, corrections, patterns from a session")
    p_know.add_argument("path")

    p_lite = sub.add_parser("lite-report",
        help="Render recall-lite evidence blocks from session rows on stdin (one process)")
    p_lite.add_argument("--query", default="", help="Search term (labels cached summaries)")
    p_lite.add_argument("--limit", type=int, default=5)
    p_lite.add_argument("--deep", action="store_true")
    p_lite.add_argument("--decisions", action="store_true")
    p_lite.add_argument("--no-summary", action="store_true",
        help="Skip cached-summary reads (parsing still re-saves)")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "sessions":
        cmd_sessions(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "session-stats":
        cmd_session_stats(args)
    elif args.command == "messages":
        cmd_messages(args)
    elif args.command == "tools":
        cmd_tools(args)
    elif args.command == "files":
        cmd_files(args)
    elif args.command == "schema":
        cmd_schema(args)
    elif args.command == "save-summary":
        cmd_save_summary(args)
    elif args.command == "extract-knowledge":
        cmd_extract_knowledge(args)
    elif args.command == "lite-report":
        cmd_lite_report(args)
    else:
        # 未知命令：帮 help 下移到结构化输出，rc=2 让 shell / 调用方能 guard。
        parser.print_help(sys.stderr)
        sys.exit(2)
