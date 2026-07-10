#!/usr/bin/env bash
# herdr-search.sh — Herdr action wrapper for session-digger search
#
# Usage (interactively via Herdr action):
#   bash scripts/herdr-search.sh
#
# If SD_SEARCH_KEYWORD env var is set (Herdr could inject it), search directly.
# Otherwise list sessions as a default entry point.
#
# Environment from Herdr:
#   HERDR_PLUGIN_ROOT  — absolute path to plugin directory
#   HERDR_WORKSPACE_ID — current workspace id
#   HERDR_ENV          — "1"

set -euo pipefail

PLUGIN_ROOT="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
RECALL="$PLUGIN_ROOT/scripts/sd-recall.py"

if [[ ! -f "$RECALL" ]]; then
    echo "ERROR: sd-recall.py not found at $RECALL" >&2
    exit 1
fi

KEYWORD="${SD_SEARCH_KEYWORD:-${1:-}}"

if [[ -n "$KEYWORD" ]]; then
    echo "🔎 搜索: $KEYWORD"
    echo "---"
    python3 "$RECALL" search "$KEYWORD" --scope all --limit 10
else
    echo "📂 最近会话 (使用 SD_SEARCH_KEYWORD=xxx 搜索)"
    echo "---"
    python3 "$RECALL" sessions --scope all --limit 15
fi
