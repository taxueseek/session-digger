#!/usr/bin/env python3
"""
index-builder.py — Pre-compute SQLite index for fast session querying.

Creates/updates ~/.claude/.session-digger/index.db with:
  sessions      — session metadata + stats (single row per .jsonl)
  messages_fts  — FTS5 full-text index over all user/assistant messages
  topic_boundaries  — auto-detected topic segment boundaries per session

Usage:
  index-builder.py [--project PATH] [--rebuild] [--agent claude|grok|kimi|cross]

v1.1: Extended with rich stats (tool_usage, tool_errors, flags, duration,
     tags, outcome) for trend analysis and skill-gap detection.
v1.0: Architectural core for session-digger v0.7 speed tier.
     Eliminates per-command file scanning + repeated Python subprocess spawns.
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

DB_DIR = Path.home() / ".claude" / ".session-digger"
DB_PATH = DB_DIR / "index.db"

# FTS stemmer: simple for cross-language (Chinese needs external tokenizer,
# but FTS5's "unicode61" handles CJK as individual characters = usable)
FTS_TOKENIZER = "unicode61"


def _dispatch_session_stats(path):
    """Get stats via echolib's unified adapter dispatch."""
    return echolib.dispatch_session_stats(path)


def _dispatch_extract_messages(path, role="both", limit=0):
    """Extract messages via echolib's unified adapter dispatch."""
    return echolib.dispatch_extract_messages(path, role=role, limit=limit)


def _compute_rich_stats(path, base_stats):
    """Compute per-tool usage, errors, flags, and duration from a session.

    Returns a dict with: tool_usage, tool_errors, flags, duration_seconds,
    project_name.  This is the 'Layer 0' ground truth that feeds the
    persistent stats index (Layer 1) for trend/skill-gap analysis.
    """
    from collections import Counter

    tool_usage = Counter()
    tool_errors = Counter()

    try:
        for t in echolib.dispatch_extract_tools(path, limit=0):
            name = t.get("name", "unknown")
            tool_usage[name] += 1
            if t.get("status") == "error":
                tool_errors[name] += 1
    except Exception:
        pass

    # Generate flags (inspired by agent-transcript-analyzer's parse_claude_code.py)
    flags = []
    total_calls = sum(tool_usage.values())
    total_errors = sum(tool_errors.values())

    if total_calls > 0 and total_errors / total_calls > 0.25:
        pct = round(100 * total_errors / total_calls)
        flags.append(f"High overall tool error rate: {total_errors}/{total_calls} "
                      f"({pct}%) — worth checking for repeated failed approaches")

    msg_count = base_stats.get("user_messages", 0) + base_stats.get("assistant_messages", 0)
    if msg_count > 40:
        flags.append(f"Long conversation ({msg_count} turns) — consider whether task "
                     f"could have been scoped/split more efficiently")

    if tool_usage:
        top_tool, top_count = tool_usage.most_common(1)[0]
        if top_count > 15:
            flags.append(f"'{top_tool}' called {top_count} times — check for inefficient retry loops")

    # Duration
    duration_seconds = None
    started = base_stats.get("started", "")
    ended = base_stats.get("ended", "")
    if started and ended:
        try:
            from datetime import datetime
            t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            duration_seconds = (t1 - t0).total_seconds()
        except Exception:
            pass

    # Project name from path
    project_name = None
    try:
        project_name = os.path.basename(os.path.dirname(path))
        # Decode Claude encoded path: -Users-<user>-Documents-MyApp → MyApp
        if project_name.startswith("-"):
            parts = project_name.split("-")
            project_name = parts[-1] if parts else project_name
    except Exception:
        pass

    return {
        "tool_usage": dict(tool_usage),
        "tool_errors": dict(tool_errors),
        "flags": flags,
        "duration_seconds": duration_seconds,
        "project_name": project_name,
    }

