---
name: jsonl-core
description: This skill should be used when the user asks to "analyze conversation history", "parse JSONL files", "read past sessions", "search conversation logs", "find what happened in a session", or needs to work with Claude Code, Grok Build, or Kimi Code conversation data. It provides the canonical parsing infrastructure for session-digger agents.
version: 0.3.0
---

# JSONL Core — Conversation Parsing Infrastructure

## Architecture

All parsing logic lives in `${CLAUDE_PLUGIN_ROOT}/scripts/echolib.py` — a single Python module (stdlib only, Python 3.6+). The shell scripts are thin wrappers around it.

## Data Locations

- **Session index (fast path)**: `~/.claude/projects/<encoded-path>/sessions-index.json`
- **Fallback index (built by session-digger)**: `~/.claude/projects/<encoded-path>/.session-digger-index.json`
- **Full conversations**: `~/.claude/projects/<encoded-path>/<uuid>.jsonl`
- **Subagent conversations**: `~/.claude/projects/<encoded-path>/<uuid>/subagents/agent-<id>.jsonl`
- **Global prompt history**: `~/.claude/history.jsonl`

The `<encoded-path>` is the project's absolute path with `/` replaced by `-` (e.g., `-Users-joker-github-myproject`).

**Important:** Only ~10% of projects have `sessions-index.json`. The scripts automatically build a fallback index from raw `.jsonl` files for the remaining 90%, cached in `.session-digger-index.json`.

## Strategy: Fast Path First

Always start with the index before opening any `.jsonl` file:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/list-sessions.sh "current" --limit 20
bash ${CLAUDE_PLUGIN_ROOT}/scripts/list-sessions.sh "all" --grep "search term" --limit 10
bash ${CLAUDE_PLUGIN_ROOT}/scripts/list-sessions.sh "/path/to/project"
```

Output is tab-separated: `SESSION_ID  CREATED  MODIFIED  MSG_COUNT  BRANCH  SUMMARY  FIRST_PROMPT  PROJECT_PATH  FULL_PATH`

The `FULL_PATH` field (9th column) is the absolute path to the `.jsonl` file. Use this to pass to other scripts.

Only open the full `.jsonl` when you need message-level detail.

## Canonical Parser

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/parse-jsonl.sh <file.jsonl> [options]
```

Key modes:
- **Schema detection** (check if format has changed):
  ```bash
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/parse-jsonl.sh <file.jsonl> --detect-schema
  ```
- **Filtered extraction** (skip noise, ~38% faster on large files):
  ```bash
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/parse-jsonl.sh <file.jsonl> --types user,assistant --skip-noise --limit 20
  ```
- **Field selection** (only extract specific fields):
  ```bash
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/parse-jsonl.sh <file.jsonl> --types user --fields timestamp,message --format tsv
  ```

## Convenience Scripts

All scripts are at `${CLAUDE_PLUGIN_ROOT}/scripts/`. They require only bash + python3 (stdlib only, no pip packages, minimum Python 3.6+). The git scripts additionally use git.

### Extract human-readable messages
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/extract-messages.sh <file.jsonl> [--role user|assistant|both] [--no-tools] [--limit N] [--thinking [LIMIT]]
```
Note: `--thinking` without a number shows full thinking blocks. `--thinking 500` truncates to 500 chars. Default: thinking blocks are hidden.

### Extract tool calls with results
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/extract-tools.sh <file.jsonl> [--tool NAME] [--errors-only] [--limit N]
```

### List files edited in a session
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/extract-files-changed.sh <file.jsonl> [--with-versions]
```
Uses reverse-read on large files (>50MB) to find the last snapshot efficiently.

### Quick session statistics (single-pass)
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/session-stats.sh <file.jsonl>
```

