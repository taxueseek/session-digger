---
name: import
description: |
  Import external conversation data (WeChat, generic JSON/CSV, transcripts) into session-digger.
  Triggers: "import chat", "导入对话", "add conversation", "微信导入", "import wechat", "导入聊天记录".
argument-hint: <input-file> [--format auto|wechat|json|csv|transcript] [--session-id NAME]
allowed-tools: Bash, Write
---

Import external conversation data into session-digger's indexable format.

Arguments: $ARGUMENTS

**Script path discovery:**
```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
[[ -z "$SD_ROOT" ]] && [[ -d "$HOME/.agents/skills/session-digger" ]] && SD_ROOT="$HOME/.agents/skills/session-digger"
```

## What it does

Converts various conversation formats into session-digger's JSONL schema,
placing the output in `~/.claude/projects/_imported/` for indexing and searching.

## Usage

```bash
python3 $SD_ROOT/scripts/dialog-adapter.py <input-file> \
  [--format auto|wechat|json|csv|transcript] \
  [--session-id <custom-id>] \
  [-o <output-path>]
```

## Supported formats

| Format | Description | Auto-detected from |
|--------|-------------|-------------------|
| `wechat` | WeChat 4.x JSON/CSV export | "wechat" in filename or structure |
| `json` | Generic `[{sender, time, content}]` array | `.json` extension or JSON content |
| `csv` | CSV with sender/time/content columns | `.csv` extension |
| `transcript` | Plain text `HH:MM Name: message` | Line patterns in content |

## Supported input structures

- Chat app exports (WeChat, LINE, Telegram plaintext)
- Interview transcripts (timestamps + speaker names)
- Meeting notes (alternating participants)
- Mail/email threads (converted to flat dialog)
- Twitter/Thread exports (author + timestamp + text)

## After import

The file goes to `~/.claude/projects/_imported/<filename>.jsonl`.

Tell the user:
```bash
# Then index it:
python3 $SD_ROOT/scripts/index-builder.py build --project ~/.claude/projects/_imported

# Then search normally:
sd-recall.py search "<keyword>" --scope all
```

## Limitations

- Does NOT decrypt WeChat's encrypted database (use wechat-local-vault for that first)
- Expects already-exported plaintext
- Filters non-text content (images, emoji-only, system messages)
- Max ~100K messages per file (split larger exports)
