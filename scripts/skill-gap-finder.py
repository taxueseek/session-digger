#!/usr/bin/env python3
"""
skill-gap-finder.py — Mine the session index for recurring pain points and
draft reviewable SKILL.md improvement proposals.

Philosophy: this NEVER auto-edits a skill file. It always produces a
human-reviewable proposal (problem + evidence + suggested rule). The person
decides whether to apply it. A single bad session is noise; only recurring
patterns across multiple sessions justify a permanent rule change.

Architecture: This is Layer 3 (DECISION) in the four-layer model:
  Layer 0: PARSE   — echolib (raw transcript → stats)
  Layer 1: INDEX   — index-builder.py (stats → SQLite persistent storage)
  Layer 2: TREND   — trend-engine.py (index → time-sliced aggregation)
  Layer 3: DECISION — THIS FILE (patterns → SKILL.md proposals)

This is the only layer that makes a judgment call ("this is a real pattern
worth acting on") — and it always surfaces evidence + a human-reviewable
proposal rather than silently deciding for the user.

Usage:
    python skill-gap-finder.py analyze --min-occurrences 3
    python skill-gap-finder.py analyze --min-occurrences 3 --skills-dir ~/.claude/skills
    python skill-gap-finder.py analyze --min-occurrences 5 --since 2026-06-01

Pipeline:
  1. Load the session index from SQLite.
  2. Mine recurring pain points:
     - tools with sustained high error rates (not just one bad session)
     - flags that recur across multiple sessions (high error rate, long
       conversation, retry loop) with the same signature
     - projects/tags with disproportionately worse stats than the overall average
  3. For each pain point, look for an existing skill whose domain plausibly
     covers it (best-effort keyword match against installed SKILL.md
     descriptions — this is a pointer, not a guarantee of relevance).
  4. Emit a structured proposal: problem, evidence (session count, ids/paths),
     suggested SKILL.md addition, and which existing skill it'd attach to
     (or "no matching skill — consider a new one").
"""

from _common import read_json, write_json, read_text, json_out
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from index_builder._schema import DB_PATH  # 单一真源：~/.claude/.session-digger/index.db 或 $SESSION_DIGGER_DATA_DIR

# Default skills search directories
DEFAULT_SKILLS_DIRS = [
    str(Path.home() / ".claude" / "skills"),
    str(Path.home() / ".agents" / "skills"),
    str(Path.home() / ".grok" / "skills"),
]

# High-frequency host tools: volume alone is not a retry loop signal.
BASELINE_TOOLS = {
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "read_file", "search_replace", "run_terminal_command", "list_dir",
    "grep", "glob", "TodoWrite", "todo_write",
}

TOOL_ALIASES = {
    "webfetch": "web_fetch",
    "WebFetch": "web_fetch",
    "web-fetch": "web_fetch",
    "WebSearch": "web_search",
    "web-search": "web_search",
    "Bash": "Bash",
    "run_terminal_command": "run_terminal_command",
}


def _normalize_tool(name):
    if not name:
        return name
    return TOOL_ALIASES.get(name, TOOL_ALIASES.get(name.lower(), name))


@dataclass(slots=True, frozen=True)
class _RedactCtx:
    """Privacy redaction context. Thread-safe immutable value object.

    Replaces the module-level `global _REDACT` state.
    - enabled=True (default): redact personal identifiers in paths / names.
    - enabled=False (--include-paths on cmdline): keep raw path in evidence.
    While enabled, _INCLUDE_REDACTED_PATHS still emits the *redacted* form
    (a path with user identity stripped) instead of omitting it entirely.
    """
    enabled: bool = True
    include_when_enabled: bool = False  # maps old _INCLUDE_REDACTED_PATHS


def _redact_path(path, ctx):
    """Privacy-safe path for evidence. Default strips user identity everywhere.

    Handles absolute paths, Claude dash-encoding (-Users-name-...), and
    URL-encoded Grok segments (%2FUsers%2Fname%2F...).
    """
    if not path:
        return None
    if not ctx.enabled:
        return str(path)
    p = str(path)
    home = str(Path.home())
    home_name = Path.home().name
    if p.startswith(home + "/") or p == home:
        p = "~" + p[len(home):]
    # Absolute /Users|/home
    p = re.sub(r"(^|/)(Users|home)/[^/]+", r"\1\2/<user>", p)
    # Claude project dir encoding: -Users-<name>-Documents-...
    if home_name:
        p = p.replace(f"-Users-{home_name}", "-Users-<user>")
        p = p.replace(f"-home-{home_name}", "-home-<user>")
        p = p.replace(home_name, "<user>")
    # URL-encoded user home fragments
    p = re.sub(r"%2FUsers%2F[^%]+%2F", "%2FUsers%2F%3Cuser%3E%2F", p, flags=re.I)
    p = re.sub(r"%2Fhome%2F[^%]+%2F", "%2Fhome%2F%3Cuser%3E%2F", p, flags=re.I)
    # Keep only agent-storage tail
    for marker in ("/.claude/", "/.grok/", "/.codex/", "/.kimi", "/.zcode/", "/.agents/", "~/.claude/", "~/.grok/"):
        i = p.find(marker)
        if i >= 0:
            return "…" + p[i:]
    # Fall back: basename only
    return "…" + "/" + Path(p).name


