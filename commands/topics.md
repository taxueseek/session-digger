---
name: topics
description: |
  Segment sessions into topics, identify topic boundaries, and browse by topic.
  Triggers: "topics", "话题分析", "session topics", "话题切分", "what did we discuss", "讨论了什么".
argument-hint: [<session-id>|--project PATH] [--min-gap SECONDS]
allowed-tools: Bash, Read, Task
---

Analyze and display topic segments for a session or project.

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
- `<session-path-or-id>` → Analyze single session topics
- `--project PATH` or no argument → Analyze all sessions in project

## Single-session topic segmentation

If $ARGUMENTS contains a file path:
```bash
python3 $SD_ROOT/scripts/topic-segmenter.py <session.jsonl> --min-gap 300
```

If $ARGUMENTS contains a session ID (not a path):
```bash
# Resolve session ID to file first
SESSION_FILE=$(python3 $SD_ROOT/scripts/sd-recall.py sessions --scope all --limit 20 | grep "<session-id>" | head -1 | awk '{print $7}')
python3 $SD_ROOT/scripts/topic-segmenter.py "$SESSION_FILE"
```

## Project-wide topics

```bash
python3 $SD_ROOT/scripts/topic-segmenter.py --project <project-dir>
```

## Output interpretation

Display each segment with:
- Time range
- Message count
- Top keywords (auto-extracted as label)

Format:
```
--- Session: <id> (42 messages, 3 topics) ---

[1] 10:30 - 11:05 (12 msgs)
    Keywords: API design, schema migration
    Boundary: 5-minute gap after config change

[2] 11:10 - 12:30 (18 msgs)
    Keywords: test coverage, pytest, fixtures
    Boundary: topic shift (API → testing)

[3] 12:35 - 13:00 (12 msgs)
    Keywords: deployment, docker, CI/CD
```

## Use cases

- **"What did we work on last Tuesday?"** → `/topics --min-gap 600` to see distinct work blocks
- **"How many distinct topics were in that meeting?"** → `/topics session.jsonl`
- **"Show me all the topics from this week"** → `/topics --project .` 

## Next step

After browsing topics, use `/recall <keyword>` to deep-dive into a specific topic segment.
