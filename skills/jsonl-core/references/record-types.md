---
description: Complete field reference for all JSONL record types in Claude Code conversation files
---

# JSONL Record Types — Complete Field Reference

## Common Fields (on most records)

| Field | Type | Description |
|-------|------|-------------|
| `uuid` | string | Unique ID for this record |
| `parentUuid` | string\|null | Parent in conversation tree (null = root) |
| `sessionId` | string | Session UUID this belongs to |
| `timestamp` | ISO 8601 | When this record was created |
| `type` | string | Record type discriminator |
| `cwd` | string | Working directory at time of message |
| `gitBranch` | string | Git branch (empty string if not a repo) |
| `slug` | string | Human-readable session name (e.g., "splendid-cooking-spark") |
| `version` | string | Claude Code version (e.g., "2.1.39") |
| `isSidechain` | boolean | True for subagent records |

## Record Type: `user`

Human input or tool result feedback.

**Key fields:**
- `message.role`: always `"user"`
- `message.content`: string (human text) OR array of content blocks
- `isMeta`: true for slash command injections
- `isCompactSummary`: true for auto-generated context after compaction
- `planContent`: present after exiting plan mode, contains the full plan text
- `todos`: array of `{content, status, activeForm}` — the live todo list
- `thinkingMetadata`: `{level, disabled, triggers}` — extended thinking config
- `permissionMode`: e.g., `"bypassPermissions"`

**Content variants:**
1. **String**: Simple human message — `"content": "fix the bug"`
2. **Array with tool_result**: `[{"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": false}]`
3. **Array with text**: `[{"type": "text", "text": "..."}]` — interrupted requests
4. **Array with image**: `[{"type": "text", ...}, {"type": "image", "source": {"type": "base64", ...}}]`

## Record Type: `assistant`

Model output with response content.

**Key fields:**
- `message.model`: model ID (e.g., `"claude-opus-4-6"`) or `"<synthetic>"` for passthrough
- `message.content[]`: array of content blocks
- `message.usage`: `{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}`
- `requestId`: Anthropic API request ID
- `isApiErrorMessage`: true if this was an error response

**Content block types:**
1. **text**: `{"type": "text", "text": "Claude's response..."}`
2. **thinking**: `{"type": "thinking", "thinking": "Let me analyze...", "signature": "..."}`
3. **tool_use**: `{"type": "tool_use", "id": "toolu_...", "name": "Read", "input": {...}}`

## Record Type: `system`

Meta-events. Discriminated by `subtype`:

| Subtype | Description | Key Fields |
|---------|-------------|------------|
| `turn_duration` | How long a turn took | `durationMs` |
| `compact_boundary` | Context was compacted | `logicalParentUuid`, `compactMetadata.preTokens` |
| `microcompact_boundary` | Lighter compaction | `microcompactMetadata.tokensSaved` |
| `stop_hook_summary` | Hook execution results | `hookCount`, `hookErrors[]`, `preventedContinuation` |
| `local_command` | Slash command executed | `content` (XML with command details) |
| `api_error` | API call failed | `error`, `retryInMs`, `retryAttempt` |
| `informational` | General info/warning | `content` |

## Record Type: `summary`

AI-generated session title. Appears at end of file (or standalone).

- `summary`: short descriptive title
- `leafUuid`: UUID of the last message in the conversation branch

A file can have multiple summary records (one per conversation branch).

## Record Type: `file-history-snapshot`

Tracks files edited during the session for undo support.

- `snapshot.trackedFileBackups`: object where keys are relative file paths, values have:
  - `backupFileName`: `"<hash>@v<N>"` — backup identifier
  - `version`: integer — number of times the file was edited
  - `backupTime`: ISO 8601

The **last** file-history-snapshot in a session has the complete list of all files touched.

## Record Type: `progress`

Real-time streaming events. Discriminated by `data.type`:

| data.type | Description |
|-----------|-------------|
| `hook_progress` | Hook executing (PreToolUse/PostToolUse) |
| `agent_progress` | Subagent task updates |
| `bash_progress` | Long-running bash output streaming |
| `mcp_progress` | MCP tool call started |
| `waiting_for_task` | Waiting for background task |
| `query_update` | Web search query issued |
| `search_results_received` | Web search results returned |

## Record Type: `pr-link`

GitHub PR created during session.

- `prNumber`, `prUrl`, `prRepository`

## Record Type: `queue-operation`

Background task queue management.

- `operation`: `"enqueue"` or `"remove"`
- `content`: JSON-encoded string with `task_id`, `description`, `task_type`

## Conversation Threading

Records form a tree via `parentUuid`. The root message has `parentUuid: null`.

**Finding the active conversation thread:** Walk backward from the most recent `assistant` record via `parentUuid` to reconstruct the canonical path.

**Branching occurs when:**
- Multiple tool calls execute in parallel (N tool_result children of one assistant)
- User retries/edits a message (abandoned branch has no children)
- Context compaction creates a new root (logicalParentUuid bridges the gap)

## sessions-index.json Format

```json
{
  "version": 1,
  "originalPath": "/path/to/project",
  "entries": [{
    "sessionId": "uuid",
    "fullPath": "/path/to/session.jsonl",
    "fileMtime": 1769684964176,
    "firstPrompt": "truncated first message (~180 chars)...",
    "summary": "AI-generated session title",
    "messageCount": 45,
    "created": "2026-01-25T00:25:01.743Z",
    "modified": "2026-01-25T00:50:38.337Z",
    "gitBranch": "main",
    "projectPath": "/path/to/project",
    "isSidechain": false
  }]
}
```
