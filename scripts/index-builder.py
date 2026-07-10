#!/usr/bin/env python3
"""index-builder.py — Pre-compute SQLite index for fast session querying.

Thin CLI wrapper around index_builder._schema and index_builder._builder.
All query functions and CLI dispatch are maintained here but delegate to
the builder submodules for scan/build logic.

Usage:
  index-builder.py build [--rebuild] [--agent all|cross|<name>]
  index-builder.py search <keyword>
  index-builder.py detail <session_id>
  index-builder.py stats
  index-builder.py summary
  index-builder.py anomalies | advantages
  index-builder.py query <agent|project|flagged|recent>
"""
import argparse
import json
import sqlite3
from pathlib import Path

import sys as _sys
_SCRIPT_DIR = Path(__file__).parent
_sys.path.insert(0, str(_SCRIPT_DIR))

from index_builder._schema import DB_PATH  # noqa: E402
from index_builder._builder import build_index, scan_sessions  # noqa: E402,F401

def search_fts(keyword, limit=10):
    """Fast full-text search using the pre-built FTS index."""
    if not DB_PATH.exists():
        return None  # Index not built yet

    conn = sqlite3.connect(str(DB_PATH))
    try:
        # FTS5 query: prefix match + NEAR for proximity
        # Sanitize: FTS5 special chars
        safe_kw = keyword.replace('"', '""').replace(":", " ")
        rows = conn.execute("""
            SELECT session_id, role, timestamp, text,
                   bm25(messages_fts) as score
            FROM messages_fts
            WHERE messages_fts MATCH ?
            ORDER BY score
            LIMIT ?
        """, (safe_kw, limit)).fetchall()

        return [
            {"session_id": r[0], "role": r[1], "timestamp": r[2],
             "text": r[3], "score": round(r[4], 2)}
            for r in rows
        ]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()


