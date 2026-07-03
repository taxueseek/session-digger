---
name: save-summary
description: |
  Save an analysis result as a cached summary for future recall.
  Triggers: "save summary", "保存分析", "记住这个分析", "缓存分析结果", "save this analysis".
argument-hint: <session-path> <analysis-text|-> [query-intent] [memory-tier]
allowed-tools: Bash
---

Save an analysis result as a `.summary.jsonl` entry so subsequent `/recall` calls hit the cache instead of re-parsing.

Arguments: $ARGUMENTS

**Step 0: Script path discovery**

```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
```

**Step 1: Parse arguments**

- `session-path` (required) — path to the .jsonl session file analyzed
- `analysis-text` (required) — the analysis result, or `-` to read from stdin
- `query-intent` (optional, default `general`) — what the analysis was about (e.g. `投资`, `skill优化`)
- `memory-tier` (optional, default `periodic`):
  - `permanent` — cognitive patterns, mental models → never expires
  - `periodic` — preferences, stage conclusions → expires after 7 days
  - `once` — temporary context → expires after 24 hours
- `excluded` (optional) — rejected directions, semicolon-separated (e.g. `"Rust太重;方案C成本高"`)

**Step 2: Save**

```bash
python3 "$SD_ROOT/scripts/sd-recall.py" save-summary SESSION_PATH "ANALYSIS_TEXT" --query QUERY_INTENT --agent auto --tier MEMORY_TIER --excluded "EXCLUDED"
```

If `analysis-text` is `-`, pipe the analysis via stdin:

```bash
echo "analysis text..." | python3 "$SD_ROOT/scripts/sd-recall.py" save-summary SESSION_PATH --stdin --query QUERY_INTENT --agent auto --tier MEMORY_TIER
```

**Step 3: Confirm**

Report what was saved: session, intent, tier, and the summary file path. Tell the user that next time they `/recall` the same topic, the cached summary will be returned instantly (marked `[CACHED]`).