### Build fallback index
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/build-index.sh [project-path|"all"]
```
Pre-warm the cache for projects without `sessions-index.json`.

## Subagent Discovery

Sessions with subagent work have a `<session-uuid>/subagents/` directory. Check for it:
```bash
ls "$(dirname <full_path>)/$(basename <full_path> .jsonl)/subagents/" 2>/dev/null
```

Subagent files follow the same JSONL format and can be parsed with the same scripts.

## Performance Notes

- Python3 startup (80ms) dominates for files < 1MB (97% of all files)
- `--limit N` enables early exit — near-instant for small N
- `--skip-noise` avoids `json.loads` on progress/queue-operation lines by string pre-filter
- For files > 10MB: `json.loads` is the CPU bottleneck (63% of time), not I/O
- `extract-files-changed.sh` uses reverse-read on files > 50MB
- `session-stats.sh` counts errors in the same pass (no double-read)
- grep is NOT faster than Python for this format — avoid grep-then-parse pipelines

## When Scripts Are Not Enough

For targeted searches within large `.jsonl` files, use Grep directly:

```
# Find user messages containing a keyword (two-step workflow):
# Step 1: Find lines matching the record type
Grep pattern='"type":"user"' path="<file.jsonl>" output_mode="content"
# Step 2: From those results, visually scan or re-grep for your keyword.
#         Alternatively, combine both conditions in one regex:
Grep pattern='"type":"user".*keyword' path="<file.jsonl>" output_mode="content"

# Find error results
Grep pattern='"is_error"\s*:\s*true'

# Find specific tool usage
Grep pattern='"name"\s*:\s*"ToolName"'

# Find decisions (AskUserQuestion usage)
Grep pattern='"name"\s*:\s*"AskUserQuestion"'
```

## Record Type Quick Reference

See `references/record-types.md` for the complete schema. For WorkBuddy's tree-structured format, see `references/workbuddy-record-types.md`. The essential types:

| Type | What It Contains | When to Use |
|------|-----------------|-------------|
| `user` (string content) | Human's actual request | Understanding intent, finding topics |
| `assistant` (text blocks) | Claude's responses and reasoning | Finding decisions, explanations |
| `assistant` (tool_use blocks) | Tool invocations | Understanding what actions were taken |
| `file-history-snapshot` | Files edited with version counts | Knowing which files were touched |
| `summary` | AI-generated session title | Quick identification (also in sessions-index.json) |
| `system` (compact_boundary) | Context compaction marker | Session was long enough to need compaction |

## Finding the Right Session Directory

To map a project path to its Claude session directory:
1. Take the absolute project path (e.g., `/Users/joker/github/myproject`)
2. Replace all `/` with `-` → `Users-joker-github-myproject`
3. Prepend `-` → `-Users-joker-github-myproject`
4. Look in `~/.claude/projects/-Users-joker-github-myproject/`

If unsure, use `list-sessions.sh` which handles the lookup automatically (including fuzzy matching via `sessions-index.json` originalPath).

## Noise Filtering

When reading raw `.jsonl`, skip these:
- Records where `type` is `progress` or `queue-operation` (streaming/internal bookkeeping)
- User records with `isMeta: true` (slash command injection)
- User records with `isCompactSummary: true` (auto-generated context, not human input)
- Assistant records with `model: "<synthetic>"` (passthrough, not real inference)
- User records where `content` is an array of `tool_result` blocks (tool outputs, not human messages)

Use `--skip-noise` with `parse-jsonl.sh` for automatic noise filtering, or use the convenience scripts which handle this internally.

## Schema Evolution Awareness

Claude Code evolves rapidly. The JSONL format has changed across versions:
- New record types appear silently (e.g., `progress` at v2.1.14, `pr-link` later)
- New optional fields are added to existing records (~3-5 per minor version)
- Some record types (`summary`, `pr-link`, `file-history-snapshot`) lack common fields like `version` or `uuid`
- The directory encoding is lossy for Unicode paths — use `sessions-index.json`'s `originalPath` field as ground truth

When parsing results look unexpected, use the schema-scout agent or run:
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/parse-jsonl.sh <file.jsonl> --detect-schema
```
to check for unknown record types or field changes.

## Multi-Agent Support

Session-digger supports parsing sessions from multiple agents. Use `detect_agent_type()` to identify the agent:

```bash
python3 -c "from echolib import detect_agent_type; print(detect_agent_type('/path/to/session'))"
```

### Grok Build Session Format

Grok stores sessions under `~/.grok/sessions/<url-encoded-cwd>/<session-id>/` with a different file layout:

```
~/.grok/sessions/<encoded-cwd>/<session-id>/
  summary.json            # metadata: title, timestamps, model, message count
  chat_history.jsonl      # raw messages (system/user/assistant + tool_use/tool_result)
  events.jsonl            # event stream (turn_started, tool_started, tool_completed)
  signals.json            # pre-aggregated stats (token usage, tool counts, errors)
  rewind_points.jsonl     # file snapshots for /rewind
  compaction_checkpoints/ # auto-compact saved state
  subagents/              # child session directories
```

