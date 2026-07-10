---
name: dashboard
description: Global memory overview — staleness alerts, token costs, and stats across all projects
argument-hint:
allowed-tools: Bash
---

Show a global overview of Claude Code memories across all projects.

**Script path discovery:** Set `SD_ROOT` before running any script:
```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
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
# Use $SD_ROOT/scripts/<script> in subsequent commands. Prefer SESSION_DIGGER_ROOT when multiple installs exist.
```

Run the memory dashboard script to get the overview:

python3 "$SD_ROOT/scripts/sd-recall.py" sessions --scope all --limit 1000

Present the output to the user. If there are staleness alerts, suggest running `/audit` or `/audit --deep` for detailed analysis. If token costs are high, suggest `/prune` for cleanup.

### Output Format

```
Memory Dashboard — Global Overview

Project                    Memories   Lines   Stale   Last Updated
────────────────────────────────────────────────────────────────────
project-a                  12         340     2       2026-03-20
project-b                  8          180     0       2026-03-28
project-c                  5          95      3       2026-02-15

Total: 25 memories, 615 lines, ~2,153 tokens
Stale: 5 memories across 2 projects (review with /session-digger:prune)
```
