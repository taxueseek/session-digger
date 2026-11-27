---
name: experience-synthesis
description: This skill should be used when the user asks to "learn from past sessions", "extract lessons", "find what worked", "identify patterns in my workflow", "what mistakes did I make", "what decisions were made", or wants to synthesize wisdom and actionable insights from conversation history across Antigravity, DSH, DimCode, ZCode, and Claude Code. Integrates with `/trend` for longitudinal patterns and `/optimize` for skill-gap proposals.
version: 0.3.1
---

# Experience Synthesis — Learning from Past Sessions

## Insight Taxonomy (Summary)

Full signal patterns, sub-types, and examples live in `references/insight-taxonomy.md`. Extract insights ordered by durability (values first):

| # | Category | Durability | Primary Signals (`echolib` / `index.db`) |
|---|----------|------------|------------------------------------------|
| 1 | **Learned Values** | Highest | Explicit trade-offs ("prefer X over Y", "X > Y"), confirmed priorities |
| 2 | **Decisions** | High | Plan-mode exits, user choices, "use X instead of Y because..." |
| 3 | **Mistakes & Corrections** | High | Tool errors (`deep-analysis`), user reverts ("no, revert"), 2+ retries |
| 4 | **Effective Patterns** | Medium | Passing test/build loops, accepted fixes, positive user confirmation |
| 5 | **Anti-patterns** | Medium | Multi-attempt failures, frequent compactions, cancelled runs, edit thrash |
| 6 | **User Preferences** | Medium | Tool/model/workflow habits across `stats`, `model_usage`, first prompts |
| 7 | **Architecture Knowledge** | Medium | Design discussions, plan specs, recurring touched paths, dependencies |
| 8 | **Recurring Problems** | Medium | Repeated error classes or regression topics across sessions |
| 9 | **Performance & Cost Patterns** | Medium | Cache hit rate (`cache_read / input`), turn duration, token/compaction spikes |

## Synthesis Methodology

1. **Gather**: Use `sd-recall.py sessions --agent cross` and `sd-recall.py search <keyword>` (with `--tools` when inspecting tool failures).
2. **Pre-diagnose**: Run `deep-analysis` (`intent-classify.py --with-errors`) on noisy/failed sessions before generalizing.
3. **Trend check**: Run `/trend` to see whether the pattern is increasing, stable, or regressing.
4. **Cross-reference**: Verify recurrence across multiple sessions (`/optimize`); correlate code outcomes with `references/git-mining.md` when needed.
5. **Synthesize**: Emit only specific, evidenced, actionable insights — skip generic programming advice or rules already present in `AGENTS.md` / `CLAUDE.md`.

## Output Format

```markdown
## [Category]: [Brief Title]
**Sessions:** [session IDs / environments / dates]
**Context:** [What happened]
**Insight:** [Core takeaway]
**Evidence:** [Concrete tool/message counts or file citations]
**Action:** [Reusable rule, check, or skill change]
```

## 上游与下游协作

| 场景 / 触发条件 | 推荐 |
|------|------|
| 单会话失败 / 错误扎堆 / 意图不清 | 先跑 **`deep-analysis`**（`intent-classify.py --with-errors`） |
| 尚无索引或需要定位虚拟 URI | `/index`，定位走 `skills/common_paths.py:resolve_session_ref` |
| 需要解析原始会话数据 | `jsonl-core` |
| 需要结合 git 历史交叉验证 | `references/git-mining.md` |
| 提炼出重复模式，需要写入记忆/规则 | `memory-management` / `/apply`（人审） |
| 处理 Trae CN 会话中的 learned 项目 | `references/trae-cn-learned-mapping.md` |

## DO NOT

- 从零创建新 skill → 外部 `manage-skills` / `taxue-build`
- 需要解析原始会话数据 → `jsonl-core`（synthesis 只做提炼，不做解析）
- 需要管理记忆/规则文件 → `memory-management`
