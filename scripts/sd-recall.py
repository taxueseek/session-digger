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
import os
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import echolib

from index_builder._schema import DB_PATH  # 单一真源：~/.claude/.session-digger/index.db 或 $SESSION_DIGGER_DATA_DIR

# CLI agent names → adapter registry names
_AGENT_MAP = {
    "claude": "claude",
    "grok": "grok",
    "kimi": "kimi_code",
    "kimi_code": "kimi_code",
    "cross": "cross",
}

# Decision keywords (bilingual)
DECISION_PATTERNS = [
    r"(?i)\bdecided to\b", r"(?i)\bchose to\b", r"(?i)\bgoing to (use|switch|try|migrate)\b",
    r"(?i)\bwill (use|switch|try|migrate|go with)\b", r"(?i)\binstead of\b",
    r"(?i)\bswitch(ed|ing)? to\b", r"(?i)\buse \w+ over\b", r"(?i)\bmoving to\b",
    r"决定", r"选择", r"改用", r"还是", r"换成", r"放弃", r"尝试",
]


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def find_sessions(scope="current", limit=50, keyword=None, agent="cross"):
    """Find session JSONL files. Returns list of (session_id, path, agent)."""
    entries = []
    seen = set()

    # Files to exclude (non-session global files)
    GLOBAL_EXCLUDES = {"prompt_history.jsonl", "history.jsonl"}

    # Map CLI agent names to scan targets
    if agent == "cross":
        agents_to_scan = ["claude", "grok", "kimi_code"]
    else:
        agents_to_scan = [_AGENT_MAP.get(agent, agent)]

    for atype in agents_to_scan:
        if atype == "claude":
            base = Path.home() / ".claude" / "projects"
            label = "claude"
            if not base.exists():
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                for jf in d.glob("*.jsonl"):
                    if "subagents" in str(jf):
                        continue
                    if ".jsonl." in jf.name:
                        continue
                    if jf.name in GLOBAL_EXCLUDES:
                        continue
                    key = str(jf.resolve())
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append((jf.stem, str(jf), label))

        elif atype == "grok":
            base = Path.home() / ".grok" / "sessions"
            label = "grok"
            if not base.exists():
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                for session_dir in d.iterdir():
                    if not session_dir.is_dir():
                        continue
                    chat_file = session_dir / "chat_history.jsonl"
                    if chat_file.exists():
                        key = str(chat_file.resolve())
                        if key in seen:
                            continue
                        seen.add(key)
                        entries.append((session_dir.name, str(chat_file), label))

        elif atype == "kimi_code":
            base = Path.home() / ".kimi-code" / "sessions"
            label = "kimi"
            if not base.exists():
                continue
            for project_dir in base.iterdir():
                if not project_dir.is_dir():
                    continue
                for session_dir in project_dir.iterdir():
                    if not session_dir.is_dir():
                        continue
                    # Kimi Code nests: session_<uuid>/agents/main/wire.jsonl
                    wire_file = session_dir / "agents" / "main" / "wire.jsonl"
                    if wire_file.exists():
                        key = str(wire_file.resolve())
                        if key in seen:
                            continue
                        seen.add(key)
                        entries.append((session_dir.name.replace("session_", ""), str(wire_file), label))

    # Sort by mtime descending
    entries.sort(key=lambda x: os.path.getmtime(x[1]) if os.path.exists(x[1]) else 0, reverse=True)

    # Scope filter: current project only
    if scope == "current":
        cwd = os.getcwd()
        entries = [e for e in entries if _session_in_cwd(e, cwd)]

    # Keyword filter: FTS first (path resolved from index — no file scan needed),
    # then file scan fallback (index missing).
    if keyword:
        fts_results = _fts_search(keyword, limit=limit)
        if fts_results is not None:
            # FTS hit — paths resolved directly from sessions table
            if scope == "current":
                cwd = os.getcwd()
                fts_results = [e for e in fts_results if _session_in_cwd(e, cwd)]
            return fts_results[:limit]
        # Fallback: file scan (index missing or no FTS match)
        matched = []
        for sid, path, at in entries:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    head = f.read(50000)
                if keyword.lower() in head.lower():
                    matched.append((sid, path, at))
                if len(matched) >= limit:
                    break
            except OSError:
                continue
        return matched

    return entries[:limit]


def _session_in_cwd(entry, cwd):
    """Check if a session belongs to the current working directory."""
    sid, path, at = entry
    ps = str(Path(path).resolve())
    cwd_resolved = str(Path(cwd).resolve())
    # For Claude: encoded project dir contains the cwd path
    if at == "claude":
        encoded = cwd_resolved.replace("/", "-")
        return encoded in ps or cwd_resolved in ps
    # For Grok: URL-encoded cwd in path
    if at == "grok":
        import urllib.parse
        encoded_cwd = urllib.parse.quote(cwd_resolved, safe="")
        return encoded_cwd in ps
    # For Kimi: project dir name may contain workspace hint
    if at == "kimi":
        return cwd_resolved.split("/")[-1] in ps
    return True


