---
name: reflect
description: |
  Local multi-environment AI usage recap HTML (habits, heatmaps, tasks, topics).
  Not Claude.ai Reflect cloud; not /usage quota; not /recap recent-session summary.
  Triggers: "使用回顾", "reflect", "usage recap", "用了多久", "AI 使用习惯",
  "时段热力", "安静时段", "使用报告", "本机回顾".
  v0.9.6: homepage usage deck, env-scoped insights, multi-theme palette.
argument-hint: [--months 1|3|6|12] [--open] [--lookback N]
allowed-tools: Bash
---

Generate a **local** multi-environment usage recap report from the session-digger index (session-digger **v0.9.6**).

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

Needs `~/.claude/.session-digger/index.db` (or `$SESSION_DIGGER_DATA_DIR/index.db`).
If missing, run `/index` first.

## Run

Parse $ARGUMENTS:

- `--months 1|3|6|12` — default period selected in the HTML UI (default `1`)
- `--lookback N` — include last N months of sessions in the payload (default `12`)
- `--open` — open the HTML in the browser after write
- bare number like `3` → treat as `--months 3`

Default: generate last-month UI selection with 12-month data, then open.

```bash
# default: 1 month UI, open browser
python3 "$SD_ROOT/scripts/reflect-report.py" --months 1 --open

# example: 3 months default, no auto-open
python3 "$SD_ROOT/scripts/reflect-report.py" --months 3
```

## What to tell the user

1. Report path from script stdout (`wrote …`)
2. Opened in browser if `--open`
3. Brief caveats (one line each, only if useful):
   - 用时 = 会话跨度估算，不是盯屏秒表
   - 安静时段 = 本机浏览器提醒，不锁账号
   - Claude / ZCode / Codex 等若摘要为空，话题会偏弱；可先 `/index` 刷新

## Not this command

| User wants | Use instead |
|------------|-------------|
| Recent session summary | `/recap` |
| Token / quota usage | product `/usage` (not digger) |
| Memory staleness dashboard | `/dashboard` |
| Week-over-week index trends | `/trend` |
| Semantic search past work | `/recall` |
