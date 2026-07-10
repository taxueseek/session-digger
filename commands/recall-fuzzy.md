---
name: recall-fuzzy
description: |
  Interactive fuzzy search over past sessions using fzf.
  Triggers: "fuzzy search", "模糊搜索", "fzf recall", "browse sessions", "select session interactively".
  Requires: fzf (auto-degrades to list mode if not installed).
argument-hint: [keyword]
allowed-tools: Bash
---

Interactively browse and select past sessions using fzf fuzzy finder.

Arguments: $ARGUMENTS

**Script path discovery:**
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

## Execution

Run the fuzzy search script via Bash:

```bash
bash $SD_ROOT/scripts/herdr-fuzzy-search.sh "$ARGUMENTS"
```

The script will:
1. If fzf is installed → open an interactive fuzzy selector showing all indexed sessions
   - Type to filter by session ID, agent, date
   - Press Enter to view the selected session's messages
   - Press Ctrl+R to refresh the list
2. If fzf is NOT installed → fall back to a plain session list with install instructions

After the user selects a session (or if in fallback mode), show what was found.

## No LLM synthesis needed

This is a direct CLI passthrough. Do not launch an agent. Do not summarize or reinterpret the output.
