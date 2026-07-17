#!/usr/bin/env bash
# _sdroot.sh — Single source of truth for session-digger script root discovery.
# Source this from slash commands: source $THIS_DIR/_sdroot.sh
# After sourcing, $SD_ROOT points to the session-digger root and
# $SD_SCRIPTS points to the scripts/ directory.

_SD_SOURCE="${BASH_SOURCE[0]:-$0}"
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "$_SD_SOURCE")/../.." 2>/dev/null && pwd)"
if [[ -z "$SD_ROOT" || ! -f "$SD_ROOT/scripts/sd-recall.py" ]]; then
  for _c in \
    "$HOME/.agents/skills/session-digger" \
    "$HOME/.claude/plugins/session-digger" \
    "$HOME/.claude/skills/session-digger" \
    "$HOME/.grok/skills/session-digger"
  do
    [[ -f "$_c/scripts/sd-recall.py" ]] && SD_ROOT="$_c" && break
  done
fi
SD_SCRIPTS="$SD_ROOT/scripts"
# Use $SD_SCRIPTS/<script> in subsequent commands. Prefer SESSION_DIGGER_ROOT when multiple installs exist.
