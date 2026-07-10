---
name: trend
description: |
  Longitudinal trend analysis over session history. Week/month-over-month
  comparison, by-project/by-tag aggregation, and tool error-rate regression
  detection. Reads the pre-built SQLite index — fast, no raw transcript parsing.
  Triggers: "trend", "趋势", "环比", "这个月比上个月", "长期来看", "工具错误率趋势",
  "哪些工具变差了", "by project", "按项目统计".
argument-hint: [period-over-period|by-theme|regressions] [--unit week|month] [--lookback N] [--group-by project|tag|agent]
allowed-tools: Bash, Read
---

Analyze longitudinal trends across your session history.

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

## Prerequisite

The index must be built first. If `~/.claude/.session-digger/index.db` doesn't
exist, run `/index` before `/trend`.

## Mode detection

Parse $ARGUMENTS:

### `period-over-period` (default)

Week-over-week or month-over-month comparison with deltas.

```bash
python3 $SD_ROOT/scripts/trend-engine.py period-over-period --unit week --lookback 8
```

- `--unit week|month` — bucket size (default: week)
- `--lookback N` — how many periods to show (default: 8)

### `by-theme`

Aggregate stats by project, tag, or agent — not time-based.

```bash
python3 $SD_ROOT/scripts/trend-engine.py by-theme --group-by project
python3 $SD_ROOT/scripts/trend-engine.py by-theme --group-by tag
python3 $SD_ROOT/scripts/trend-engine.py by-theme --group-by agent
```

### `regressions`

Detect tools whose error rate is trending upward — recurring pain points,
not one-off bad sessions.

```bash
python3 $SD_ROOT/scripts/trend-engine.py regressions --unit month --lookback 6
```

## Default behavior

If no mode is specified, run `period-over-period --unit month --lookback 6`.

## Output interpretation

Present findings like a competent analyst would:

1. **Direction first** — lead with whether the error rate is improving,
   worsening, or stable. Don't bury the lede in a wall of numbers.
2. **Notable changes** — call out specific tools or projects with significant
   deltas. Explain WHY it matters.
3. **Data sufficiency** — if there's too little history to say anything
   trustworthy, say that plainly. The script already notes this; don't paper
   over it with a confident-sounding narrative.
4. **Actionable next steps** — if regressions are found, suggest running
   `/optimize` to get concrete improvement proposals.

## Architecture note

This is Layer 2 (TREND) — pure arithmetic over the index, no new judgment
calls. The actual conversation content is never touched; if you need to
investigate a specific session behind a trend, use `/recall` with the
session_id from the trend output.

## Next step

- If regressions found → suggest `/optimize` for concrete skill improvement proposals
- If long-conversation flag is common → suggest `/analyze` for retry pattern details
- If project outlier found → suggest reviewing that project's CLAUDE.md
