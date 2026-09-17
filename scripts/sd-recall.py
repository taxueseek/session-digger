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


def find_sessions(scope="current", limit=50, keyword=None, agent="cross", tag=None, outcome=None):
    """Find session JSONL files. Returns list of (session_id, path, agent).

    Discovery is fully registry-driven (``ADAPTER_REGISTRY`` / ``cross_tool_list_sessions``).
    New adapters appear here automatically — no per-env path scan in this file.
    Explicit *tag* / *outcome* keep the function free of global argparse state.
    """
    del tag, outcome  # reserved for future FTS filters; explicit > globals

    # Keyword filter: FTS first (paths from index — no filesystem scan).
    if keyword:
        fts_results = _fts_search(keyword, limit=max(limit * 3, 20))
        if fts_results is not None:
            if scope == "current":
                cwd = os.getcwd()
                fts_results = [e for e in fts_results if _session_in_cwd(e, cwd)]
            # Optional agent narrow after FTS (index is multi-env).
            reg = _resolve_cli_agent(agent)
            if reg != "cross":
                fts_results = [e for e in fts_results if e[2] == reg or e[2] == agent]
            return fts_results[:limit]

    entries = _list_from_registry(agent=agent, limit=limit, keyword=keyword or "", scope=scope)

    # Scope filter for adapters that ignore cwd at list time.
    if scope == "current":
        cwd = os.getcwd()
        entries = [e for e in entries if _session_in_cwd(e, cwd)]

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




def _fts_search(keyword, limit=10):
    """FTS5 search. Returns list of (session_id, jsonl_path, agent) tuples.

    Semantics: None = index missing or query failed (caller may fall back to
    file scan); [] = index answered, genuinely no hits (never fall back —
    a rescan of file heads cannot find what the full-text index did not).

    Resolves paths directly from the sessions table (jsonl_path column),
    avoiding the need for a full file-system scan to build a path_map.
    """
    if not DB_PATH.exists():
        return None
    match_q = build_match_query(keyword)
    if not match_q:
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        # FTS5 returns message-level rows; join to sessions for path/agent.
        # Get distinct session_ids in BM25 score order, then resolve paths.
        rows = conn.execute("""
            SELECT m.session_id, s.jsonl_path, s.agent
            FROM messages_fts m
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
            ORDER BY bm25(messages_fts)
            LIMIT ?
        """, (match_q, limit * 5)).fetchall()
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
        if sid not in seen and path:
            seen.add(sid)
            results.append((sid, path, agent or "claude"))
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


def cmd_search(args):
    """Main search command."""
    t_start = time.time()

    sessions = find_sessions(
        scope=args.scope,
        limit=args.limit,
        keyword=args.keyword,
        agent=args.agent,
    )

    if not sessions:
        print(f"No matching sessions found for '{args.keyword}' in scope '{args.scope}'.")
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="session-digger unified recall engine")
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Search sessions by keyword")
    p_search.add_argument("keyword")
    p_search.add_argument("--scope", default="current", choices=["current", "all"])
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument("--decisions", action="store_true")
    p_search.add_argument("--deep", action="store_true")
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
    else:
        # 未知命令：帮 help 下移到结构化输出，rc=2 让 shell / 调用方能 guard。
        parser.print_help(sys.stderr)
        sys.exit(2)
