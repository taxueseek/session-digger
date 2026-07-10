---
name: analyze
description: |
  Analyze sessions for patterns — retry loops, errors, user corrections, and candidate rules.
  Triggers: "analyze session", "session analysis", "find retry patterns", "what went wrong", "分析会话", "模式分析".
argument-hint: [<session-id>|--all] [--limit N] [--format text|json]
allowed-tools: Bash, Read, Task
---

Analyze Claude Code session transcripts to detect patterns and generate rules.

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

## Mode detection

Parse $ARGUMENTS:
- `--all` → Cross-session aggregate analysis (last N sessions)
- `<session-path-or-id>` → Deep single-session analysis
- Neither → Default: analyze the **current session** (most recent for this project)

## Single-session analysis

```bash
bash $SD_ROOT/scripts/analyze-session.sh <session.jsonl> [--format text|json]
```

If $ARGUMENTS contains a session ID (UUID) but not a file path, resolve it first:
```bash
	python3 $SD_ROOT/scripts/sd-recall.py sessions --scope current --limit 20
```
Then pass the matching `.jsonl` file to the analyze script.

Default to `--format text` for readability. Use `--format json` when the user wants to pipe or programmatically process results.

## Cross-session analysis (`--all`)

```bash
bash $SD_ROOT/scripts/analyze-session.sh --all [--limit N]
```

Aggregates: total errors, error rate, errors by tool, retry pattern count, candidate rules across last N sessions (default 10).

## Output interpretation

Present findings in this order:
1. **Overview** — messages, tool calls, error rate, tokens
2. **Retry patterns** — which tool failed repeatedly and how
3. **Errors by tool** — breakdown
4. **User corrections** — what the user had to fix
5. **Candidate rules** — concrete CLAUDE.md suggestions

## Next step

Always end with: "Run `/apply` to review and persist the candidate rules."
