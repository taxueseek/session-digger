---
name: index
description: |
  Build or update the session-digger search index for fast queries.
  Triggers: "build index", "index sessions", "rebuild index", "刷新索引", "建立索引".
argument-hint: [--rebuild] [--agent cross|claude|grok|kimi_code|codex|workbuddy|trae_cn|zcode|dim|reasonix]
allowed-tools: Bash
---

Build or update the pre-computed SQLite FTS index for fast session search.

Arguments: $ARGUMENTS

**Script path discovery:**
```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
[[ -z "$SD_ROOT" ]] && [[ -d "$HOME/.agents/skills/session-digger" ]] && SD_ROOT="$HOME/.agents/skills/session-digger"
```

## What the index provides

- **Full-text search** in <50ms (vs seconds of file scanning)
- **Pre-computed stats** on every session (no re-parsing)
- **Topic boundaries** auto-detected within sessions
- **Incremental updates**: only re-indexes changed files (mtime check)

## Usage

```bash
# Build/update index (incremental: only changed files)
python3 $SD_ROOT/scripts/index-builder.py build $ARGUMENTS

# Full rebuild (re-parse everything)
python3 $SD_ROOT/scripts/index-builder.py build --rebuild $ARGUMENTS

# View index stats
python3 $SD_ROOT/scripts/index-builder.py stats
```

Supported arguments:
- `--rebuild` — Force re-index all files (ignore mtime cache)
- `--agent cross` (default) | `claude` | `grok` | `kimi_code` | `codex` | `workbuddy` | `trae_cn` | `zcode` | `dim` | `reasonix` — Which session source to index

## After indexing

Tell the user:
- How many sessions indexed vs skipped (unchanged)
- How long it took
- Next: `/recall` searches will now use the fast FTS index

## Index location

`~/.claude/.session-digger/index.db` (SQLite with FTS5)

Set up automatic indexing: run `/index` after every session or register as a cron job.
