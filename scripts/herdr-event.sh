#!/usr/bin/env bash
# herdr-event.sh — Herdr event hooks for session-digger
#
# Triggered by Herdr lifecycle events:
#   worktree.created  — new worktree created
#   worktree.removed  — worktree removed
#
# This handler logs the event and opportunistically rebuilds the
# session-digger index in the background so the stats pane stays fresh.
#
# Environment from Herdr:
#   HERDR_PLUGIN_ROOT   — absolute path to plugin directory
#   HERDR_WORKSPACE_ID  — current workspace id
#   HERDR_ENV           — "1"

set -euo pipefail

EVENT="${1:-unknown}"
PLUGIN_ROOT="${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
INDEX_BUILDER="$PLUGIN_ROOT/scripts/index-builder.py"
LOG_DIR="$HOME/.claude/.session-digger"
LOG_FILE="$LOG_DIR/herdr-events.log"

mkdir -p "$LOG_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] event=$EVENT workspace=${HERDR_WORKSPACE_ID:-?} plugin=${HERDR_PLUGIN_ID:-?}" >> "$LOG_FILE"

case "$EVENT" in
    worktree.created)
        echo "⛏ session-digger: 检测到新工作树，重建索引..."
        if [[ -f "$INDEX_BUILDER" ]]; then
            python3 "$INDEX_BUILDER" --agent cross >> "$LOG_FILE" 2>&1 || true
            echo "✅ 索引重建完成"
        fi
        ;;
    worktree.removed)
        echo "⛏ session-digger: 工作树已移除。"
        ;;
    *)
        echo "⛏ session-digger: 未知事件 $EVENT"
        ;;
esac
