---
name: jsonl-core
description: This skill should be used when the user asks to "analyze conversation history", "parse JSONL files", "read past sessions", "search conversation logs", "find what happened in a session", or needs to work with Claude Code, Grok Build, Kimi Code, ZCode, DIM, or Reasonix conversation data. It provides the canonical parsing infrastructure for session-digger agents.
version: 0.4.0
---

# JSONL Core — Conversation Parsing Infrastructure

## Architecture

All parsing logic lives in `${CLAUDE_PLUGIN_ROOT}/scripts/echolib.py` — a single Python module (stdlib only, Python 3.6+). User-facing tools are `sd-recall.py` (search/list/stats), `index-builder.py` (FTS index), `topic-segmenter.py` (segmentation), `trend-engine.py` (longitudinal aggregation), `skill-gap-finder.py` (pain-point mining), `format-detector.py` (unknown agent format detection), `topic_classify.py` (topic routing), `chat-profiles.py` (group chat participant extraction), and `remember.py` (auto memory generation).

## Data Locations

- **Session index (fast path)**: `~/.claude/projects/<encoded-path>/sessions-index.json`
- **Fallback index (built by session-digger)**: `~/.claude/projects/<encoded-path>/.session-digger-index.json`
- **SQLite unified index**: `~/.claude/.session-digger/index.db` — auto-maintained by `index-builder.py`, stores session stats, tool usage JSON, timestamps for all environments
- **Full conversations**: `~/.claude/projects/<encoded-path>/<uuid>.jsonl`
- **Subagent conversations**: `~/.claude/projects/<encoded-path>/<uuid>/subagents/agent-<id>.jsonl`
- **Global prompt history**: `~/.claude/history.jsonl`

**Important:** Only ~10% of projects have `sessions-index.json`. The scripts automatically build a fallback index from raw `.jsonl` files for the remaining 90%, cached in `.session-digger-index.json`.

## Strategy: Fast Path First

Always start with the index before opening any `.jsonl` file:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sd-recall.py sessions --scope current --limit 20
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sd-recall.py search "search term" --limit 10
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sd-recall.py sessions --scope path --limit 20
```

Output is tab-separated: `SESSION_ID  CREATED  MODIFIED  MSG_COUNT  BRANCH  SUMMARY  FIRST_PROMPT  PROJECT_PATH  FULL_PATH`

The `FULL_PATH` field (9th column) is the absolute path to the `.jsonl` file. Use this to pass to other scripts.

Only open the full `.jsonl` when you need message-level detail.

## Primary Tools

All parsing lives in `${CLAUDE_PLUGIN_ROOT}/scripts/echolib.py`. Use these front-ends:

```bash
# Build fast FTS search index (one-time, ~1-2s)
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/index-builder.py build

# Search across all sessions (FTS, <50ms with index)
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sd-recall.py search "keyword" --limit 10

# List sessions
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sd-recall.py sessions --scope all --limit 20

# Aggregate statistics
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sd-recall.py stats

# Topic segmentation per session
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/topic-segmenter.py <file.jsonl>

# Pattern analysis (retries, errors, user corrections)
bash ${CLAUDE_PLUGIN_ROOT}/scripts/analyze-session.sh <file.jsonl>
```

### Direct Python API

```python
import echolib
for msg in echolib.extract_messages("file.jsonl", role="user", limit=20):
    print(msg["text"])
for tool in echolib.extract_tools("file.jsonl", errors_only=True):
    print(tool["name"], tool["status"])
stats = echolib.session_stats("file.jsonl")
```

## Subagent Discovery

Sessions with subagent work have a `<session-uuid>/subagents/` directory. Check for it:
```bash
ls "$(dirname <full_path>)/$(basename <full_path> .jsonl)/subagents/" 2>/dev/null
```

Subagent files follow the same JSONL format and can be parsed with the same scripts.

## Performance Notes

- With FTS index: keyword search <50ms regardless of total sessions
- `index-builder.py` incremental update: only re-parses changed files
- `extract_messages(limit=N)` enables early exit — near-instant for small N
- `iter_records(skip_noise=True)` avoids `json.loads` on noise lines
- For files > 10MB: `json.loads` is the CPU bottleneck (63% of time), not I/O
- `extract_files_changed()` uses reverse-read for large files
- `session_stats()` single-pass: counts errors, tokens, compactions together

## When Grep Is Enough

For targeted searches when no FTS index is built yet:

```
# Find user messages containing a keyword
Grep pattern='"type":"user"' path="<file.jsonl>" output_mode="content"

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

