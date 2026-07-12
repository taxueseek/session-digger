#!/usr/bin/env python3
"""
trend-engine.py — Longitudinal analysis over the session-digger SQLite index.

Reads ONLY the pre-built index (~/.claude/.session-digger/index.db), never raw
transcripts. Fast, cheap, re-runnable. If you need the actual conversation
content behind a trend, use sd-recall.py with the session_id.

Three analysis modes:
  1. period-over-period  — week/month-over-month comparison with deltas
  2. by-theme            — aggregate by project or tag
  3. regressions         — detect tools whose error rate is trending upward

Usage:
    python trend-engine.py period-over-period --unit week --lookback 8
    python trend-engine.py period-over-period --unit month --lookback 6
    python trend-engine.py by-theme --group-by project
    python trend-engine.py by-theme --group-by tag
    python trend-engine.py regressions --unit week

Architecture: This is Layer 2 (TREND) in the four-layer model:
  Layer 0: PARSE   — echolib (raw transcript → stats)
  Layer 1: INDEX   — index-builder.py (stats → SQLite persistent storage)
  Layer 2: TREND   — THIS FILE (index → time-sliced aggregation)
  Layer 3: DECISION — skill-gap-finder.py (patterns → SKILL.md proposals)

Each layer has a different cost and trust level. This layer is pure
arithmetic over Layer 1's stored data — no new judgment calls.
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from index_builder._schema import DB_PATH  # 单一真源


def _connect():
    if not DB_PATH.exists():
        print(json.dumps({"error": "Index not built. Run: index-builder.py build"}))
        sys.exit(1)
    return sqlite3.connect(str(DB_PATH))


def _period_key(dt, unit):
    """Bucket a datetime into a period key."""
    if unit == "week":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    elif unit == "month":
        return f"{dt.year}-{dt.month:02d}"
    else:
        raise ValueError("unit must be 'week' or 'month'")


def _parse_session_date(date_str):
    """Parse a session date string (from 'created' column)."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


def _summarize_sessions(rows):
    """Aggregate stats for a set of session rows from the DB."""
    tool_usage = defaultdict(int)
    tool_errors = defaultdict(int)
    total_turns = 0
    total_duration = 0.0
    duration_count = 0
    flagged_count = 0

    for row in rows:
        # row: (id, created, tool_calls, errors, tool_usage_json, tool_errors_json,
        #        message_count, duration_seconds, flags_json, project_name, tags, agent)
        _, _, tool_calls, errors, tu_json, te_json, msg_count, duration, flags_json, _, _, _ = row

        total_turns += msg_count or 0

        if duration:
            total_duration += duration
            duration_count += 1

        if flags_json and flags_json != "[]":
            flagged_count += 1

        try:
            tu = json.loads(tu_json or "{}")
            for t, c in tu.items():
                tool_usage[t] += c
        except (json.JSONDecodeError, TypeError):
            pass

        try:
            te = json.loads(te_json or "{}")
            for t, c in te.items():
                tool_errors[t] += c
        except (json.JSONDecodeError, TypeError):
            pass

    total_calls = sum(tool_usage.values())
    total_err = sum(tool_errors.values())

    tool_error_rate = {}
    for t in tool_usage:
        if tool_usage[t] > 0:
            tool_error_rate[t] = round(tool_errors.get(t, 0) / tool_usage[t], 3)

    return {
        "session_count": len(rows),
        "total_turns": total_turns,
        "total_tool_calls": total_calls,
        "overall_error_rate": round(total_err / total_calls, 3) if total_calls else 0,
        "avg_duration_seconds": round(total_duration / duration_count, 1) if duration_count else None,
        "flagged_sessions": flagged_count,
        "tool_usage": dict(tool_usage),
        "tool_error_rate": tool_error_rate,
    }


def _load_sessions(conn, since=None, until=None):
    """Load all session rows with rich stats, optionally filtered by date."""
    query = """
        SELECT id, created, tool_calls, errors, tool_usage_json, tool_errors_json,
               message_count, duration_seconds, flags_json, project_name, tags, agent
        FROM sessions
        WHERE tool_calls IS NOT NULL
    """
    params = []
    if since:
        query += " AND created >= ?"
        params.append(since)
    if until:
        query += " AND created <= ?"
        params.append(until)

    return conn.execute(query, params).fetchall()


def cmd_period_over_period(args):
    """Week/month-over-month comparison with deltas."""
    conn = _connect()
    rows = _load_sessions(conn)
    conn.close()

    # Bucket sessions by period
    buckets = defaultdict(list)
    for row in rows:
        dt = _parse_session_date(row[1])  # row[1] = created
        if dt:
            buckets[_period_key(dt, args.unit)].append(row)

    sorted_keys = sorted(buckets.keys())[-args.lookback:]

    if len(sorted_keys) < 1:
        print(json.dumps({"note": "Not enough data for trend analysis. "
                                   "Run /index first to build the index."},
                         indent=2, ensure_ascii=False))
        return

    timeline = []
    prev_summary = None
    for k in sorted_keys:
        summary = _summarize_sessions(buckets[k])
        entry = {"period": k, **summary}
        if prev_summary:
            entry["error_rate_delta"] = round(
                summary["overall_error_rate"] - prev_summary["overall_error_rate"], 3)
            entry["session_count_delta"] = summary["session_count"] - prev_summary["session_count"]
            entry["tool_calls_delta"] = summary["total_tool_calls"] - prev_summary["total_tool_calls"]
        timeline.append(entry)
        prev_summary = summary

    # Direction summary
    if len(timeline) >= 2:
        first = timeline[0]
        last = timeline[-1]
        direction = {
            "error_rate_trend": "improving" if last["overall_error_rate"] < first["overall_error_rate"]
                              else ("worsening" if last["overall_error_rate"] > first["overall_error_rate"]
                              else "stable"),
            "first_period": first["period"],
            "last_period": last["period"],
            "first_error_rate": first["overall_error_rate"],
            "last_error_rate": last["overall_error_rate"],
        }
    else:
        direction = {"note": "Only one period with data — no trend direction yet"}

    print(json.dumps({
        "unit": args.unit,
        "periods_analyzed": len(timeline),
        "direction": direction,
        "timeline": timeline,
    }, indent=2, ensure_ascii=False))