Key differences from Claude Code:
- **Tool calls in chat_history.jsonl**: Grok stores `tool_use` blocks in assistant messages and `tool_result` as separate messages, all within `chat_history.jsonl`. Use `grok_extract_tools()` instead of `extract_tools()`.
- **No file-history-snapshot**: Grok has no equivalent. Use `rewind_points.jsonl` for file change tracking.
- **Pre-aggregated stats**: `signals.json` contains token counts, tool call counts, etc. Use `grok_session_stats()` instead of `session_stats()`.
- **FTS5 search**: `session_search.sqlite` provides full-text search. Use `grok_list_sessions()` with keyword parameter.
- **No is_error field**: Grok's tool_result uses `outcome: "success"/"failure"` instead of `is_error: true/false`.
- **No gitBranch field**: Grok sessions don't track git branch info.

Grok adapter functions in echolib.py:
- `detect_agent_type(path)` — returns "claude", "grok", "both", or "unknown"
- `grok_list_sessions(cwd, limit, keyword)` — list/search Grok sessions
- `grok_session_stats(session_dir)` — read pre-aggregated stats
- `grok_extract_messages(session_dir, role, limit)` — extract from chat_history.jsonl
- `grok_extract_tools(session_dir, tool_filter, errors_only, limit)` — extract tool calls from chat_history.jsonl
- `grok_session_path(cwd, session_id)` — find a Grok session directory

### Kimi Code Session Format

Kimi Code stores sessions under `~/.kimi/sessions/<project-hash>/<session-uuid>/`:

```
~/.kimi/sessions/<project-hash>/<session-uuid>/
  wire.jsonl       # raw event stream (TurnBegin, ToolCall, ToolResult, ContentPart, ...)
  context.jsonl    # context management records (_checkpoint, compaction boundaries)
  metadata.json    # session_id, title, wire_mtime
  state.json       # optional: model, agent state
  context_sub_N.jsonl  # sub-context files from compaction
  subagents/       # child session directories
```

Key differences from Claude Code:
- **Top-level type field**: Kimi stores message type in `message.type` (e.g. `TurnBegin`, `ToolCall`, `ContentPart`), not in the record-level `type`.
- **User input in TurnBegin**: User messages are nested in `TurnBegin.payload.user_input[].text`. Use `kimi_extract_messages()`.
- **Tool calls by ID**: ToolCall and ToolResult are matched by `tool_call_id` (exact match, not proximity). Use `kimi_extract_tools()`.
- **wire_mtime for timestamps**: Session timestamp comes from `metadata.json`'s `wire_mtime` field (Unix float), not from individual message timestamps.
- **No is_error field**: Kimi's ToolResult doesn't expose error status. Tool failures are not distinguishable from successes in the wire format.
- **No gitBranch field**: Kimi sessions don't track git branch info.

Kimi adapter functions in echolib.py:
- `kimi_list_sessions(cwd, limit, keyword)` — list/search Kimi sessions
- `kimi_session_stats(session_dir)` — count messages, tools from wire.jsonl
- `kimi_extract_messages(session_dir, role, limit)` — extract from wire.jsonl
- `kimi_extract_tools(session_dir, tool_filter, errors_only, limit)` — extract tool calls from wire.jsonl
- `kimi_session_path(cwd, session_id)` — find a Kimi session directory

### Cross-Tool Unified Interface

For analysis across all three agents, use the unified functions:

- `cross_tool_list_sessions(limit, keyword, agent_filter)` — returns merged list of dicts with `agent`, `session_id`, `created`, `summary`, `first_prompt`, `msg_count`, `full_path`
- `cross_tool_session_stats(session_path)` — auto-detects agent type and dispatches

Shell script usage:
```bash
# List across all agents
bash ${CLAUDE_PLUGIN_ROOT}/scripts/list-sessions.sh "all" --agent cross --limit 20

# List only Kimi sessions
bash ${CLAUDE_PLUGIN_ROOT}/scripts/list-sessions.sh "all" --agent kimi --limit 20
```


## 下游协作

| 触发条件 | 推荐 |
|----------|------|
| 解析完会话数据，需要提炼经验 | `experience-synthesis` |
| 需要管理/归档记忆文件 | `memory-management` |
| 需要结合 git 历史交叉分析 | `git-mining` |

## DO NOT

- 从零创建新 skill → `skill-creator`
- 需要提炼经验/教训 → `experience-synthesis`（jsonl-core 只做解析，不做分析）
- 需要管理 MEMORY.md → `memory-management`
