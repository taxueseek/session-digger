#!/usr/bin/env python3
"""Check dimcode main vs subagent split feasibility."""
import sqlite3
import json
import os
from pathlib import Path

INDEX_DB = Path.home() / ".claude" / ".session-digger" / "index.db"
DIMCODE_DB = Path.home() / ".dimcode" / "v2" / "dimcode.sqlite"

# ── 1. Query index.db for dimcode sessions ──────────────────────────
print("=" * 70)
print("1. INDEX.DB — dimcode sessions (main vs subagent)")
print("=" * 70)

if not INDEX_DB.exists():
    print(f"INDEX_DB not found: {INDEX_DB}")
else:
    conn = sqlite3.connect(str(INDEX_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # All dimcode sessions
    cur.execute("SELECT id, cache_hit_rate, total_tokens FROM sessions WHERE agent='dimcode'")
    rows = cur.fetchall()

    main_rows = [r for r in rows if "sess_" in r["id"] and "subagent_" not in r["id"]]
    sub_rows = [r for r in rows if "subagent_" in r["id"]]
    other_rows = [r for r in rows if "subagent_" not in r["id"] and "sess_" not in r["id"]]

    def summarize(label, subset):
        n = len(subset)
        rate_gt0 = [r for r in subset if r["cache_hit_rate"] is not None and r["cache_hit_rate"] > 0]
        n_gt0 = len(rate_gt0)
        rates = [r["cache_hit_rate"] for r in rate_gt0]
        mean_rate = sum(rates) / len(rates) if rates else 0.0
        tot_tokens = sum(r["total_tokens"] or 0 for r in subset)
        print(f"\n  {label}:")
        print(f"    n={n}  n(rate>0)={n_gt0}  mean_rate={mean_rate:.4f}  total_tokens={tot_tokens}")
        # Show some sample IDs
        if subset:
            print(f"    sample IDs (first 5): {[r['id'] for r in subset[:5]]}")

    summarize("dimcode ALL", rows)
    summarize("main (sess_*)", main_rows)
    summarize("subagent (subagent_*)", sub_rows)
    if other_rows:
        summarize("other", other_rows)

    conn.close()

# ── 2. Check DimCode SQLite schema ───────────────────────────────────
print("\n" + "=" * 70)
print("2. DIMCODE SQLITE — schema (sessions table)")
print("=" * 70)

if not DIMCODE_DB.exists():
    print(f"DIMCODE_DB not found: {DIMCODE_DB}")
else:
    conn = sqlite3.connect(f"file:{DIMCODE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Check sessions table columns
    cur.execute("PRAGMA table_info(sessions)")
    cols = cur.fetchall()
    print("\n  sessions columns:")
    for c in cols:
        print(f"    {c['name']:25s} {c['type']:15s} nullable={not c['notnull']}")

    # Check for parent_id / relation fields
    cur.execute("SELECT COUNT(*) as cnt FROM sessions")
    total = cur.fetchone()["cnt"]
    print(f"\n  total sessions: {total}")

    # Check sessionId patterns
    cur.execute("SELECT sessionId, title FROM sessions ORDER BY sessionId LIMIT 40")
    sample_sids = cur.fetchall()
    print(f"\n  sample sessionIds (first 40):")
    for r in sample_sids:
        print(f"    {r['sessionId']:60s} title={r['title'][:60] if r['title'] else '(none)'}")

    # Count by prefix pattern
    cur.execute("""
        SELECT
            CASE
                WHEN sessionId LIKE 'sess_subagent_agent_%' THEN 'subagent (sess_subagent_agent_)'
                WHEN sessionId LIKE 'sess_subagent_%' THEN 'subagent (sess_subagent_)'
                WHEN sessionId LIKE 'subagent_%' THEN 'subagent (subagent_)'
                WHEN sessionId LIKE 'sess_%' THEN 'main (sess_)'
                ELSE 'other'
            END as cat,
            COUNT(*) as cnt
        FROM sessions
        GROUP BY cat
        ORDER BY cnt DESC
    """)
    for r in cur.fetchall():
        print(f"    {r['cat']:45s}: {r['cnt']}")

    # Check if there are any parent/relation columns
    # Try common names
    for col_name in ["parent_id", "parentId", "parentSessionId", "relation", "task_type", "type"]:
        try:
            cur.execute(f"SELECT COUNT(*) as cnt FROM sessions WHERE {col_name} IS NOT NULL")
            cnt = cur.fetchone()["cnt"]
            if cnt > 0:
                print(f"\n  {col_name} non-null count: {cnt}")
        except sqlite3.OperationalError:
            pass  # column doesn't exist

    # Check all table names
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r["name"] for r in cur.fetchall()]
    print(f"\n  all tables: {tables}")

    # Check messages table for subagent links
    if "messages" in tables:
        cur.execute("PRAGMA table_info(messages)")
        msg_cols = cur.fetchall()
        print(f"\n  messages columns: {[c['name'] for c in msg_cols]}")
        # Check if messages reference parent sessions
        cur.execute("SELECT COUNT(DISTINCT sessionId) FROM messages")
        msg_sessions = cur.fetchone()[0]
        print(f"  messages distinct sessionIds: {msg_sessions}")

    conn.close()

# ── 3. Summary ──────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("3. PRELIMINARY FINDINGS")
print("=" * 70)
