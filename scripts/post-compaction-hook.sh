#!/usr/bin/env bash
# post-compaction-hook.sh — Detect context compaction and notify Claude Code.
#
# Session Digger hook for Claude Code (PreToolUse).
# Checks once per Claude process whether compaction has occurred.
# When detected, warns that session-digger recall tools can recover lost context.
#
# Usage (Claude Code hook registration):
#   Register in ~/.claude/settings.json:
#   {
#     "hooks": {
#       "PreToolUse": [
#         {
#           "matcher": "Bash|Read|Edit|Write|Glob|Grep",
#           "hooks": [{ "type": "command", "command": "PATH/TO/post-compaction-hook.sh", "timeout": 5 }]
#         }
#       ]
#     }
#   }
#
# MUST always output {"decision": "approve"} on stdout (Claude Code hook contract).
#
# v1.0: Inspired by session-recall post-compaction-recall.sh hook.

set -euo pipefail

# Claude Code hook contract: always approve, even on error
trap 'echo "{\"decision\": \"approve\"}"' EXIT

# Only run in Claude Code environments
if [[ -z "${CLAUDE_CODE_SESSION_ID:-}" && -z "${CLAUDE_PLUGIN_ROOT:-}" && ! -d "$HOME/.claude/projects" ]]; then
  exit 0
fi

# Walk process tree to find Claude's PID (session-stable flag key)
CLAUDE_PID=""
PID=$$
for _ in 1 2 3 4 5 6 7 8 9 10; do
  PARENT=$(ps -o ppid= -p "$PID" 2>/dev/null | tr -d ' ') || break
  [[ -z "$PARENT" || "$PARENT" == "1" || "$PARENT" == "0" ]] && break
  PID="$PARENT"
  PNAME=$(ps -o comm= -p "$PID" 2>/dev/null | tr -d ' ') || continue
  case "$PNAME" in claude|claude-code|node) CLAUDE_PID="$PID"; break ;; esac
done

FLAG="/tmp/session-digger-post-compact-${CLAUDE_PID:-$$}"
[[ -f "$FLAG" ]] && exit 0
touch "$FLAG" 2>/dev/null || true

# Check if current session was compacted
# We detect compaction by looking for compact_boundary records in recent JSONL
COMPACTION_DETECTED=0

# Find the current session's JSONL files
if [[ -d "$HOME/.claude/projects" ]]; then
  # Get most recently modified JSONL file
  LATEST_JSONL=$(find "$HOME/.claude/projects" -name "*.jsonl" -not -path "*/subagents/*" -mmin -120 -print0 2>/dev/null | \
    xargs -0 ls -t 2>/dev/null | head -1) || true

  if [[ -n "$LATEST_JSONL" ]]; then
    # Quick check: does this file contain a compact_boundary record?
    if grep -q '"compact_boundary"\|"microcompact_boundary"' "$LATEST_JSONL" 2>/dev/null; then
      COMPACTION_DETECTED=1
    fi
  fi
fi

if [[ "$COMPACTION_DETECTED" -eq 1 ]]; then
  # Post notification to stderr (goes to Claude's context)
  cat >&2 << 'EOF'
⚡ Session Digger: Context was compacted. Lost details recoverable:
   → Use /session-digger:recall <keyword> to search past sessions
   → Use /session-digger:analyze for pattern detection
   → Use scripts/recall-lite.sh for zero-API raw search
EOF
fi

exit 0
