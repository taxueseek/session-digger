---
name: prune
description: |
  Interactive memory cleanup — review and prune stale memories.
  Triggers: "prune", "清理记忆", "删除过期记忆", "记忆太多了", "clean up memories", "memory cleanup".
argument-hint: [project] [--dry-run]
allowed-tools: Bash, Read, Edit, AskUserQuestion
---

Interactively clean up stale Claude Code memories.

Arguments: $ARGUMENTS

**Parse arguments:**
- If `--dry-run` is present: show what would be flagged without taking action
- If a project name is given: filter to that project
- Otherwise: scan all projects

**Step 1: Get staleness data**

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
	python3 "$SD_ROOT/scripts/sd-recall.py" sessions --scope all --limit 1000 2>/dev/null || echo "(no sessions found)"
```

**Step 2: Present flagged memories**

For each memory with staleness score > 50, sorted by score descending:

1. Show the full file content
2. Show staleness score, age, type, and reasons
3. Show recommended action (review or prune)

If `--dry-run`: just show the list and stop.

**Step 3: Interactive cleanup**

For each flagged memory, ask the user to choose:

- **Delete** — Before deleting, print the full file content to the conversation (backup in transcript). Then remove the file and its entry from MEMORY.md.
- **Archive** — Move file to `memory/archive/` subdirectory. Remove MEMORY.md entry. (Not available for standalone MEMORY.md layout — offer Delete or Keep instead.)
- **Keep** — Touch the file to reset mtime: `touch <filepath>`. This resets the staleness clock.
- **Edit** — Show current content, ask user what to change, apply via Edit tool, then keep.
- **Skip** — Move to next memory without action.

**Step 4: Summary**

Report actions taken: N deleted, N archived, N kept, N edited, N skipped.
Report estimated tokens saved (sum of deleted + archived memory token estimates).
