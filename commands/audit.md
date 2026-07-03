---
name: audit
description: |
  Audit memory staleness — check which memories are outdated.
  Triggers: "audit", "记忆过期了", "检查记忆", "哪些记忆该清理", "memory audit", "review memories".
argument-hint: [project] [--deep]
allowed-tools: Bash, Task
---

Audit Claude Code memories for staleness.

Arguments: $ARGUMENTS

**Parse arguments:**
- If `--deep` is present: dispatch the `memory-auditor` agent for content-aware verification
- If a project name/path is given: filter to that project
- Otherwise: audit all projects with memories

**Without --deep (default):**

Set script root and run the heuristic audit script:
```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
bash "$SD_ROOT/scripts/memory-dashboard.sh" --project PROJECT_IF_SPECIFIED

Present the detailed staleness table. For each memory with score > 50, show:
- File path
- Type and age
- Score and recommended action

Suggest `/prune` for memories recommended for pruning, or `/audit --deep` for content verification.

**With --deep:**

Launch the `memory-auditor` agent via the Task tool with:
- **Target project**: from arguments (or "all" if not specified)
- **Memory files**: list all memory files to audit (from dashboard script output)
- **Project roots**: resolved project root paths for file/code verification

The memory-auditor agent will verify claims in memory content against current project state and produce a detailed report.