def init_db(conn):
    """Create tables if missing, and migrate older schemas."""
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        project_path TEXT,
        agent TEXT,
        created TEXT,
        modified TEXT,
        message_count INTEGER,
        user_messages INTEGER,
        assistant_messages INTEGER,
        tool_calls INTEGER,
        errors INTEGER,
        compactions INTEGER,
        total_tokens INTEGER,
        branch TEXT,
        summary TEXT,
        first_prompt TEXT,
        jsonl_mtime REAL,
        indexed_at REAL,
        jsonl_path TEXT
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        session_id UNINDEXED,
        role UNINDEXED,
        timestamp UNINDEXED,
        text,
        tokenize='{FTS_TOKENIZER}'
    );

    CREATE TABLE IF NOT EXISTS topic_boundaries (
        session_id TEXT,
        message_index INTEGER,
        timestamp TEXT,
        topic_label TEXT,
        confidence REAL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    );

    CREATE TABLE IF NOT EXISTS index_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    # Migrate older databases: add columns if missing
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "jsonl_path" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN jsonl_path TEXT")
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN content_hash TEXT")
    # v0.8: rich stats columns for trend/skill-gap analysis
    if "tool_usage_json" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN tool_usage_json TEXT DEFAULT '{}'")
    if "tool_errors_json" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN tool_errors_json TEXT DEFAULT '{}'")
    if "flags_json" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN flags_json TEXT DEFAULT '[]'")
    if "duration_seconds" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN duration_seconds REAL")
    if "project_name" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN project_name TEXT")
    if "tags" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN tags TEXT DEFAULT '[]'")
    if "outcome" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN outcome TEXT")

    conn.commit()


def _file_fingerprint(jsonl_path):
    """Triple fingerprint: mtime + size + head hash.
    Upgraded from mtime-only to detect content changes that preserve mtime
    (e.g., git checkout, file copy with preserved timestamp).
    """
    try:
        st = os.stat(jsonl_path)
        size = st.st_size
        mtime = st.st_mtime
        # Hash first 4KB for fast change detection
        with open(jsonl_path, "rb") as f:
            head = f.read(4096)
        import hashlib
        content_hash = hashlib.md5(f"{size}:{head}".encode()).hexdigest()
        return mtime, content_hash
    except OSError:
        return None, None


def scan_sessions(agent_filter="cross"):
    """Yield (session_id, jsonl_path, agent_type) tuples.

    Dynamically scans ALL registered environments from echolib's ENV_REGISTRY
    and KNOWN_UNADAPTED, not just hardcoded paths. This ensures every new
    adapter is automatically picked up by the index builder.
    """
    entries = []

    # Merge registered and unadapted environments
    all_envs = {}
    all_envs.update(echolib.ENV_REGISTRY)
    all_envs.update(echolib.KNOWN_UNADAPTED)

    for env_id, env_info in all_envs.items():
        # Skip if filtering to a specific agent that isn't this one
        if agent_filter not in ("cross", env_id, "all"):
            continue

        root = Path(os.path.expanduser(env_info["root"]))
        if not root.exists():
            continue

        adapter_name = env_info.get("adapter", "universal")

        # Find all JSONL files under this environment's root
        try:
            jsonl_files = _find_jsonl_files(root, env_id)
        except Exception:
            continue

        for jf in jsonl_files:
            # Skip subagent files, backup files, events files
            if "subagents" in str(jf):
                continue
            if ".jsonl." in jf.name:
                continue
            if jf.name.endswith(".events.jsonl"):
                continue
            if jf.name == "backfill.jsonl":
                continue

            # Generate a session ID from the path
            session_id = _generate_session_id(jf, root, env_id)
            entries.append((session_id, str(jf), adapter_name))

    return entries


