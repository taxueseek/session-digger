#!/usr/bin/env bash
# git-sessions.sh — Cluster git commits into work sessions based on time gaps
# Usage: git-sessions.sh [repo-path] [--since "14 days ago"] [--gap 3600]
#
# A "session" is a burst of commits with less than --gap seconds between them.
# Output format:
#   --- SESSION N (YYYY-MM-DD HH:MM, X commits) ---
#     HASH  SUBJECT
#     FILES: file1.ext, file2.ext, ...
#
# Uses Python for cross-platform consistency (no awk dependency).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse repo path: only consume first arg if it's not a flag
REPO="."
if [[ $# -gt 0 && "${1}" != --* ]]; then
  REPO="$1"
  shift
fi

SINCE="30 days ago"
GAP=3600

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --gap)   GAP="$2"; shift 2 ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! command -v git &>/dev/null; then
  echo "ERROR: git is not installed or not in PATH" >&2
  exit 1
fi

if ! git -C "$REPO" rev-parse --git-dir &>/dev/null; then
  echo "ERROR: $REPO is not a git repository" >&2
  exit 1
fi

# Save git output to temp file to avoid pipe/heredoc stdin conflict
ES_TMPFILE=$(mktemp)
trap 'rm -f "$ES_TMPFILE"' EXIT

git -C "$REPO" log \
  --pretty=format:"%at%x1f%ai%x1f%h%x1f%s" \
  --name-only \
  --since="$SINCE" > "$ES_TMPFILE"

ES_GAP="$GAP" ES_INPUT="$ES_TMPFILE" python3 << 'PYEOF'
import sys, os

gap = int(os.environ.get("ES_GAP", "3600"))
input_file = os.environ["ES_INPUT"]

sessions = []
current = None

with open(input_file, encoding="utf-8", errors="replace") as f:
    for raw_line in f:
        line = raw_line.rstrip("\n")

        # Commit header line (has unit separator)
        if "\x1f" in line:
            parts = line.split("\x1f", 3)
            if len(parts) >= 4:
                try:
                    ts = int(parts[0])
                except ValueError:
                    continue
                date_str = parts[1][:16]
                hash_val = parts[2]
                subject = parts[3]

                # Check if we need a new session
                if current is None or (current["prev_ts"] - ts) > gap:
                    if current is not None:
                        sessions.append(current)
                    current = {
                        "date": date_str,
                        "commits": [],
                        "files": set(),
                        "prev_ts": ts,
                    }
                current["commits"].append((hash_val, subject))
                current["prev_ts"] = ts
        elif line.strip():
            # File name line
            if current is not None:
                current["files"].add(line.strip())

if current is not None:
    sessions.append(current)

for i, s in enumerate(sessions, 1):
    print("--- SESSION {} ({}, {} commits) ---".format(i, s["date"], len(s["commits"])))
    for h, subj in s["commits"]:
        print("  {}  {}".format(h, subj))
    if s["files"]:
        print("  FILES: {}".format(", ".join(sorted(s["files"]))))
    print()
PYEOF
