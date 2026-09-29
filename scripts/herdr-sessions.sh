#!/usr/bin/env bash
# herdr-sessions.sh — list sessions; redact PATH for display
# Usage: herdr-sessions.sh [limit]   (default 20)

set -euo pipefail

# The first positional argument is a limit, so --help must be intercepted
# before it becomes "sessions --limit --help" and dies in argparse at rc=2.
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

PLUGIN_ROOT="${HERDR_PLUGIN_ROOT:-${SESSION_DIGGER_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}}"
RECALL="$PLUGIN_ROOT/scripts/sd-recall.py"
LIMIT="${1:-20}"
[[ -f "$RECALL" ]] || { echo "ERROR: sd-recall.py not found" >&2; exit 1; }

python3 "$RECALL" sessions --scope all --limit "$LIMIT" | python3 -c '
import sys, re
from pathlib import Path
home = str(Path.home())
user = Path.home().name

def redact(s: str) -> str:
    if not s:
        return s
    if s.startswith(home):
        s = "~" + s[len(home):]
    if user:
        s = s.replace(user, "<user>")
    s = re.sub(r"/Users/[^/]+", "/Users/<user>", s)
    s = re.sub(r"/home/[^/]+", "/home/<user>", s)
    s = re.sub(r"%2FUsers%2F[^%]+", "%2FUsers%2F%3Cuser%3E", s, flags=re.I)
    return s

for line in sys.stdin:
    line = line.rstrip("\n")
    if not line or line.startswith("---"):
        print(line)
        continue
    parts = line.split("\t")
    if len(parts) >= 7 and parts[0] != "SESSION_ID":
        parts[-1] = redact(parts[-1])
        print("\t".join(parts))
    else:
        print(line)
'
