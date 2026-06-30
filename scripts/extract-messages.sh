#!/usr/bin/env bash
# extract-messages.sh — Extract human-readable messages from a session file or directory
# Usage: extract-messages.sh <file.jsonl|session_dir> [--role user|assistant|both] [--limit N] [--no-tools] [--thinking N]
#
# Options:
#   --role      Filter by role: user, assistant, or both (default: both)
#   --limit     Max messages to output (default: 0 = all)
#   --no-tools  Omit tool_use summaries from assistant messages
#   --thinking  Max chars for thinking blocks (0 = full, -1 = hide, default: 0)
#
# Output format:
#   === [ROLE] [TIMESTAMP] ===
#   message text
#   ---

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

INPUT="${1:?Usage: extract-messages.sh <file.jsonl|session_dir> [--role user|assistant|both] [--limit N] [--no-tools] [--thinking N]}"
shift

ROLE="both"
LIMIT=0
NO_TOOLS=0
THINKING=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --no-tools) NO_TOOLS=1; shift ;;
    --thinking) THINKING="$2"; shift 2 ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ "$ROLE" != "both" && "$ROLE" != "user" && "$ROLE" != "assistant" ]]; then
  echo "ERROR: --role must be user, assistant, or both" >&2
  exit 1
fi
if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --limit must be a number" >&2
  exit 1
fi
if ! [[ "$THINKING" =~ ^-?[0-9]+$ ]]; then
  echo "ERROR: --thinking must be a number (0=full, -1=hide, N=max chars)" >&2
  exit 1
fi

ES_INPUT="$INPUT" ES_ROLE="$ROLE" ES_LIMIT="$LIMIT" ES_NO_TOOLS="$NO_TOOLS" \
ES_THINKING="$THINKING" ES_SCRIPT_DIR="$SCRIPT_DIR" \
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

input_path = os.environ["ES_INPUT"]
role = os.environ.get("ES_ROLE", "both")
limit = int(os.environ.get("ES_LIMIT", "0"))
no_tools = os.environ.get("ES_NO_TOOLS", "0") == "1"
thinking_limit = int(os.environ.get("ES_THINKING", "0"))

# Detect agent type and dispatch
if os.path.isdir(input_path):
    agent = echolib.detect_agent_type(input_path)
    if agent == "grok":
        for msg in echolib.grok_extract_messages(input_path, role=role, limit=limit, thinking_limit=thinking_limit):
            print("=== [{}] [{}] ===".format(msg["role"], msg.get("timestamp", "")))
            print(msg["text"][:500])
            print("---")
        sys.exit(0)
    if agent == "kimi_code":
        for msg in echolib.kimi_code_extract_messages(input_path, role=role, limit=limit, thinking_limit=thinking_limit):
            print("=== [{}] [{}] ===".format(msg["role"], msg.get("timestamp", "")))
            print(msg["text"][:500])
            print("---")
        sys.exit(0)

# Default: Claude Code JSONL file
for msg in echolib.extract_messages(input_path, role=role, no_tools=no_tools, limit=limit, thinking_limit=thinking_limit):
    print("=== [{}] [{}] ===".format(msg["role"], msg["timestamp"]))
    print(msg["text"])
    print("---")
PYEOF