Use `sd-recall.py sessions` which handles lookup automatically:

## Noise Filtering

When reading raw `.jsonl`, skip these:
- Records where `type` is `progress` or `queue-operation` (streaming/internal bookkeeping)
- User records with `isMeta: true` (slash command injection)
- User records with `isCompactSummary: true` (auto-generated context, not human input)
- Assistant records with `model: "<synthetic>"` (passthrough, not real inference)
- User records where `content` is an array of `tool_result` blocks (tool outputs, not human messages)

This is handled automatically by `echolib.py` functions (`skip_noise=True` by default).

## Schema Evolution Awareness

Claude Code evolves rapidly. The JSONL format has changed across versions:
- New record types appear silently (e.g., `progress` at v2.1.14, `pr-link` later)
- New optional fields are added to existing records (~3-5 per minor version)
- Some record types (`summary`, `pr-link`, `file-history-snapshot`) lack common fields like `version` or `uuid`
- The directory encoding is lossy for Unicode paths — use `sessions-index.json`'s `originalPath` field as ground truth

When parsing results look unexpected, use `echolib.detect_schema("file.jsonl")` or the schema-scout agent to check for unknown record types.

## Multi-Agent Support

Session-digger supports parsing sessions from multiple agents. Use `detect_agent_type()` to identify the agent:

```bash
python3 -c "from echolib import detect_agent_type; print(detect_agent_type('/path/to/session'))"
```

### Grok Build Session Format

Grok stores sessions under `~/.grok/sessions/<url-encoded-cwd>/<session-id>/` with a different file layout:

```
$GROK_HOME/sessions/<encoded-cwd-or-slug>/<session-id>/   # GROK_HOME defaults to ~/.grok
  summary.json            # metadata: title, timestamps, model, message count
  chat_history.jsonl      # raw messages sent to the model
  updates.jsonl           # authoritative ACP session/update stream (resume source)
  events.jsonl            # internal event stream (tool_started/completed, turns)
  signals.json            # pre-aggregated stats (toolFailureCount, toolCallCount, …)
  plan.json               # in-session TODO state (when present)
  rewind_points.jsonl     # file snapshots for /rewind
  compaction_checkpoints/ # auto-compact saved state
  subagents/              # child session metadata; child transcripts live in sessions tree
  .cwd                    # (group dir only) original cwd when path encoding is rewritten
```

Key differences from Claude Code:
- **Data root**: honour `GROK_HOME` (official CLI), not only `~/.grok`.
- **Tool calls in chat_history.jsonl**: embedded `tool_calls` on assistant messages + `tool_result` rows. Use `grok_extract_tools()` (events.jsonl supplements timestamps/outcome).
- **Stats source of truth**: `signals.json` for activity counters (tools/messages/errors); **`updates.jsonl` → `params.update.usage`** for billable tokens (`inputTokens`/`outputTokens`/`cachedReadTokens`). Usage snapshots are run-cumulative; a drop in `modelCalls` starts a new run — digger sums last-of-each-run. Do **not** use `signals.contextTokensUsed` as billable input (that is window occupancy).
- **No file-history-snapshot**: use `rewind_points.jsonl` for file change tracking.
- **FTS5 search**: `session_search.sqlite` under the sessions root. Use `grok_list_sessions()` with keyword.
- **Outcome field**: tool_completed uses `outcome: "success"|"error"` (also accept `"failure"`).
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

For analysis across all agents, use the unified functions:

- `cross_tool_list_sessions(limit, keyword, agent_filter)` — returns merged list of dicts with `agent`, `session_id`, `created`, `summary`, `first_prompt`, `msg_count`, `full_path`
- `cross_tool_session_stats(session_path)` — auto-detects agent type and dispatches
- `scan_all_environments_parallel()` — probes all known agent directories in parallel, returns status per environment
- `dispatch_resolve_agent(path)` — maps a path to its agent type (claude, grok, kimi_code, codex, workbuddy, trae_cn, zcode, dim, reasonix, universal)

Use `sd-recall.py`:

```bash
# List across all agents
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sd-recall.py sessions --scope all --agent cross --limit 20

# List only Kimi sessions
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sd-recall.py sessions --scope all --agent kimi --limit 20
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
