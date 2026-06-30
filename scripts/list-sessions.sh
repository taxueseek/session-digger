#!/usr/bin/env bash
# list-sessions.sh — List sessions from Claude Code, Grok Build, or Kimi Code
# Usage: list-sessions.sh [project-path|"all"|"current"] [--limit N] [--since YYYY-MM-DD] [--grep PATTERN] [--agent claude|grok|kimi_code|cross|auto]
#
# Output format (tab-separated):
#   SESSION_ID  CREATED  MODIFIED  MSG_COUNT  BRANCH  SUMMARY  FIRST_PROMPT  PROJECT_PATH  FULL_PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse scope: only consume first arg if it's not a flag
SCOPE="current"
if [[ $# -gt 0 && "${1}" != --* ]]; then
  SCOPE="$1"
  shift
fi

LIMIT=50
SINCE=""
GREP_PAT=""
AGENT="auto"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) LIMIT="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    --grep)  GREP_PAT="$2"; shift 2 ;;
    --agent) AGENT="$2"; shift 2 ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --limit must be a number, got: $LIMIT" >&2
  exit 1
fi

ES_SCOPE="$SCOPE" ES_TARGET="$(pwd)" ES_LIMIT="$LIMIT" ES_SINCE="$SINCE" ES_GREP="$GREP_PAT" \
ES_AGENT="$AGENT" ES_SCRIPT_DIR="$SCRIPT_DIR" \
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

scope = os.environ["ES_SCOPE"]
target = os.environ.get("ES_TARGET", "")
limit = int(os.environ.get("ES_LIMIT", "50"))
since = os.environ.get("ES_SINCE", "")
grep_pat = os.environ.get("ES_GREP", "")
agent = os.environ.get("ES_AGENT", "auto")

# Auto-detect: if Grok sessions exist and no Claude sessions found, use Grok
if agent == "auto":
    agent = echolib.detect_agent_type()

entries = []

if agent == "grok":
    cwd = target if scope in ("current", "path") else None
    entries = echolib.grok_list_sessions(cwd=cwd, limit=limit, keyword=grep_pat)
elif agent == "kimi_code":
    cwd = target if scope in ("current", "path") else None
    entries = echolib.kimi_code_list_sessions(cwd=cwd, limit=limit, keyword=grep_pat)
elif agent == "cross" or agent == "all":
    entries_raw = echolib.cross_tool_list_sessions(limit=limit, keyword=grep_pat)
    # cross_tool_list_sessions returns dicts, not SessionMeta; wrap for to_tsv()
    class _Wrapper:
        def __init__(self, d):
            self.session_id = d.get("session_id", "")
            self.created = d.get("created", "")
            self.modified = d.get("created", "")
            self.message_count = d.get("msg_count", 0)
            self.git_branch = ""
            self.summary = d.get("summary", "")
            self.first_prompt = d.get("first_prompt", "")
            self.project_path = d.get("agent", "")
            self.full_path = d.get("full_path", "")
        def to_tsv(self):
            return "\t".join(str(x) for x in [
                self.session_id, self.created, self.modified, self.message_count,
                self.git_branch, self.summary, self.first_prompt,
                self.project_path, self.full_path,
            ])
    entries = [_Wrapper(r) for r in entries_raw]
else:
    # Claude Code sessions
    if scope in ("current", "all"):
        entries = echolib.list_sessions(scope=scope, target=target, limit=limit, since=since, grep_pat=grep_pat)
    else:
        entries = echolib.list_sessions(scope="path", target=scope, limit=limit, since=since, grep_pat=grep_pat)

if not entries and scope == "current":
    print("ERROR: No session directory found for " + target, file=sys.stderr)
    print("Hint: try 'list-sessions.sh all' to search all projects, or --agent grok", file=sys.stderr)
    sys.exit(1)

for e in entries:
    print(e.to_tsv())
PYEOF
