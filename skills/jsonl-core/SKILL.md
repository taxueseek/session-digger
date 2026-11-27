---
name: jsonl-core
description: This skill should be used when the user asks to "analyze conversation history", "parse JSONL files", "read past sessions", "search conversation logs", "find what happened in a session", or needs to work with Antigravity, DSH, DimCode, ZCode, Claude Code, Grok Build, Kimi Code, or Reasonix conversation data. It provides the canonical multi-format parsing infrastructure for session-digger agents.
version: 0.4.1
---

# JSONL Core — Multi-Environment Parsing Infrastructure

## Architecture & Single-Pass Protocol

All parsing lives in `$SD_ROOT/scripts/echolib/` (`SD_ROOT="$(scripts/resolve_root.sh)"`, Python 3.9+ stdlib only) and indexes into `~/.session-digger/index.db` via `index-builder.py`.

```python
import echolib
parsed = echolib.dispatch_parse_session(path_or_uri)  # single pass: stats + messages + tools
# Compatible incremental helpers:
msgs = echolib.dispatch_extract_messages(path_or_uri, role="user", limit=20)
tools = echolib.dispatch_extract_tools(path_or_uri, errors_only=True)
agent = echolib.dispatch_resolve_agent(path_or_uri)
```

## Data Locations (5 Core Agent Families + Unified Index)

| Environment | Source Path / URI | Adapter / Notes |
| --- | --- | --- |
| **Unified Index (SSOT)** | `~/.session-digger/index.db` | FTS5 (`USER`/`ASSISTANT`/`TOOL`), `session_role`, token splits |
| **Antigravity** | `~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript.jsonl` + `~/.gemini/antigravity/brain/` | Strips metadata noise; model vote from `PLANNER_RESPONSE.model`; `conversation_summaries.db` |
| **DSH (DeepSeek)** | `~/.dsh/sessions/<slug>/<dir>/session[.vN].jsonl.zstd` | Streaming zstd decode; checks `message.isError` & `content[].isError` |
| **DimCode / DIM** | `~/.dimcode/v2/dimcode.sqlite` (`dimcode://<id>`) + `~/.dim/memory/` | Reads `tool_use`/`tool_result`, `session_relations`, `compaction_states`, `usage_skill_events` |
| **ZCode** | `~/.zcode/cli/db/db.sqlite` (`zcode://<id>`) + `~/.zcode/v2/` + `~/.zcode/cli/agents/` | Thread-local RO SQLite pool (`get_ro_sqlite`); model billing from `model_usage` |
| **Claude Code** | `~/.claude/projects/<encoded-path>/<uuid>.jsonl` | Strictly read-only; fallback cache written to `~/.session-digger/cache/claude-index/` |

Additional hosts (`grok`, `kimi_code`, `codex`, `cursor`, `workbuddy`, `trae_cn`, `qwen`, `reasonix`) are registered in `echolib._registry_data.ENV_REGISTRY`; see `references/record-types.md`, `references/workbuddy-record-types.md`, and `references/extraction-patterns.md`.

## Fast Path First (CLI)

Always query the index before opening raw transcripts or databases:

```bash
python3 "$SD_ROOT/scripts/index-builder.py" build               # incremental FTS sync (~1s warm)
python3 "$SD_ROOT/scripts/sd-recall.py" search "<term>" --scope all --limit 20
python3 "$SD_ROOT/scripts/sd-recall.py" sessions --scope all --agent cross --limit 20
python3 "$SD_ROOT/scripts/sd-recall.py" sessions --scope all --agent zcode --limit 20   # zcode family
python3 "$SD_ROOT/scripts/sd-recall.py" sessions --scope all --agent dim --limit 20     # dim + dimcode
python3 "$SD_ROOT/scripts/sd-recall.py" stats
python3 "$SD_ROOT/scripts/topic-segmenter.py" <path_or_uri>
```

TSV columns from `sessions`: `SESSION_ID  CREATED  MODIFIED  MSG_COUNT  BRANCH  SUMMARY  FIRST_PROMPT  PROJECT_PATH  FULL_PATH` (column 9 is file path or virtual URI `zcode://...` / `dimcode://...`).

## Performance & Parsing Rules

- **Index search**: SQLite FTS5 across `USER`/`ASSISTANT`/`TOOL` (<200ms across 8k+ sessions).
- **Virtual URIs**: `zcode://` and `dimcode://` are resolved transparently by `echolib.dispatch_*` and `skills/common_paths.py:resolve_session_ref()`. Never pass virtual URIs to raw `cat`/`grep`.
- **Single-pass vs compat**: Call `echolib.dispatch_parse_session(ref)` when needing messages, tools, and stats together.
- **Noise filtering**: `skip_noise=True` automatically skips `progress`, `queue-operation`, `isMeta`, `isCompactSummary`, `<synthetic>` models, Antigravity `<ADDITIONAL_METADATA>`, and pure `tool_result` wrapper entries.
- **Schema drift check**: Run `echolib.detect_schema(path)` or invoke `schema-scout` when unexpected record layouts appear.

## 下游协作

| 触发条件 | 推荐 |
|---|---|
| 解析完会话数据，需要提炼经验 | `experience-synthesis` |
| 单会话错误根因与意图分析 | `deep-analysis` |
| 需要管理/归档记忆文件 | `memory-management` |
| 需要结合 git 历史交叉分析 | `references/git-mining.md` |

## DO NOT

- 从零创建新 skill → 外部 `manage-skills` / `taxue-build`
- 需要提炼经验/教训 → `experience-synthesis`（jsonl-core 只做解析，不做分析）
- 需要管理记忆/规则文件 → `memory-management`
- 禁止修改 `~/.claude/`、`~/.gemini/`、`~/.dsh/`、`~/.dimcode/`、`~/.zcode/` 原始会话数据
