#!/usr/bin/env bash
# extract-knowledge.sh — Two-pass knowledge extraction from a session
# Usage: extract-knowledge.sh <session-jsonl-path>
#
# All extraction logic lives in echolib.extract_knowledge().
# This script is a thin parameter-passing wrapper.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

SESSION_PATH="${1:?Usage: extract-knowledge.sh <session.jsonl>}"

ES_INPUT="$SESSION_PATH" ES_SCRIPT_DIR="$SCRIPT_DIR" \
python3 << 'PYEOF'
import os, sys, json
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

session_path = os.environ["ES_INPUT"]
items = list(echolib.extract_knowledge(session_path))
json.dump(items, sys.stdout, indent=2)
PYEOF
