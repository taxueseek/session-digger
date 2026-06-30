#!/usr/bin/env bash
# session-stats.sh — Quick statistics for a session directory or .jsonl file
# Usage: session-stats.sh <file.jsonl|session_dir>
#
# Output: key=value pairs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

INPUT="${1:?Usage: session-stats.sh <file.jsonl|session_dir>}"

ES_INPUT="$INPUT" ES_SCRIPT_DIR="$SCRIPT_DIR" \
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

input_path = os.environ["ES_INPUT"]

# Detect if this is a Grok session directory
if os.path.isdir(input_path):
    agent = echolib.detect_agent_type(input_path)
    if agent == "grok":
        stats = echolib.grok_session_stats(input_path)
    elif agent == "kimi_code":
        stats = echolib.kimi_code_session_stats(input_path)
    else:
        # Claude Code: find the .jsonl file in the directory
        jsonl_files = [f for f in os.listdir(input_path) if f.endswith(".jsonl")]
        if jsonl_files:
            jsonl_path = os.path.join(input_path, jsonl_files[0])
            stats = echolib.session_stats(jsonl_path)
        else:
            print("ERROR: No .jsonl file found in " + input_path, file=sys.stderr)
            sys.exit(1)
else:
    # Assume it's a .jsonl file
    stats = echolib.session_stats(input_path)

print("model={}".format(stats.get("model", "")))
print("started={}".format(stats.get("started", "")))
print("ended={}".format(stats.get("ended", "")))
print("user_messages={}".format(stats.get("user_messages", 0)))
print("assistant_messages={}".format(stats.get("assistant_messages", 0)))
print("tool_calls={}".format(stats.get("tool_calls", 0)))
print("files_edited={}".format(stats.get("files_edited", 0)))
print("errors={}".format(stats.get("errors", 0)))
print("input_tokens={}".format(stats.get("input_tokens", 0)))
print("output_tokens={}".format(stats.get("output_tokens", 0)))
print("total_tokens={}".format(stats.get("total_tokens", 0)))
print("compactions={}".format(stats.get("compactions", 0)))
if stats.get("summary"):
    print("summary={}".format(stats["summary"]))
PYEOF