def _find_jsonl_files(root, env_id):
    """Find JSONL files for a specific environment.

    Different environments store sessions in different directory structures:
    - Claude: projects/<encoded-cwd>/*.jsonl
    - Grok: sessions/<session-id>/chat_history.jsonl
    - Kimi Code: sessions/<project>/<session>/agents/main/wire.jsonl
    - Codex: sessions/*.jsonl (rollout-*.jsonl)
    - WorkBuddy: projects/*/*.jsonl
    - Trae CN: memory/projects/*/*.jsonl
    - ZCode: agents/sess_*/agent_*/transcript.jsonl
    - DIM: memory/*/*.jsonl
    - Reasonix: sessions/*.jsonl
    - Others: scan recursively for *.jsonl
    """
    jsonl_files = []

    if env_id == "claude":
        for d in root.iterdir():
            if d.is_dir():
                for jf in d.glob("*.jsonl"):
                    jsonl_files.append(jf)
    elif env_id == "grok":
        # Grok stores sessions in nested dirs: <encoded-project>/<session-id>/chat_history.jsonl
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                chat_file = session_dir / "chat_history.jsonl"
                if chat_file.exists():
                    jsonl_files.append(chat_file)
    elif env_id == "kimi_code":
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                wire_file = session_dir / "agents" / "main" / "wire.jsonl"
                if wire_file.exists():
                    jsonl_files.append(wire_file)
    elif env_id == "zcode":
        for sess_dir in root.iterdir():
            if not sess_dir.is_dir() or not sess_dir.name.startswith("sess_"):
                continue
            for agent_dir in sess_dir.iterdir():
                if not agent_dir.is_dir():
                    continue
                transcript = agent_dir / "transcript.jsonl"
                if transcript.exists():
                    jsonl_files.append(transcript)
    elif env_id == "dim":
        for mem_dir in root.iterdir():
            if not mem_dir.is_dir():
                continue
            for date_dir in mem_dir.iterdir():
                if not date_dir.is_dir():
                    continue
                for jf in date_dir.glob("*.jsonl"):
                    jsonl_files.append(jf)
    else:
        # Generic: recursively find all .jsonl files
        jsonl_files = list(root.rglob("*.jsonl"))

    return jsonl_files


def _generate_session_id(jsonl_path, root, env_id):
    """Generate a unique session ID from a JSONL file path.

    Includes environment prefix to avoid collisions between different
    environments that may have same-named files (e.g. 'transcript' in
    both zcode and other tools).
    """
    if env_id == "kimi_code":
        # Use parent's parent (session dir) name
        base = jsonl_path.parent.parent.name.replace("session_", "")
    elif env_id == "grok":
        base = jsonl_path.parent.name
    elif env_id == "zcode":
        base = jsonl_path.parent.name  # agent_XXX
    else:
        base = jsonl_path.stem
    # Prefix with environment to ensure global uniqueness
    return f"{env_id}:{base}"


def detect_topic_boundaries(messages, min_gap_seconds=300):
    """
    Heuristic topic segmentation based on:
    1. Time gaps > min_gap_seconds
    2. Role shifts (user→assistant→user)
    3. Keyword overlaps between adjacent pairs

    Returns list of (index, timestamp, label, confidence)
    """
    if len(messages) < 3:
        return []

    boundaries = []

    for i in range(1, len(messages)):
        msg = messages[i]
        prev = messages[i - 1]

        # Time gap heuristic
        ts_cur = msg.get("timestamp", "")
        ts_prev = prev.get("timestamp", "")
        gap_score = 0.0
        if ts_cur and ts_prev:
            try:
                from datetime import datetime
                fmt = "%Y-%m-%dT%H:%M:%S"
                t_cur = datetime.strptime(ts_cur[:19], fmt)
                t_prev = datetime.strptime(ts_prev[:19], fmt)
                gap = (t_cur - t_prev).total_seconds()
                if gap > min_gap_seconds:
                    gap_score = min(gap / 3600, 1.0)
            except (ValueError, TypeError):
                pass

        # Content similarity (simple token overlap)
        text_cur = set(msg.get("text", "").lower().split())
        text_prev = set(prev.get("text", "").lower().split())
        if text_cur and text_prev:
            overlap = len(text_cur & text_prev) / max(len(text_cur), 1)
            content_score = 1.0 - overlap
        else:
            content_score = 0.5

        # Combined confidence
        confidence = 0.4 * gap_score + 0.6 * content_score

        if confidence > 0.5:
            boundaries.append((i, ts_cur, f"topic_{len(boundaries)+1}", round(confidence, 2)))

    return boundaries


