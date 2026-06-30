---
description: Grep patterns and one-liners for extracting specific data from JSONL session files
---

# Extraction Patterns — Grep Patterns for Common Queries

**Note:** These grep patterns assume common JSON formatting. They are heuristics for quick scanning — Claude Code's JSON output may vary in whitespace. For robust parsing, use the Python-based extraction scripts.

## Finding Specific Content in .jsonl Files

### User Messages Only (human text, not tool results)
```bash
# Lines that are user type AND have string content (not array)
grep '"type":"user"' file.jsonl | grep -v '"tool_result"' | grep -v '"isMeta":true'
```

### Assistant Text Responses
```bash
grep '"type":"assistant"' file.jsonl | grep -v '"<synthetic>"'
```

### Error Results
```bash
grep '"is_error":true\|"is_error": true' file.jsonl
```

### Specific Tool Usage
```bash
# All Read tool calls
grep '"name":"Read"\|"name": "Read"' file.jsonl

# All Bash commands
grep '"name":"Bash"\|"name": "Bash"' file.jsonl

# All file writes/edits
grep '"name":"Write"\|"name":"Edit"\|"name": "Write"\|"name": "Edit"' file.jsonl
```

### Decision Points (AskUserQuestion)
```bash
grep '"name":"AskUserQuestion"\|"name": "AskUserQuestion"' file.jsonl
```

### Plan Mode Content
```bash
grep '"planContent"' file.jsonl
grep '"name":"ExitPlanMode"\|"name":"EnterPlanMode"' file.jsonl
```

### Session Summary
```bash
grep '"type":"summary"\|"type": "summary"' file.jsonl
```

### Files Changed
```bash
grep '"type":"file-history-snapshot"\|"type": "file-history-snapshot"' file.jsonl | tail -1
```

### Context Compaction Events
```bash
grep '"compact_boundary"\|"microcompact_boundary"' file.jsonl
```

### Token Usage Per Turn
```bash
grep '"output_tokens"' file.jsonl
```

### Web Searches
```bash
grep '"name":"WebSearch"\|"name": "WebSearch"' file.jsonl
```

### Subagent Launches
```bash
grep '"name":"Task"\|"name": "Task"' file.jsonl
```

## Cross-Session Patterns

### Find sessions mentioning a topic (using sessions-index.json)
```bash
grep -i "topic" ~/.claude/projects/*/sessions-index.json
```

### Find sessions that edited a specific file
```bash
grep '"specific-file.ts"' ~/.claude/projects/<project-dir>/*.jsonl
```

### Find all sessions with errors
```bash
for f in ~/.claude/projects/<project-dir>/*.jsonl; do
  if grep -q '"is_error":true\|"is_error": true' "$f"; then
    echo "$f"
  fi
done
```

## Useful One-Liners

### Extract all unique tool names used
```bash
grep -o '"name":"[^"]*"' file.jsonl | sort -u
```

### Get conversation timeline (timestamps of user messages)
```bash
grep '"type":"user"' file.jsonl | grep -v '"tool_result"' | grep -o '"timestamp":"[^"]*"' | head -20
```

### Count messages by type (using parse-jsonl.sh)
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/parse-jsonl.sh file.jsonl --detect-schema
```
This gives per-type record counts without fragile awk patterns.
