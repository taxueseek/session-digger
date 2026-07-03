#!/usr/bin/env bash
# apply-rules.sh — Format candidate rules for interactive review and write to targets.
# Helper for /apply command. Zero API calls.
#
# Usage:
#   apply-rules.sh format <session.jsonl>           # Output numbered candidate rules (text)
#   apply-rules.sh dump-json <session.jsonl>        # Output suggestions as JSON array
#   apply-rules.sh append <rule_text> [--target FILE]  # Append rule to target file
#   apply-rules.sh memory <rule_text> [category] [--project PATH]  # Write to memory/
#
# v1.0: Inspired by session-recall --apply interactive review flow.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ES_SCRIPT_DIR="$SCRIPT_DIR"

cmd="${1:-}"
shift || true

case "$cmd" in
  format)
    session="${1:-}"
    [[ -z "$session" ]] && { echo "ERROR: Missing session file" >&2; exit 1; }
    [[ ! -f "$session" ]] && { echo "ERROR: File not found: $session" >&2; exit 1; }
    ES_FILE="$session" python3 << 'PYEOF'
import json, os, sys
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib
from collections import Counter

file_path = os.environ["ES_FILE"]
fmt = os.environ.get("ANALYZE_FORMAT", "text")

stats = echolib.session_stats(file_path)

# Retry detection
tool_sequences = []
for t in echolib.extract_tools(file_path):
    tool_sequences.append((t["name"], t["key_input"], t["status"], t["timestamp"]))

import re
retry_patterns = []
i = 0
while i < len(tool_sequences):
    name, key, status, ts = tool_sequences[i]
    if status == "error":
        run_len = 1
        j = i + 1
        while j < len(tool_sequences) and tool_sequences[j][0] == name and tool_sequences[j][2] == "error":
            run_len += 1
            j += 1
        if run_len >= 2:
            retry_patterns.append({"tool": name, "count": run_len, "sample_input": key[:120], "timestamp": ts})
        i = j
    else:
        i += 1

# Rules
suggestions = []
for rp in retry_patterns:
    suggestions.append({
        "rule": "When {} fails {} times in a row, switch approach. Do not keep retrying the same command.".format(rp["tool"], rp["count"]),
        "category": "retry",
        "evidence": "Retried {}x at {}: {}".format(rp["count"], rp["timestamp"], rp["sample_input"][:80]),
    })

# User corrections (simplified sample)
correction_patterns = [
    r"(?i)\bdon't\b", r"(?i)\bstop\b", r"(?i)\bwrong\b", r"(?i)\bactually\b",
    r"(?i)不对", r"(?i)别", r"(?i)应该", r"(?i)重新",
]
seen = set()
for rec in echolib.extract_messages(file_path, role="user"):
    text = rec["text"]
    if len(text) < 20 or len(text) > 200:
        continue
    for pat in correction_patterns:
        if re.search(pat, text):
            key = text[:40]
            if key not in seen:
                seen.add(key)
                suggestions.append({
                    "rule": text.strip(),
                    "category": "correction",
                    "evidence": "User message at {}".format(rec["timestamp"][:19]),
                })
            break

# Output
print("CANDIDATE RULES FROM: {}\n".format(file_path))
for idx, s in enumerate(suggestions, 1):
    print("  ({}) [{}] {}".format(idx, s["category"], s["rule"]))
    print("      Evidence: {}".format(s["evidence"]))
    print()

if not suggestions:
    print("  No patterns detected.")
PYEOF
    ;;

  dump-json)
    session="${1:-}"
    [[ -z "$session" ]] && { echo "ERROR: Missing session file" >&2; exit 1; }
    ES_FILE="$session" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 << 'PYEOF'
import json, os, sys, re
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

file_path = os.environ["ES_FILE"]
tool_sequences = [(t["name"], t["key_input"], t["status"], t["timestamp"])
                  for t in echolib.extract_tools(file_path)]

retry_patterns = []
i = 0
while i < len(tool_sequences):
    name, key, status, ts = tool_sequences[i]
    if status == "error":
        run_len = 1
        j = i + 1
        while j < len(tool_sequences) and tool_sequences[j][0] == name and tool_sequences[j][2] == "error":
            run_len += 1
            j += 1
        if run_len >= 2:
            retry_patterns.append({"tool": name, "count": run_len, "sample_input": key[:120], "timestamp": ts})
        i = j
    else:
        i += 1

