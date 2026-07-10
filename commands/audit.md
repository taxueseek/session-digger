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

Set script root and run the heuristic audit:

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

# List sessions for context
python3 "$SD_ROOT/scripts/sd-recall.py" sessions --scope all --limit 1000 2>/dev/null || echo "(no sessions found)"
```

Then compute memory staleness scores using the filesystem:

```bash
# Scan memory files across all projects and compute staleness
for memfile in ~/.claude/projects/*/memory/*.md; do
  [[ -f "$memfile" ]] || continue
  age_days=$(( ($(date +%s) - $(stat -f %m "$memfile")) / 86400 ))
  lines=$(wc -l < "$memfile")
  # Staleness heuristic: age_days * 2 + (0 if lines>5 else 5)
  score=$(( age_days * 2 + (lines > 5 ? 0 : 5) ))
  echo "score=$score age=${age_days}d lines=$lines $memfile"
done | sort -t= -k2 -rn | head -20
```

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
