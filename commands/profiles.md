---
name: profiles
description: |
  Extract and update incremental participant profiles from group chat sessions.
  Triggers: "profiles", "群友画像", "who talked", "participant analysis", "画像分析".
argument-hint: <session-id-or-path> [--profiles-dir PATH]
allowed-tools: Bash, Read
---

Extract incremental participant profiles from a group chat session.

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

## Usage

```bash
# Auto-detect profiles dir (sibling to session file)
python3 $SD_ROOT/scripts/chat-profiles.py <session.jsonl>

# Custom profiles dir
python3 $SD_ROOT/scripts/chat-profiles.py <session.jsonl> /path/to/profiles/
```

## What it does

Scans a group chat JSONL for per-participant messages and maintains one `.json` profile per sender. Profiles use **append-only merge rules** (inspired by baoyu-wechat-summary):

- **APPEND**: classic quotes, signature events — grow unbounded, capped at 20
- **MERGE**: interest areas, active hours — frequency-sorted, deduped
- **REFINE**: speaking style — only updated when crossing 100-message thresholds

## Output

One JSON file per participant in `profiles/` (sibling to session or `--profiles-dir`):

```json
{
  "name": "张三",
  "total_messages": 47,
  "classic_quotes": ["这个方案可行！", "我建议再想想…"],
  "interest_areas": ["AI", "写作", "投资"],
  "active_hours": {"9": 3, "14": 12, "20": 8},
  "peak_hour": 14,
  "first_seen": "2026-06-01T09:00:00",
  "last_seen": "2026-07-04T20:30:00"
}
```

## Limitations

- Requires sender-identified messages (adapted by `dialog-adapter.py` or native group chat format)
- Minimum 2 messages per participant to generate a profile
- Does NOT decrypt WeChat databases (use wechat-local-vault first)