def _fts_search(keyword, limit=10):
    """Try FTS5 search. Returns list of (session_id, jsonl_path, agent) tuples,
    or None if index missing/no matches.

    Resolves paths directly from the sessions table (jsonl_path column),
    avoiding the need for a full file-system scan to build a path_map.
    """
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        safe_kw = keyword.replace('"', '""').replace(":", " ")
        # FTS5 returns message-level rows; join to sessions for path/agent.
        # Get distinct session_ids in BM25 score order, then resolve paths.
        rows = conn.execute("""
            SELECT m.session_id, s.jsonl_path, s.agent
            FROM messages_fts m
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
            ORDER BY bm25(messages_fts)
            LIMIT ?
        """, (safe_kw, limit * 5)).fetchall()
        conn.close()
        if not rows:
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
        return results if results else None
    except Exception:
        return None


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
    except Exception:
        pass

    # Tool errors via dispatch
    try:
        for t in echolib.dispatch_extract_tools(session_path, errors_only=True, limit=20):
            result["tool_errors"].append(t)
    except Exception:
        pass

    # Deep mode: full excerpt
    if deep:
        try:
            all_msgs = list(echolib.dispatch_extract_messages(session_path, role="both", limit=30))
            result["full_excerpt"] = all_msgs
        except Exception:
            pass

    return result


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
    print(f"  Found {len(sessions)} session(s). Index: {'HIT' if DB_PATH.exists() else 'MISS (run: index-builder.py build)'}")
    print()

    for i, (sid, path, agent) in enumerate(sessions, 1):
        try:
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
        except OSError:
            mtime = "?"

        quick_stats = {}
        try:
            s = echolib.dispatch_session_stats(path)
            quick_stats = {
                "msgs": s.get("user_messages", 0),
                "tools": s.get("tool_calls", 0),
                "errors": s.get("errors", 0),
                "branch": s.get("branch", ""),
            }
        except Exception:
            pass

        print(f"--- [{i}/{len(sessions)}] {sid} ({agent}, {mtime}) ---")
        if quick_stats:
            print(f"  Messages: {quick_stats.get('msgs', '?')} | Tools: {quick_stats.get('tools', '?')} | Errors: {quick_stats.get('errors', '?')} | Branch: {quick_stats.get('branch', '')}")

        evidence = extract_evidence(path, decisions=args.decisions, deep=args.deep)

        if evidence["user_messages"]:
            print(f"\n  User messages ({len(evidence['user_messages'])}):")
            for m in evidence["user_messages"][:8]:
                ts = m["timestamp"][:19] if m.get("timestamp") else "?"
                text = m["text"][:150].replace("\n", " ")
                print(f"    [{ts}] {text}")

        if evidence["tool_errors"]:
            print(f"\n  Tool errors ({len(evidence['tool_errors'])}):")
            for t in evidence["tool_errors"][:5]:
                print(f"    [{t['timestamp'][:19]}] {t['name']}: {t['result_preview'][:80]}")

        if args.decisions and evidence.get("decisions"):
            print(f"\n  Decision points ({len(evidence['decisions'])}):")
            for d in evidence["decisions"][:5]:
                ts = d["timestamp"][:19] if d.get("timestamp") else "?"
                role = d.get("role", "?")
                print(f"    [{ts}] {role}: {d['text'][:120]}")

        if args.deep and evidence.get("full_excerpt"):
            print(f"\n  Full excerpt ({len(evidence['full_excerpt'])} msgs):")
            for m in evidence["full_excerpt"][:20]:
                ts = m.get("timestamp", "?")[:19]
                role = m.get("role", "?")
                text = m.get("text", "")[:100].replace("\n", " ")
                print(f"    [{ts}] {role}: {text}")

        print()

    elapsed = time.time() - t_start
    print(f"=== done in {elapsed:.2f}s ===")


def cmd_sessions(args):
    """List sessions."""
    sessions = find_sessions(scope=args.scope, limit=args.limit, agent=args.agent)
    print("SESSION_ID\tCREATED\tMODIFIED\tMSGS\tBRANCH\tAGENT\tPATH")
    for sid, path, agent in sessions:
        try:
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
            stats = echolib.dispatch_session_stats(path)
            created = stats.get("started", "")[:10]
            msgs = stats.get("user_messages", 0) + stats.get("assistant_messages", 0)
            branch = stats.get("branch", "")
        except Exception:
            created = mtime = "?"
            msgs = 0
            branch = ""
        print(f"{sid}\t{created}\t{mtime}\t{msgs}\t{branch}\t{agent}\t{path}")
    print(f"--- {len(sessions)} session(s) ---")


def cmd_stats(args):
    """Show aggregate stats."""
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
    print(f"Total errors: {total_errors}")
    print(f"Total tokens: {total_tokens}")
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        fts_n = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        idx_n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()
        print(f"Index: {idx_n} sessions, {fts_n} messages indexed")


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
    p_search.add_argument("--agent", default="cross", choices=["claude", "grok", "kimi", "kimi_code", "cross"])

    p_list = sub.add_parser("sessions", help="List sessions")
    p_list.add_argument("--scope", default="current", choices=["current", "all"])
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--agent", default="cross", choices=["claude", "grok", "kimi", "kimi_code", "cross"])

    p_stats = sub.add_parser("stats", help="Aggregate statistics")
    p_stats.add_argument("--agent", default="cross", choices=["claude", "grok", "kimi", "kimi_code", "cross"])

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
        parser.print_help()
