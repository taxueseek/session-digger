---
name: memory-management
description: |
  Use when auditing, extracting, pruning, or writing Claude Code memory files.
  Triggers: "memory management", "记忆管理", "记忆格式", "MEMORY.md", "记忆文件",
  "how to save memories", "how to organize memories", "记忆归档".
  Covers memory file format, MEMORY.md index conventions, staleness scoring,
  claim extraction heuristics, destination routing, mutation rules, and archive conventions.
version: 0.2.0
---

# Memory Management

## Memory File Format

Frontmatter schema (simple key: value, NOT general YAML):

    ---
    name: <string, required>           # short identifier
    description: <string, required>    # one-line summary for relevance matching
    type: <enum, required>             # value | user | feedback | project | reference | insight
    ---

- All values are plain strings, no quoting needed unless value contains `:`
- No nested structures, no lists, no multi-line values
- Body follows after the closing `---` delimiter as markdown

## MEMORY.md Index Format

- Plain markdown file, no frontmatter
- Contains links to individual memory files with brief descriptions
- Lines after 200 are truncated by Claude Code's loader — keep concise
- Entry format: `- [filename.md](filename.md) — brief description`
- When writing: create individual .md file first, then append entry

## Staleness Scoring

Exponential decay: `score = 100 * (1 - exp(-age_days * ln(2) / half_life))`

| Type | Half-life | Score 50 at | Score 90 at |
|------|-----------|-------------|-------------|
| value | 365 days | 365d | ~1213d |
| insight | 180 days | 180d | ~598d |
| user | 180 days | 180d | ~598d |
| feedback | 90 days | 90d | ~299d |
| reference | 60 days | 60d | ~199d |
| project | 14 days | 14d | ~47d |
| unknown | 30 days | 30d | ~100d |

insight 类型用于经验合成（experience-synthesis）产生的跨会话模式：
- "X 模式下 Y 工具效率最高"
- "反复出现 Z 模式，应提取为 skill"
- 有效期同 user 类型，但 deep verification 不适用（insight 无法验证文件/URL 是否存在）

Score-to-action: 0-50 = keep, 50-75 = review, 75-100 = prune.

Deep verification modifiers (additive, capped at 100):
- +20 if referenced file is missing
- +30 if referenced function/class not found
- +10 if URL returns non-200
- +15 if referenced branch doesn't exist

## Claim Extraction Heuristics (Deep Audit)

| Claim Type | Detection Pattern | Verification |
|------------|-------------------|--------------|
| File path | Contains `/`, ends with file extension | Glob for existence |
| Function/class | Backtick identifier in camelCase/PascalCase/snake_case | Grep in project |
| URL | Starts with `http://` or `https://` | curl HEAD request |
| Branch | After "branch" keyword or git pattern in backticks | `git branch -a` |
| Package | In dependency/package context | Grep in manifest files |

Skip generic descriptions that aren't verifiable (e.g., "use a database" vs "uses PostgreSQL 15").

## Destination Routing

| Destination | When to Use | Target Path |
|-------------|-------------|-------------|
| Memory file (value) | Learned value choices — "X is better than Y" | Session's project `memory/` dir, type=value |
| Memory file | Knowledge for Claude's future behavior | Session's project `memory/` dir |
| CLAUDE.md | High-impact instructions for every conversation | Session's project root CLAUDE.md |
| Knowledge file | Human-readable notes, decision logs | `docs/knowledge/` in project root |
| Skip | Session-specific, not worth preserving | — |

**Priority:** Value choices are the most durable memories. When extracting, surface them first. Facts decay (files move, APIs change); values persist (readability > cleverness survives any rewrite).

**Rule:** Always target the session's originating project, not the current shell cwd.

## Mutation Rules

| Layout | How to Write |
|--------|-------------|
| Index + files | Create .md file with frontmatter, append to MEMORY.md |
| Standalone MEMORY.md | Convert to index layout: create first .md file, rewrite MEMORY.md as index |
| No memory dir | Create `memory/`, create MEMORY.md, create .md file |
| Malformed frontmatter | Read as type=unknown. Never corrupt existing files. |

## Archive Convention

- Location: `memory/archive/` subdirectory
- Files preserved intact (frontmatter + content)
- MEMORY.md entry removed on archive
- `iter_memories()` skips `archive/` — invisible to dashboard/audit/tokens
- Standalone MEMORY.md: archiving not supported (offer delete or keep)
- Restore: manual move from `archive/` + re-add to MEMORY.md


## 下游协作

| 触发条件 | 推荐 |
|----------|------|
| 需要解析会话数据补充记忆 | `jsonl-core` |
| 需要从历史会话提炼经验再写入 | `experience-synthesis` |
| 需要结合 git 提交交叉引用 | `git-mining` |

## Dedup Heuristics

写入记忆前，先检查是否已存在相似内容：

1. **同名文件已存在**: 对比 body，若 Same → 跳过；若 Superset → 更新旧文件
2. **不同名但内容相似**: 用 keyword overlap (>60% shared rare words) 检测，合并为单一文件
3. **同一 insight 被 multiple sessions 重复提取**: 只保留最近一次（timestamp 最新），追加 source 行到 body 末尾

## CLI History Recovery

当 `~/.claude/history.jsonl` 中的 sessionId 在 `~/.claude/projects/` 找不到对应 JSONL 时：

1. 该会话已被 Claude Code 压缩/清理，但 `display` 字段保留了用户原始 prompt
2. 从 history.jsonl 提取 display + timestamp，可用于：
   - 识别反复出现的 prompt 模式（用户在问什么）
   - 估算会话丢失率（本环境 60%）
3. 工具: `parse-jsonl.sh ~/.claude/history.jsonl` 调用 jsonl-core 的 env-adapters 模块

## DO NOT

- 从零创建新 skill → `skill-creator`
- 需要提炼经验模式 → `experience-synthesis`（先提炼再写入）
- 需要解析原始会话数据 → `jsonl-core`
- 重复写入相同 insight → 先检查同名文件和内容 overlap
