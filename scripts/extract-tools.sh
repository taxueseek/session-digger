#!/usr/bin/env bash
# extract-tools.sh — Extract tool calls and their results from a session file or directory
# Usage: extract-tools.sh <file.jsonl|session_dir> [--tool NAME] [--errors-only] [--limit N]
#
# Output format (tab-separated):
#   TIMESTAMP  TOOL_NAME  STATUS  KEY_INPUT  RESULT_PREVIEW

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

INPUT="${1:?Usage: extract-tools.sh <file.jsonl|session_dir> [--tool NAME] [--errors-only] [--limit N]}"
shift

TOOL_FILTER=""
ERRORS_ONLY=0
LIMIT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tool) TOOL_FILTER="$2"; shift 2 ;;
    --errors-only) ERRORS_ONLY=1; shift ;;
    --limit) LIMIT="$2"; shift 2 ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --limit must be a number" >&2
  exit 1
fi

ES_INPUT="$INPUT" ES_TOOL="$TOOL_FILTER" ES_ERRORS="$ERRORS_ONLY" ES_LIMIT="$LIMIT" \
ES_SCRIPT_DIR="$SCRIPT_DIR" \
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

input_path = os.environ["ES_INPUT"]
tool_filter = os.environ.get("ES_TOOL", "")
errors_only = os.environ.get("ES_ERRORS", "0") == "1"
limit = int(os.environ.get("ES_LIMIT", "0"))

# Detect agent type and dispatch
if os.path.isdir(input_path):
    agent = echolib.detect_agent_type(input_path)
    if agent == "grok":
        for t in echolib.grok_extract_tools(input_path, tool_filter=tool_filter,
                                             errors_only=errors_only, limit=limit):
            print("{}\t{}\t{}\t{}\t{}".format(
                t["timestamp"], t["name"], t["status"],
                t["key_input"], t["result_preview"]))
        sys.exit(0)
    if agent == "kimi_code":
        for t in echolib.kimi_code_extract_tools(input_path, tool_filter=tool_filter,
                                             errors_only=errors_only, limit=limit):
            print("{}\t{}\t{}\t{}\t{}".format(
                t["timestamp"], t["name"], t["status"],
                t["key_input"], t["result_preview"]))
        sys.exit(0)

# Default: Claude Code JSONL file
for t in echolib.extract_tools(input_path, tool_filter=tool_filter,
                                 errors_only=errors_only, limit=limit):
    print("{}\t{}\t{}\t{}\t{}".format(
        t["timestamp"], t["name"], t["status"],
        t["key_input"], t["result_preview"]))
PYEOF