suggestions = []
for rp in retry_patterns:
    suggestions.append({
        "rule": "When {} fails {} times in a row, switch approach. Do not keep retrying.".format(rp["tool"], rp["count"]),
        "category": "retry",
        "evidence": "Retried {}x at {}".format(rp["count"], rp["timestamp"]),
        "target": "CLAUDE.md",
    })

correction_patterns = [r"(?i)\bdon't\b", r"(?i)\bstop\b", r"(?i)\bwrong\b",
                       r"(?i)\bactually\b", r"(?i)不对", r"(?i)别", r"(?i)应该"]
seen = set()
for rec in echolib.extract_messages(file_path, role="user"):
    text = rec["text"]
    if len(text) < 20 or len(text) > 200:
        continue
    for pat in correction_patterns:
        if re.search(pat, text):
            key = text[:40]
            if key not in seen:
                seen.add(key)
                suggestions.append({
                    "rule": text.strip(),
                    "category": "correction",
                    "evidence": "User message at {}".format(rec["timestamp"][:19]),
                    "target": "CLAUDE.md",
                })
            break

print(json.dumps(suggestions, ensure_ascii=False, indent=2))
PYEOF
    ;;

  append)
    rule="${1:-}"
    [[ -z "$rule" ]] && { echo "ERROR: Missing rule text" >&2; exit 1; }
    target="${2:-CLAUDE.md}"
    [[ "$target" == "--target" ]] && { target="${3:-CLAUDE.md}"; }

    # Create if missing
    if [[ ! -f "$target" ]]; then
      echo "# Project Rules" > "$target"
      echo "" >> "$target"
    fi

    # Check if section exists
    if ! grep -q "## Session Learned Rules" "$target" 2>/dev/null; then
      echo "" >> "$target"
      echo "## Session Learned Rules" >> "$target"
      echo "" >> "$target"
    fi

    # Append rule (before any next ## heading, or at end of section)
    timestamp="$(date +%Y-%m-%d)"
    rule_line="- $rule"
    if grep -q "<!-- session-digger:apply" "$target" 2>/dev/null; then
      # Insert after the last session-digger:apply comment line
      last_comment_line="$(grep -n "<!-- session-digger:apply" "$target" | tail -1 | cut -d: -f1)"
      sed -i '' "${last_comment_line}a\\
$rule_line" "$target" 2>/dev/null || {
        # Fallback: simple append to file
        echo "$rule_line" >> "$target"
      }
    else
      # Add comment + rule at end of section
      echo "<!-- session-digger:apply $timestamp -->" >> "$target"
      echo "$rule_line" >> "$target"
    fi

    echo "OK: appended rule to $target"
    ;;

  memory)
    rule="${1:-}"
    category="${2:-feedback}"
    [[ -z "$rule" ]] && { echo "ERROR: Missing rule text" >&2; exit 1; }

    # Resolve project
    project="${PWD}"
    for arg in "$@"; do
      if [[ "$arg" == "--project" ]]; then
        _next_proj=1
      elif [[ "${_next_proj:-0}" -eq 1 ]]; then
        project="$arg"
        _next_proj=0
      fi
    done

    mem_dir="$([[ -d "$project/memory" ]] && echo "$project/memory" || echo "$project/.claude/memory")"
    mkdir -p "$mem_dir"

    date_tag="$(date +%Y%m%d-%H%M%S)"
    out_file="$mem_dir/lessons-${date_tag}.md"

    cat > "$out_file" << MDEOF
---
name: "Session lesson ${date_tag}"
description: "Auto-extracted rule from session analysis"
type: ${category}
---

- ${rule}
MDEOF

    # Update MEMORY.md
    memo="$mem_dir/MORY.md"
    if [[ ! -f "$memo" ]]; then
      memo="$mem_dir/MEMORY.md"
    fi
    if [[ -f "$memo" ]]; then
      if ! grep -q "lessons-${date_tag}" "$memo" 2>/dev/null; then
        echo "![[lessons-${date_tag}.md]]" >> "$memo"
      fi
    fi

    echo "OK: written to $out_file"
    ;;

  *)
    echo "Usage: apply-rules.sh <format|dump-json|append|memory> [args]" >&2
    exit 1
    ;;
esac
