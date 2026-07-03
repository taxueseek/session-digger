---
name: recap
description: |
  Summarize recent sessions.
  Triggers: "recap", "最近做了什么", "最近在忙什么", "最近的工作", "recent work", "what have I been doing".
argument-hint: [N-sessions-or-days] [--detail low|medium|high]
---

Summarize recent Claude Code sessions for the current project.

Arguments: $ARGUMENTS

If a number is given (e.g., "3"), summarize the last 3 sessions.
If a duration is given (e.g., "7d" or "1w"), summarize sessions from that period.
Default: last 5 sessions.

Launch the `recall` agent via the Task tool with the following context:

- **Focus**: recap / recent summary
- **Parse arguments**: $ARGUMENTS (number, duration, --detail flag)
- **Default**: last 5 sessions, medium detail

The agent should first set the script root:
```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
```

Then:
1. List recent sessions using `python3 $SD_ROOT/scripts/sd-recall.py sessions --scope current --limit N`
2. If zero sessions are found, report "No sessions found for the current project." and suggest the user check that they are in the correct project directory, or try `sessions --scope all` to search across all projects.
3. For each session, get stats: `python3 $SD_ROOT/scripts/sd-recall.py session-stats <path>` — if this fails, warn the user and continue without stats for that session.
4. For medium/high detail, read user messages: `python3 $SD_ROOT/scripts/sd-recall.py messages <path> --role user --no-tools --limit 10` — if this fails, skip message extraction for that session.
5. Synthesize: what was accomplished, what's in progress, what problems were encountered

Detail levels:
- **low**: Date, summary, message count, file count per session + overall trajectory
- **medium**: Add per-session goal, key actions, outcome, files touched
- **high**: Add errors encountered, decisions made, unfinished work, token usage
