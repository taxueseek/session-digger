#!/usr/bin/env bash
# herdr-search.sh — Herdr action wrapper for session-digger search
#
# Env (Herdr injects):
#   HERDR_PLUGIN_ROOT  — plugin directory
# Optional:
#   SD_SEARCH_KEYWORD / $1 — search keyword
#   SESSION_DIGGER_ROOT — override plugin root when not under Herdr

set -euo pipefail

PLUGIN_ROOT="${HERDR_PLUGIN_ROOT:-${SESSION_DIGGER_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}}"
RECALL="$PLUGIN_ROOT/scripts/sd-recall.py"

if [[ ! -f "$RECALL" ]]; then
    echo "ERROR: sd-recall.py not found (set HERDR_PLUGIN_ROOT or SESSION_DIGGER_ROOT)" >&2
    exit 1
fi

KEYWORD="${SD_SEARCH_KEYWORD:-${1:-}}"

if [[ -n "$KEYWORD" ]]; then
    echo "搜索: $KEYWORD"
    echo "---"
    python3 "$RECALL" search "$KEYWORD" --scope all --limit 10
else
    echo "最近会话 (设置 SD_SEARCH_KEYWORD=xxx 或传入参数可直接搜索)"
    echo "---"
    python3 "$RECALL" sessions --scope all --limit 15
fi