def session_detail(session_id):
    """Return full detail for a session."""
    if not DB_PATH.exists():
        return None

    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        conn.close()
        return None

    cols = [d[0] for d in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    result = dict(zip(cols, row))

    topics = conn.execute(
        "SELECT message_index, timestamp, topic_label, confidence FROM topic_boundaries WHERE session_id = ?",
        (session_id,)
    ).fetchall()
    result["topics"] = [
        {"index": t[0], "timestamp": t[1], "label": t[2], "confidence": t[3]}
        for t in topics
    ]

    conn.close()
    return result


# ---------------------------------------------------------------------------
# Cached Query API — for other skills to consume session data efficiently.
# Every function reads ONLY from the pre-built SQLite index, never from raw
# transcripts. This is the "data analysis" layer that enables AI to find
# anomalies and advantages across all environments.
# ---------------------------------------------------------------------------

def query_by_agent(agent=None, limit=50):
    """Query sessions grouped by agent/environment.

    Args:
        agent: specific adapter name (e.g. "claude", "grok"), or None for all.
        limit: max sessions to return.

    Returns list of session summaries with rich stats.
    """
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        if agent:
            rows = conn.execute("""
                SELECT id, agent, created, user_messages, assistant_messages,
                       tool_calls, errors, project_name, summary,
                       tool_usage_json, flags_json, duration_seconds
                FROM sessions WHERE agent = ?
                ORDER BY created DESC LIMIT ?
            """, (agent, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, agent, created, user_messages, assistant_messages,
                       tool_calls, errors, project_name, summary,
                       tool_usage_json, flags_json, duration_seconds
                FROM sessions
                ORDER BY created DESC LIMIT ?
            """, (limit,)).fetchall()
        return [_row_to_summary(r) for r in rows]
    finally:
        conn.close()


def query_by_project(project=None, limit=50):
    """Query sessions grouped by project.

    Args:
        project: project name filter, or None for all.
    """
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        if project:
            rows = conn.execute("""
                SELECT id, agent, created, user_messages, assistant_messages,
                       tool_calls, errors, project_name, summary,
                       tool_usage_json, flags_json, duration_seconds
                FROM sessions WHERE project_name = ?
                ORDER BY created DESC LIMIT ?
            """, (project, limit)).fetchall()
        else:
            # Aggregate by project
            rows = conn.execute("""
                SELECT project_name, COUNT(*) as cnt,
                       SUM(user_messages) as um, SUM(assistant_messages) as am,
                       SUM(tool_calls) as tc, SUM(errors) as er
                FROM sessions WHERE project_name IS NOT NULL
                GROUP BY project_name
                ORDER BY cnt DESC LIMIT ?
            """, (limit,)).fetchall()
            return [{"project": r[0], "session_count": r[1],
                     "user_messages": r[2], "assistant_messages": r[3],
                     "tool_calls": r[4], "errors": r[5]} for r in rows]
        return [_row_to_summary(r) for r in rows]
    finally:
        conn.close()


def query_flagged(limit=50):
    """Find sessions with quality flags (high error rate, long conversations, etc.)."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute("""
            SELECT id, agent, created, user_messages, assistant_messages,
                   tool_calls, errors, project_name, summary,
                   tool_usage_json, flags_json, duration_seconds
            FROM sessions WHERE flags_json != '[]' AND flags_json IS NOT NULL
            ORDER BY created DESC LIMIT ?
        """, (limit,)).fetchall()
        results = []
        for r in rows:
            s = _row_to_summary(r)
            try:
                s["flags"] = json.loads(r[10] or "[]")
            except (json.JSONDecodeError, TypeError):
                s["flags"] = []
            results.append(s)
        return results
    finally:
        conn.close()


def query_recent(days=7, limit=50):
    """Find sessions from the last N days."""
    if not DB_PATH.exists():
        return []
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute("""
            SELECT id, agent, created, user_messages, assistant_messages,
                   tool_calls, errors, project_name, summary,
                   tool_usage_json, flags_json, duration_seconds
            FROM sessions WHERE created >= ?
            ORDER BY created DESC LIMIT ?
        """, (cutoff, limit)).fetchall()
        return [_row_to_summary(r) for r in rows]
    finally:
        conn.close()


def query_anomalies(min_error_rate=0.25, min_sessions=1):
    """Detect anomalous sessions: high error rates, retry loops, etc.

    This is the 'anomaly detection' function that leverages the cached
    index to find sessions that deviate significantly from the norm.
    """
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        anomalies = []

        # 1. High error rate sessions
        rows = conn.execute("""
            SELECT id, agent, jsonl_path, tool_calls, errors,
                   tool_usage_json, tool_errors_json, project_name, created
            FROM sessions WHERE tool_calls > 0 AND errors > 0
            AND (CAST(errors AS FLOAT) / CAST(tool_calls AS FLOAT)) >= ?
            ORDER BY created DESC LIMIT 50
        """, (min_error_rate,)).fetchall()
        for r in rows:
            error_rate = r[4] / r[3] if r[3] > 0 else 0
            anomalies.append({
                "type": "high_error_rate",
                "session_id": r[0], "agent": r[1], "source_path": r[2],
                "tool_calls": r[3], "errors": r[4],
                "error_rate": round(error_rate, 2),
                "project": r[7], "created": r[8],
            })

        # 2. Retry loops (one tool called 15+ times)
        rows = conn.execute("""
            SELECT id, agent, jsonl_path, tool_usage_json, project_name, created
            FROM sessions WHERE tool_usage_json IS NOT NULL
            AND tool_usage_json != '{}'
        """).fetchall()
        for r in rows:
            try:
                tu = json.loads(r[3] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for tool, count in tu.items():
                if count >= 15:
                    anomalies.append({
                        "type": "retry_loop",
                        "session_id": r[0], "agent": r[1], "source_path": r[2],
                        "tool": tool, "call_count": count,
                        "project": r[4], "created": r[5],
                    })

        # 3. Very long conversations (50+ messages)
        rows = conn.execute("""
            SELECT id, agent, jsonl_path, user_messages, assistant_messages,
                   project_name, created
            FROM sessions WHERE (user_messages + assistant_messages) >= 50
            ORDER BY created DESC LIMIT 50
        """).fetchall()
        for r in rows:
            anomalies.append({
                "type": "long_conversation",
                "session_id": r[0], "agent": r[1], "source_path": r[2],
                "total_messages": r[3] + r[4],
                "project": r[5], "created": r[6],
            })

        return anomalies
    finally:
        conn.close()


def query_advantages(limit=30):
    """Detect positive patterns: efficient sessions, low error rates, etc.

    This complements query_anomalies by finding sessions that demonstrate
    good practices — useful for extracting 'what works' patterns.
    """
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        advantages = []

        # 1. Efficient sessions: high message count, zero errors, reasonable tool usage
        rows = conn.execute("""
            SELECT id, agent, jsonl_path, user_messages, assistant_messages,
                   tool_calls, errors, project_name, created, duration_seconds
            FROM sessions WHERE errors = 0 AND tool_calls > 5
            AND (user_messages + assistant_messages) BETWEEN 10 AND 50
            ORDER BY (user_messages + assistant_messages) DESC LIMIT ?
        """, (limit,)).fetchall()
        for r in rows:
            advantages.append({
                "type": "clean_efficient",
                "session_id": r[0], "agent": r[1], "source_path": r[2],
                "messages": r[3] + r[4], "tool_calls": r[5],
                "project": r[7], "created": r[8],
                "duration_seconds": r[9],
            })

        # 2. Fast resolution: short sessions with tools (quick problem-solving)
        rows = conn.execute("""
            SELECT id, agent, jsonl_path, user_messages, assistant_messages,
                   tool_calls, project_name, created, duration_seconds
            FROM sessions WHERE duration_seconds IS NOT NULL
            AND duration_seconds > 0 AND duration_seconds < 300
            AND tool_calls > 3
            ORDER BY duration_seconds ASC LIMIT ?
        """, (limit,)).fetchall()
        for r in rows:
            advantages.append({
                "type": "fast_resolution",
                "session_id": r[0], "agent": r[1], "source_path": r[2],
                "messages": r[3] + r[4], "tool_calls": r[5],
                "duration_seconds": r[8],
                "project": r[6], "created": r[7],
            })

        return advantages
    finally:
        conn.close()


def export_summary():
    """Export a complete analytical summary of the entire index.

    This is the primary entry point for other skills to consume session data.
    Returns a JSON-serializable dict with:
    - environment breakdown
    - project breakdown
    - anomaly summary
    - advantage summary
    - trend indicators
    """
    if not DB_PATH.exists():
        return {"error": "Index not built. Run: index-builder.py build"}

    conn = sqlite3.connect(str(DB_PATH))
    try:
        # Environment breakdown
        env_rows = conn.execute("""
            SELECT agent, COUNT(*) as cnt,
                   SUM(user_messages) as um, SUM(assistant_messages) as am,
                   SUM(tool_calls) as tc, SUM(errors) as er,
                   COUNT(CASE WHEN flags_json != '[]' THEN 1 END) as flagged
            FROM sessions GROUP BY agent ORDER BY cnt DESC
        """).fetchall()
        environments = [{
            "agent": r[0], "session_count": r[1],
            "user_messages": r[2] or 0, "assistant_messages": r[3] or 0,
            "tool_calls": r[4] or 0, "errors": r[5] or 0,
            "flagged_sessions": r[6] or 0,
        } for r in env_rows]

        # Project breakdown (top 10)
        proj_rows = conn.execute("""
            SELECT project_name, COUNT(*) as cnt,
                   SUM(tool_calls) as tc, SUM(errors) as er
            FROM sessions WHERE project_name IS NOT NULL
            GROUP BY project_name ORDER BY cnt DESC LIMIT 10
        """).fetchall()
        projects = [{
            "project": r[0], "session_count": r[1],
            "tool_calls": r[2] or 0, "errors": r[3] or 0,
        } for r in proj_rows]

        # Overall stats
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        total_errors = conn.execute("SELECT COALESCE(SUM(errors),0) FROM sessions").fetchone()[0]
        total_tool_calls = conn.execute("SELECT COALESCE(SUM(tool_calls),0) FROM sessions").fetchone()[0]
        total_flagged = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE flags_json != '[]' AND flags_json IS NOT NULL"
        ).fetchone()[0]

        last_build = conn.execute(
            "SELECT value FROM index_meta WHERE key='last_build'"
        ).fetchone()

        return {
            "total_sessions": total,
            "total_messages_indexed": fts_count,
            "total_tool_calls": total_tool_calls,
            "total_errors": total_errors,
            "overall_error_rate": round(total_errors / total_tool_calls, 3) if total_tool_calls > 0 else 0,
            "flagged_sessions": total_flagged,
            "environments": environments,
            "top_projects": projects,
            "last_build_timestamp": float(last_build[0]) if last_build else None,
        }
    finally:
        conn.close()


def _row_to_summary(r):
    """Convert a DB row to a summary dict."""
    try:
        tu = json.loads(r[9] or "{}")
    except (json.JSONDecodeError, TypeError):
        tu = {}
    return {
        "id": r[0], "agent": r[1], "created": r[2],
        "user_messages": r[3], "assistant_messages": r[4],
        "tool_calls": r[5], "errors": r[6],
        "project": r[7], "summary": r[8],
        "tool_usage": tu,
        "duration_seconds": r[11],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="session-digger index builder")
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="Build/update the index")
    p_build.add_argument("--project", default=None)
    p_build.add_argument("--rebuild", action="store_true")
    p_build.add_argument("--agent", default="all",
                        help="Agent filter: 'all' (default), 'cross', or specific adapter name "
                             "(claude, grok, kimi_code, codex, workbuddy, trae_cn, zcode, dim, reasonix)")

    p_search = sub.add_parser("search", help="Full-text search across all sessions")
    p_search.add_argument("keyword")
    p_search.add_argument("--limit", type=int, default=10)

    p_detail = sub.add_parser("detail", help="Get session detail")
    p_detail.add_argument("session_id")

    p_stats = sub.add_parser("stats", help="Index statistics")

    p_summary = sub.add_parser("summary", help="Export full analytical summary as JSON")

    p_anomalies = sub.add_parser("anomalies", help="Find anomalous sessions")

    p_advantages = sub.add_parser("advantages", help="Find positive pattern sessions")

    p_query = sub.add_parser("query", help="Query sessions by category")
    p_query.add_argument("query_type", choices=["agent", "project", "flagged", "recent"],
                        help="Query type: agent, project, flagged, or recent")
    p_query.add_argument("--value", default=None,
                        help="Filter value (agent name, project name, or days for recent)")
    p_query.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()

    if args.command == "build":
        result = build_index(rebuild=args.rebuild, agent_filter=args.agent)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "search":
        results = search_fts(args.keyword, limit=args.limit)
        if results is None:
            print("Index not built. Run: index-builder.py build")
        else:
            print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.command == "detail":
        detail = session_detail(args.session_id)
        if detail is None:
            print("Session not found in index.")
        else:
            print(json.dumps(detail, ensure_ascii=False, indent=2))

    elif args.command == "stats":
        summary = export_summary()
        if "error" in summary:
            print(summary["error"])
        else:
            print(f"Sessions: {summary['total_sessions']} | "
                  f"Messages indexed: {summary['total_messages_indexed']}")
            print(f"Tool calls: {summary['total_tool_calls']} | "
                  f"Errors: {summary['total_errors']} "
                  f"({summary['overall_error_rate']*100:.0f}%)")
            print(f"Flagged sessions: {summary['flagged_sessions']}")
            print(f"\nEnvironments ({len(summary['environments'])}):")
            for env in summary["environments"]:
                print(f"  {env['agent']:15s} | sessions: {env['session_count']:4d} | "
                      f"tools: {env['tool_calls']:5d} | errors: {env['errors']:4d} | "
                      f"flagged: {env['flagged_sessions']}")
            if summary["top_projects"]:
                print(f"\nTop projects:")
                for proj in summary["top_projects"][:5]:
                    print(f"  {proj['project']:20s} | sessions: {proj['session_count']:4d} | "
                          f"tools: {proj['tool_calls']:5d} | errors: {proj['errors']:4d}")
            if summary["last_build_timestamp"]:
                from datetime import datetime
                ts = datetime.fromtimestamp(summary["last_build_timestamp"])
                print(f"\nLast built: {ts.strftime('%Y-%m-%d %H:%M:%S')}")

    elif args.command == "summary":
        print(json.dumps(export_summary(), ensure_ascii=False, indent=2))

    elif args.command == "anomalies":
        results = query_anomalies()
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.command == "advantages":
        results = query_advantages()
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.command == "query":
        if args.query_type == "agent":
            results = query_by_agent(agent=args.value, limit=args.limit)
        elif args.query_type == "project":
            results = query_by_project(project=args.value, limit=args.limit)
        elif args.query_type == "flagged":
            results = query_flagged(limit=args.limit)
        elif args.query_type == "recent":
            results = query_recent(days=int(args.value or 7), limit=args.limit)
        else:
            results = []
        print(json.dumps(results, ensure_ascii=False, indent=2))

    else:
        parser.print_help()