def cmd_by_theme(args):
    """Aggregate stats by project or tag."""
    conn = _connect()
    rows = _load_sessions(conn)
    conn.close()

    groups = defaultdict(list)
    for row in rows:
        if args.group_by == "project":
            key = row[9] or "unknown"  # row[9] = project_name
            groups[key].append(row)
        elif args.group_by == "tag":
            try:
                tags = json.loads(row[10] or "[]")  # row[10] = tags
            except (json.JSONDecodeError, TypeError):
                tags = []
            if not tags:
                tags = ["untagged"]
            for tag in tags:
                groups[tag].append(row)
        elif args.group_by == "agent":
            key = row[11] or "unknown"  # row[11] = agent
            groups[key].append(row)

    result = {}
    for key, recs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        result[key] = _summarize_sessions(recs)

    print(json.dumps({
        "group_by": args.group_by,
        "total_groups": len(result),
        "groups": result,
    }, indent=2, ensure_ascii=False))


def cmd_regressions(args):
    """Flag tools whose error rate is trending upward across recent periods."""
    conn = _connect()
    rows = _load_sessions(conn)
    conn.close()

    # Bucket by period
    buckets = defaultdict(list)
    for row in rows:
        dt = _parse_session_date(row[1])
        if dt:
            buckets[_period_key(dt, args.unit)].append(row)

    sorted_keys = sorted(buckets.keys())
    if len(sorted_keys) < 2:
        print(json.dumps({"note": "Not enough periods with data to detect a trend. "
                                   "Need at least 2 periods — keep using sessions and re-indexing."}))
        return

    # Compute per-period tool error rates
    per_period_rates = {}
    for k in sorted_keys:
        summary = _summarize_sessions(buckets[k])
        per_period_rates[k] = summary["tool_error_rate"]

    # Collect all tools
    all_tools = set()
    for rates in per_period_rates.values():
        all_tools.update(rates.keys())

    # Find regressions (tools getting worse)
    recent_keys = sorted_keys[-args.lookback:]
    regressions = []
    for tool in all_tools:
        series = [per_period_rates[k].get(tool) for k in recent_keys]
        present = [(i, v) for i, v in enumerate(series) if v is not None]
        if len(present) < 2:
            continue
        first_i, first_v = present[0]
        last_i, last_v = present[-1]
        if last_v > first_v and (last_v - first_v) >= 0.1:
            regressions.append({
                "tool": tool,
                "period_start": recent_keys[first_i],
                "rate_start": first_v,
                "period_end": recent_keys[last_i],
                "rate_end": last_v,
                "increase": round(last_v - first_v, 3),
            })

    regressions.sort(key=lambda r: -r["increase"])

    # Also find improvements (tools getting better)
    improvements = []
    for tool in all_tools:
        series = [per_period_rates[k].get(tool) for k in recent_keys]
        present = [(i, v) for i, v in enumerate(series) if v is not None]
        if len(present) < 2:
            continue
        first_i, first_v = present[0]
        last_i, last_v = present[-1]
        if first_v > last_v and (first_v - last_v) >= 0.1:
            improvements.append({
                "tool": tool,
                "period_start": recent_keys[first_i],
                "rate_start": first_v,
                "period_end": recent_keys[last_i],
                "rate_end": last_v,
                "decrease": round(first_v - last_v, 3),
            })

    improvements.sort(key=lambda r: -r["decrease"])

    note = ("Tools not listed here either had stable/improving error rates, "
            "or too little data to judge") if regressions else \
           ("No tool showed a meaningful (>=10pt) error-rate increase "
            "across the examined periods")

    print(json.dumps({
        "unit": args.unit,
        "periods_examined": recent_keys,
        "regressions": regressions,
        "improvements": improvements,
        "note": note,
    }, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="Trend analysis over the session-digger index")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("period-over-period",
                        help="Week/month-over-month comparison")
    p1.add_argument("--unit", choices=["week", "month"], default="week")
    p1.add_argument("--lookback", type=int, default=8,
                    help="Number of periods to look back (default: 8)")
    p1.set_defaults(func=cmd_period_over_period)

    p2 = sub.add_parser("by-theme",
                        help="Aggregate by project, tag, or agent")
    p2.add_argument("--group-by", choices=["project", "tag", "agent"],
                    default="project")
    p2.set_defaults(func=cmd_by_theme)

    p3 = sub.add_parser("regressions",
                        help="Detect tools whose error rate is trending upward")
    p3.add_argument("--unit", choices=["week", "month"], default="week")
    p3.add_argument("--lookback", type=int, default=8)
    p3.set_defaults(func=cmd_regressions)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
