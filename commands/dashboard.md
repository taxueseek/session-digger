---
name: dashboard
description: Global memory overview — staleness alerts, token costs, and stats across all projects
argument-hint:
allowed-tools: Bash
---

Show a global overview of Claude Code memories across all projects.

**Script path discovery:** Set `SD_ROOT` before running any script:
```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
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
