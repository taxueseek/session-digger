---
name: dashboard
description: Global memory overview — staleness alerts, token costs, and stats across all projects
argument-hint:
allowed-tools: Bash
---

Show a global overview of Claude Code memories across all projects.

Run the memory dashboard script to get the overview:

bash "${CLAUDE_PLUGIN_ROOT}/scripts/memory-dashboard.sh"

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
