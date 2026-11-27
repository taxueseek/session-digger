---
name: memory-management
description: |
  Use when auditing, extracting, pruning, or writing agent memory and rule files across Antigravity, DSH, DimCode, ZCode, and Claude Code.
  Triggers: "memory management", "记忆管理", "记忆格式", "MEMORY.md", "记忆文件",
  "how to save memories", "how to organize memories", "记忆归档".
  Covers memory file format, MEMORY.md index conventions, staleness scoring,
  claim verification, cross-host destination routing, dedup heuristics, and `remember.py`.
version: 0.3.1
---

# Memory Management

## Auto-Generated Memory (`remember.py`)

`scripts/remember.py` reads `~/.session-digger/index.db` and generates `memory/auto-stats.md`, `memory/auto-env-habits.md`, `memory/auto-skill-usage.md`, `memory/auto-errors.md`, and `memory/MEMORY.md`:

```bash
python3 scripts/remember.py --dry-run   # preview only
python3 scripts/remember.py               # rebuild all
python3 scripts/remember.py --only skill  # single category
```

## Memory File & Index Format

- **File frontmatter** (flat `key: value`, no nested YAML/lists): `name`, `description`, `type` (`value | user | feedback | project | reference | insight`).
- **`MEMORY.md` index**: plain markdown links `- [filename.md](filename.md) — brief description`. Keep under 200 lines. Create individual `.md` files before appending to `MEMORY.md`.

## Staleness Scoring & Deep Verification

Exponential decay: `score = 100 * (1 - exp(-age_days * ln(2) / half_life))` (`0–50 keep`, `50–75 review`, `75–100 prune`).

| Type | Half-life | Notes |
|------|-----------|-------|
| `value` | 365d | Durable preference rules; check first |
| `insight` / `user` | 180d | Skip missing-file penalties for abstract `insight` notes |
| `feedback` / `reference` | 90d / 60d | Add +20 missing file, +30 missing symbol, +10 bad URL, +15 missing branch (cap 100) |
| `project` / `unknown` | 14d / 30d | Short-lived milestone state |

## Cross-Host Destination Routing

Always target the **session's originating project or host**, not the incidental shell cwd.

| Destination | When to Use | Target Path |
|-------------|-------------|-------------|
| Global agent rules | Cross-env rules (Antigravity / DSH / ZCode / Claude) | `~/AGENTS.md` (single source of truth) |
| Project rules | High-impact repo instructions | Project root `AGENTS.md` or `CLAUDE.md` |
| DIM / DimCode memory | Persistent DIM insights | `~/.dim/memory.md` |
| ZCode rules | ZCode-specific overrides | `~/.zcode/AGENTS.md` |
| Claude memory dir | Claude project memory & `MEMORY.md` | Project `memory/` dir (`type=value` first) |
| Knowledge docs | Human architecture / decision logs | Project `docs/knowledge/` |

## Mutation, Archive & Dedup Rules

1. **Mutation**: Write new `.md` first, then update `MEMORY.md` index. Never corrupt malformed frontmatter (treat as `type=unknown`).
2. **Archive**: Move retired files to `memory/archive/` intact and remove their `MEMORY.md` link (`iter_memories()` skips `archive/`).
3. **Dedup**: Compare against existing filenames & >60% rare-word overlap before creating; merge recurring insights keeping the latest timestamp + source citations.
4. **Lost Transcript Recovery**: When an index/history prompt (e.g., `~/.claude/history.jsonl`) lacks a live transcript on disk, fall back to stored `display` / `first_prompt` + `sd-recall.py stats`.

## 下游协作

| 触发条件 | 推荐 |
|----------|------|
| 需要解析会话数据补充记忆 | `jsonl-core` |
| 需要从历史会话提炼经验再写入 | `experience-synthesis` |
| 需要结合 git 提交交叉引用 | `references/git-mining.md` |

## DO NOT

- 从零创建新 skill → 外部 `manage-skills` / `taxue-build`
- 需要提炼经验模式 → `experience-synthesis`（先提炼再写入）
- 需要解析原始会话数据 → `jsonl-core`
- 重复写入相同 insight → 先检查同名文件与内容重叠