def _evidence_item(sid, path, ctx, **extra):
    """Evidence row. By default omit filesystem paths (id is enough to re-find).

    ctx.enabled:
      False → raw path in source_path (user opted in with --include-paths).
      True  → omit; unless ctx.include_when_enabled, then emit redacted path.
    """
    item = {"id": sid}
    if path is not None and not ctx.enabled:
        item["source_path"] = str(path)
    elif path is not None and ctx.include_when_enabled:
        item["source_path"] = _redact_path(path, ctx)
    item.update(extra)
    return item


def _redact_project(name, ctx):
    if not name or not ctx.enabled:
        return name
    home_name = Path.home().name
    n = str(name)
    if home_name:
        n = n.replace(home_name, "<user>")
    n = re.sub(r"(Users|home)/[^/]+", r"\1/<user>", n)
    # Drop personal workspace folder names; keep last path segment only when long
    if "/" in n or n.startswith("-"):
        n = Path(n.replace("\\", "/")).name or n
    return n


def _connect():
    if not DB_PATH.exists():
        print(json.dumps({"error": "Index not built. Run: index-builder.py build"}))
        sys.exit(1)
    return sqlite3.connect(str(DB_PATH))


def _find_installed_skills(skills_dirs):
    """Return [{name, description, path}] for every SKILL.md found."""
    skills = []
    for skills_dir in skills_dirs:
        if not skills_dir or not os.path.isdir(skills_dir):
            continue
        for root, _, files in os.walk(skills_dir):
            for fn in files:
                if fn == "SKILL.md":
                    path = os.path.join(root, fn)
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            text = f.read()
                    except Exception:
                        continue
                    name_m = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
                    # Description can be multi-line with | or single line
                    desc_m = re.search(r"^description:\s*(.+?)(?=\n\w|\n---|\Z)",
                                       text, re.MULTILINE | re.DOTALL)
                    desc = ""
                    if desc_m:
                        desc = desc_m.group(1).strip()
                        if desc.startswith("|"):
                            desc = desc[1:].strip()
                    skills.append({
                        "name": name_m.group(1).strip() if name_m else os.path.basename(root),
                        "description": desc[:300] if desc else "",
                        "path": path,
                    })
    return skills


def _best_matching_skill(keywords, skills):
    """Best-effort keyword overlap between pain point terms and skill descriptions."""
    best = (None, 0)
    for skill in skills:
        haystack = (skill["name"] + " " + skill["description"]).lower()
        score = sum(1 for kw in keywords if kw.lower() in haystack)
        if score > best[1]:
            best = (skill, score)
    return best


