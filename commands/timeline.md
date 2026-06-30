---
name: timeline
description: Show chronological timeline of project work
argument-hint: [--limit N] [--since YYYY-MM-DD]
---

Show a chronological timeline of work on the current project, combining Claude Code session history with git history.

Optional arguments: $ARGUMENTS

Launch the `recall` agent via the Task tool with the following context:

- **Focus**: timeline / project history
- **Task**: Build a chronological timeline of all work on the current project
- **Combine**: Claude Code sessions (what was discussed, tried, failed) + git commits (what shipped)
- **Default limit**: 50 sessions, 30 days of git history
- **Parse any flags**: --limit N, --since YYYY-MM-DD from: $ARGUMENTS

The agent should:
1. List all sessions for the current project
2. Check if this is a git repo (`git rev-parse --is-inside-work-tree`). If yes, get git history. If not, skip git data and build the timeline from session data only.
3. Merge both timelines chronologically, correlating by timestamp (session-only if not a git repo)
4. Enrich key sessions (high message count, error-heavy, or milestones) with stats
5. Present as a timeline with clear markers for milestones
6. If not a git repo, note at the top: "No git repository detected — timeline is based on session data only."

Output format:
```
# Project Timeline: [project name]
Branch: [current branch] (if git repo)
Period: [earliest] → [latest]
Total: [N sessions, M git commits]

### [YYYY-MM-DD] — [Session summary]
  Branch: [branch] | Messages: [N] | Files: [N]
  [1-2 sentence description]
  [Git: [commit hash] [commit message]] (only if git repo)
```

Note: For non-git projects, omit the Git line entirely and note at the top: "No git repository detected — timeline is based on session data only."