def build_index(rebuild=False, agent_filter="cross"):
    DB_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)

    if rebuild:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM messages_fts")
        conn.execute("DELETE FROM topic_boundaries")
        conn.commit()

    entries = scan_sessions(agent_filter)
    indexed = 0
    skipped = 0
    errors = 0

    t_start = time.time()

    for session_id, jsonl_path, agent in entries:
        # Skip if content unchanged (fast path: mtime + size + head hash)
        mtime, content_hash = _file_fingerprint(jsonl_path)
        if mtime is None:
            errors += 1
            continue

        existing = conn.execute(
            "SELECT jsonl_mtime, content_hash FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()

        if existing and existing[0] == mtime and existing[1] == content_hash and not rebuild:
            skipped += 1
            continue

        try:
            stats = _dispatch_session_stats(jsonl_path)
        except Exception:
            errors += 1
            continue

        # Compute rich stats (per-tool usage, flags, duration)
        rich = _compute_rich_stats(jsonl_path, stats)

        # Preserve tags/outcome from existing record (don't overwrite on re-index)
        existing_tags = "[]"
        existing_outcome = None
        if existing:
            old_row = conn.execute(
                "SELECT tags, outcome FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if old_row:
                existing_tags = old_row[0] or "[]"
                existing_outcome = old_row[1]

        # Upsert session metadata (with rich stats)
        conn.execute("""
            INSERT OR REPLACE INTO sessions
            (id, project_path, agent, created, modified, message_count,
             user_messages, assistant_messages, tool_calls, errors,
             compactions, total_tokens, branch, summary, first_prompt,
             jsonl_mtime, indexed_at, jsonl_path, content_hash,
             tool_usage_json, tool_errors_json, flags_json, duration_seconds,
             project_name, tags, outcome)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session_id,
            str(Path(jsonl_path).parent),
            agent,
            stats.get("started", ""),
            stats.get("ended", ""),
            stats.get("user_messages", 0) + stats.get("assistant_messages", 0),
            stats.get("user_messages", 0),
            stats.get("assistant_messages", 0),
            stats.get("tool_calls", 0),
            stats.get("errors", 0),
            stats.get("compactions", 0),
            stats.get("total_tokens", 0),
            stats.get("branch", ""),
            stats.get("summary", ""),
            "",
            mtime,
            time.time(),
            jsonl_path,
            content_hash,
            json.dumps(rich["tool_usage"], ensure_ascii=False),
            json.dumps(rich["tool_errors"], ensure_ascii=False),
            json.dumps(rich["flags"], ensure_ascii=False),
            rich["duration_seconds"],
            rich["project_name"],
            existing_tags,
            existing_outcome,
        ))

        # Extract messages once — reused for FTS indexing and topic segmentation
        try:
            all_msgs = list(_dispatch_extract_messages(jsonl_path, role="both"))
        except Exception:
            all_msgs = []

        # Index messages into FTS (batch insert via executemany)
        if existing:
            conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))

        if all_msgs:
            fts_rows = [
                (session_id, m.get("role", ""), m.get("timestamp", ""), m.get("text", "")[:2000])
                for m in all_msgs
            ]
            try:
                conn.executemany(
                    "INSERT INTO messages_fts (session_id, role, timestamp, text) VALUES (?,?,?,?)",
                    fts_rows,
                )
            except Exception:
                pass

        # Topic segmentation (reuses all_msgs — no second parse)
        if existing:
            conn.execute("DELETE FROM topic_boundaries WHERE session_id = ?", (session_id,))

        if all_msgs:
            try:
                boundaries = detect_topic_boundaries(all_msgs)
                if boundaries:
                    boundary_rows = [
                        (session_id, idx, ts, label, conf)
                        for idx, ts, label, conf in boundaries
                    ]
                    conn.executemany(
                        "INSERT INTO topic_boundaries (session_id, message_index, timestamp, topic_label, confidence) VALUES (?,?,?,?,?)",
                        boundary_rows,
                    )
            except Exception:
                pass

        indexed += 1

    # Update meta
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?,?)",
        ("last_build", str(time.time()))
    )
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?,?)",
        ("total_sessions", str(indexed + skipped))
    )
    conn.commit()
    conn.close()

    elapsed = time.time() - t_start
    return {"indexed": indexed, "skipped": skipped, "errors": errors, "elapsed": round(elapsed, 2)}


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