def _mine_tool_error_patterns(rows, min_occurrences, ctx):
    """Find tools that show high error rates in at least N sessions."""
    # rows: (id, jsonl_path, tool_calls, errors, tool_usage_json, tool_errors_json,
    #         flags_json, project_name, tags, created)
    tool_session_rates = defaultdict(list)
    for row in rows:
        session_id = row[0]
        jsonl_path = row[1]
        try:
            tu = json.loads(row[4] or "{}")
            te = json.loads(row[5] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        # normalize + merge error counts under canonical tool names
        norm_calls = defaultdict(int)
        norm_errs = defaultdict(int)
        for tool, calls in tu.items():
            if not calls:
                continue
            nt = _normalize_tool(tool)
            norm_calls[nt] += calls
            norm_errs[nt] += te.get(tool, 0)
            # also pick errors stored under already-normalized keys
            if tool != nt:
                norm_errs[nt] += te.get(nt, 0)
        for tool, calls in norm_calls.items():
            rate = norm_errs.get(tool, 0) / calls if calls else 0
            tool_session_rates[tool].append((session_id, jsonl_path, rate))

    patterns = []
    for tool, sessions in tool_session_rates.items():
        bad_sessions = [(sid, path, rate) for sid, path, rate in sessions if rate >= 0.25]
        if len(bad_sessions) >= min_occurrences:
            patterns.append({
                "type": "recurring_tool_errors",
                "tool": tool,
                "occurrence_count": len(bad_sessions),
                "total_sessions_using_tool": len(sessions),
                "evidence_sessions": [
                    _evidence_item(sid, path, ctx, error_rate=round(rate, 2))
                    for sid, path, rate in bad_sessions[:10]
                ],
                "keywords": [tool, "error", "failure", "retry"],
            })
    return patterns


def _mine_recurring_flags(rows, min_occurrences, ctx):
    """Find stats-engine flags whose category recurs across sessions."""
    flag_categories = {
        "high_error_rate": re.compile(r"High overall tool error rate"),
        "long_conversation": re.compile(r"Long conversation"),
        "retry_loop": re.compile(r"called \d+ times"),
    }
    category_sessions = defaultdict(list)
    for row in rows:
        session_id = row[0]
        jsonl_path = row[1]
        try:
            flags = json.loads(row[6] or "[]")
        except (json.JSONDecodeError, TypeError):
            flags = []
        for flag in flags:
            if not isinstance(flag, str):
                continue
            for cat, pat in flag_categories.items():
                if not pat.search(flag):
                    continue
                # High call counts on baseline tools are normal agent traffic, not retry loops.
                if cat == "retry_loop":
                    m = re.search(r"'([^']+)' called \d+ times", flag)
                    tool = m.group(1) if m else ""
                    if _normalize_tool(tool) in BASELINE_TOOLS or tool in BASELINE_TOOLS:
                        continue
                category_sessions[cat].append(
                    _evidence_item(session_id, jsonl_path, ctx, flag_text=flag)
                )

    keyword_map = {
        "high_error_rate": ["error", "failure", "retry", "debugging"],
        "long_conversation": ["scope", "planning", "task breakdown", "long conversation"],
        "retry_loop": ["retry", "loop", "repeated", "inefficient"],
    }
    patterns = []
    for cat, sessions in category_sessions.items():
        if len(sessions) >= min_occurrences:
            patterns.append({
                "type": f"recurring_flag_{cat}",
                "occurrence_count": len(sessions),
                "evidence_sessions": sessions[:10],
                "keywords": keyword_map.get(cat, [cat]),
            })
    return patterns


def _mine_project_outliers(rows, min_occurrences, ctx):
    """Find projects whose average error rate is notably worse than overall."""
    by_project = defaultdict(list)
    for row in rows:
        proj = row[7] or "unknown"
        tool_calls = row[2] or 0
        errors = row[3] or 0
        rate = errors / tool_calls if tool_calls > 0 else 0
        by_project[proj].append({
            "id": row[0], "source_path": row[1],
            "tool_calls": tool_calls, "errors": errors, "error_rate": rate,
        })

    if not by_project:
        return []

    # Overall average error rate (weighted by tool calls)
    total_calls = sum(r["tool_calls"] for recs in by_project.values() for r in recs)
    total_errors = sum(r["errors"] for recs in by_project.values() for r in recs)
    overall_rate = total_errors / total_calls if total_calls > 0 else 0

    patterns = []
    for proj, recs in by_project.items():
        if len(recs) < min_occurrences:
            continue
        proj_calls = sum(r["tool_calls"] for r in recs)
        proj_errors = sum(r["errors"] for r in recs)
        proj_rate = proj_errors / proj_calls if proj_calls > 0 else 0
        if proj_rate >= overall_rate + 0.1 and proj_rate > 0.15:
            patterns.append({
                "type": "project_outlier",
                "project": proj,
                "session_count": len(recs),
                "project_error_rate": round(proj_rate, 3),
                "overall_error_rate": round(overall_rate, 3),
                "evidence_sessions": [
                    _evidence_item(r["id"], r["source_path"], ctx)
                    for r in recs[:10]
                ],
                "keywords": [proj],
            })
    return patterns


def _draft_proposal(pattern, skills, ctx):
    """Turn a mined pattern into a human-reviewable proposal."""
    matched_skill, score = _best_matching_skill(pattern["keywords"], skills)

    if pattern["type"] == "recurring_tool_errors":
        problem = (f"The '{pattern['tool']}' tool shows a high error rate "
                   f"(>=25%) in {pattern['occurrence_count']} of "
                   f"{pattern['total_sessions_using_tool']} sessions that used it.")
        suggested_rule = (f"Before using {pattern['tool']}, check for common failure "
                          f"modes. Consider adding a pre-flight check or a documented "
                          f"gotcha for '{pattern['tool']}' to the relevant skill.")
    elif pattern["type"] == "recurring_flag_high_error_rate":
        problem = (f"{pattern['occurrence_count']} sessions were flagged for high overall "
                   f"tool error rates — a recurring pattern, not an isolated incident.")
        suggested_rule = ("Consider adding an explicit troubleshooting/verification step "
                          "before proceeding after a tool error, rather than immediate retry.")
    elif pattern["type"] == "recurring_flag_long_conversation":
        problem = (f"{pattern['occurrence_count']} sessions ran unusually long "
                   f"(40+ turns) — may indicate tasks that should be scoped or "
                   f"broken down earlier.")
        suggested_rule = ("Consider adding upfront task-scoping guidance: break large asks "
                          "into sub-tasks with checkpoints before starting implementation.")
    elif pattern["type"] == "recurring_flag_retry_loop":
        problem = (f"{pattern['occurrence_count']} sessions showed one tool being "
                   f"called an unusually high number of times — possible inefficient "
                   f"retry loops.")
        suggested_rule = ("Consider adding a rule: after 2-3 consecutive failures of the "
                          "same tool call, stop and reassess approach rather than retrying "
                          "with minor variations.")
    elif pattern["type"] == "project_outlier":
        project_name = _redact_project(pattern["project"], ctx)
        problem = (f"Project '{project_name}' has a notably higher error rate "
                   f"({pattern['project_error_rate']:.0%}) than the overall average "
                   f"({pattern['overall_error_rate']:.0%}) across {pattern['session_count']} sessions.")
        suggested_rule = (f"Consider documenting project-specific conventions/gotchas for "
                          f"'{project_name}' — e.g. as a references/ file in the relevant "
                          f"skill, or a CLAUDE.md note in the project itself.")
    else:
        problem = "Unrecognized pattern type."
        suggested_rule = "Manual review needed."

    return {
        "problem": problem,
        "evidence_count": pattern.get("occurrence_count") or pattern.get("session_count"),
        "evidence_sessions": pattern.get("evidence_sessions", []),
        "matched_skill": {
            "name": matched_skill["name"],
            "path": _redact_path(matched_skill["path"], ctx),
            "match_confidence": "low" if score <= 1 else ("medium" if score == 2 else "high"),
        } if matched_skill else None,
        "suggested_skill_md_addition": suggested_rule,
        "note": ("This is a proposal, not an automatic edit. Review the evidence sessions "
                 "before adding this to any SKILL.md — confirm the root cause first."),
    }


def cmd_analyze(args):
    # Privacy context: immutable value passed down the call stack.
    # Default redacts user identity in paths/names; --include-paths disables.
    ctx = _RedactCtx(
        enabled=not getattr(args, "include_paths", False),
    )

    conn = _connect()

    query = """
        SELECT id, jsonl_path, tool_calls, errors, tool_usage_json, tool_errors_json,
               flags_json, project_name, tags, created
        FROM sessions
        WHERE tool_calls IS NOT NULL
    """
    params = []
    if args.since:
        query += " AND created >= ?"
        params.append(args.since)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if len(rows) < args.min_occurrences:
        print(json.dumps({
            "note": f"Only {len(rows)} indexed sessions available — need at least "
                    f"{args.min_occurrences} to distinguish a recurring pattern from noise. "
                    f"Keep using sessions and re-indexing to build up history.",
            "proposals": [],
        }, indent=2, ensure_ascii=False))
        return

    # Mine patterns
    patterns = []
    patterns += _mine_tool_error_patterns(rows, args.min_occurrences, ctx)
    patterns += _mine_recurring_flags(rows, args.min_occurrences, ctx)
    patterns += _mine_project_outliers(rows, args.min_occurrences, ctx)

    # Find installed skills
    skills_dirs = args.skills_dir if args.skills_dir else DEFAULT_SKILLS_DIRS
    skills = _find_installed_skills(skills_dirs)

    # Draft proposals
    proposals = [_draft_proposal(p, skills, ctx) for p in patterns]
    proposals.sort(key=lambda p: -(p["evidence_count"] or 0))

    print(json.dumps({
        "sessions_analyzed": len(rows),
        "patterns_found": len(patterns),
        "skills_scanned": len(skills),
        "proposals": proposals,
    }, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(
        description="Mine the session index for recurring patterns and draft skill improvement proposals")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("analyze",
                        help="Analyze the index for recurring pain points")
    p1.add_argument("--min-occurrences", type=int, default=3,
                    help="Minimum sessions a pattern must appear in (default: 3)")
    p1.add_argument("--skills-dir", action="append", default=None,
                    help="Directory to search for SKILL.md files (can repeat; "
                         "defaults to ~/.claude/skills and ~/.agents/skills)")
    p1.add_argument("--since", default=None,
                    help="Only consider sessions created on/after this ISO date")
    p1.add_argument("--include-paths", action="store_true",
                    help="Include absolute filesystem paths in evidence (default: redacted)")
    p1.set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
