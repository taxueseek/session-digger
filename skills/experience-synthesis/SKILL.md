---
name: experience-synthesis
description: This skill should be used when the user asks to "learn from past sessions", "extract lessons", "find what worked", "identify patterns in my workflow", "what mistakes did I make", "what decisions were made", or wants to synthesize wisdom and actionable insights from conversation history. Integrates with `/trend` for longitudinal patterns and `/optimize` for skill-gap proposals.
version: 0.3.0
---

# Experience Synthesis — Learning from Claude's Past

## Insight Taxonomy

When analyzing conversations, extract insights in these categories (ordered by durability — values first):

### 1. Learned Values
Comparative preferences and priority orderings — "X is better than Y".

**Signal sources:**
- User states "prefer X over Y", "X is better than Y", "X matters more than Y"
- User confirms a comparative statement from Claude ("yes, readability > cleverness")
- Trade-off discussions that resolve into a clear preference
- Statements with "prioritize", "choose X over Y", "the most important thing is"

**What to capture:** The value choice, both sides of the comparison, and the reasoning. Values are the most durable type of memory — they survive codebase rewrites.

### 2. Decisions
What was chosen, why, and what alternatives were rejected.

**Signal sources:**
- `AskUserQuestion` tool calls + the user's response in the next tool_result
- Plan mode content (`planContent` field on user records, `ExitPlanMode` tool calls)
- Thinking blocks where Claude weighs options
- Assistant text containing "I'll use X instead of Y because..."

**What to capture:** The decision, the rationale, the alternatives considered, and the context (what problem it solved).

### 3. Mistakes & Corrections
What went wrong, root cause, and how it was fixed.

**Signal sources:**
- Tool results with `is_error: true`
- Bash results containing: error, failed, FAIL, exit code 1, stack trace, traceback, Exception
- User corrections: "no, that's wrong", "revert that", "that broke X"
- Retry patterns: same tool called 2+ times on the same target with different inputs
- Reverted file edits (same file edited, then edited back)

**What to capture:** What failed, why it failed, what fixed it, how to avoid it next time.

### 4. Effective Patterns
Approaches that worked well and could be reused.

**Signal sources:**
- Successful test runs (Bash results with: passed, PASS, success, 0 errors)
- Successful builds (built in, compiled, no errors)
- PR creation (`pr-link` records)
- Git commits (successful completion of work)
- User satisfaction signals: "perfect", "great", "exactly what I needed"

**What to capture:** The approach, when it applies, why it worked.

### 5. Anti-patterns
Approaches that failed, were abandoned, or caused problems.

**Signal sources:**
- Sequences where multiple attempts fail before finding a working solution
- Sessions with many compactions (long, possibly struggling sessions)
- Tool calls that were user-interrupted
- Files edited many times in one session (version count > 3 in file-history-snapshot)

**What to capture:** What was tried, why it failed, what worked instead.

### 6. User Preferences
The user's preferred tools, styles, and workflows.

**Signal sources:**
- Tool usage frequency across sessions (which tools does the user/Claude use most?)
- Model choices in sessions (opus vs sonnet vs haiku patterns)
- Permission mode patterns
- Common first prompts (workflow entry points)
- Branch naming conventions

**What to capture:** The preference, evidence across sessions, strength of pattern.

### 7. Architecture Knowledge
System design decisions, component relationships, tech stack details.

**Signal sources:**
- Early messages in sessions (problem descriptions, requirement discussions)
- Plan mode content (architectural plans)
- File paths touched across sessions (reveals project structure)
- Dependencies installed or configured

**What to capture:** Component, its role, relationships, key decisions about it.

### 8. Recurring Problems
Issues that keep coming back across sessions.

**Signal sources:**
- Similar error messages appearing in different sessions
- Same files being edited for fixes repeatedly
- Similar user prompts ("fix X again", "the Y bug is back")

**What to capture:** The problem, frequency, root cause pattern, whether it has a permanent fix.

### 9. Performance & Cost Patterns
Token usage trends, session efficiency, cost optimization opportunities.

**Signal sources:**
- `sd-recall.py stats` output across sessions: token totals, compaction counts
- Turn duration from `system` records with `subtype: turn_duration`
- Cache hit ratios (cache_read vs cache_creation tokens)
- Model selection patterns (when opus vs sonnet is used)

**What to capture:** Trends, outliers, optimization opportunities.

## Synthesis Methodology

When extracting insights, follow this process:

1. **Gather**: Use `sd-recall.py sessions` to identify relevant sessions, then `sd-recall.py search <keyword>` for detail
2. **Trend check**: Run `/trend` to see if a pattern is increasing/decreasing over time
3. **Identify**: Look for signals from the taxonomy above
4. **Cross-reference**: Check if a pattern appears in multiple sessions (stronger signal). Use `/optimize` for automated pain-point detection
5. **Contextualize**: Combine with git history when available (what code resulted from the decision/mistake?)
6. **Synthesize**: Produce actionable insights, not just observations

## Output Format

Present insights in a structured format:

```
## [Category]: [Brief Title]
**Sessions:** [session IDs or dates]
**Context:** [What was happening]
**Insight:** [The key learning]
**Evidence:** [Specific data points]
**Action:** [What to do differently / what to keep doing]
```

## Insight Quality Criteria

Good insights are:
- **Specific**: "Use --follow with git log when tracing renamed files" not "git is useful"
- **Actionable**: Something you can apply in future sessions
- **Evidenced**: Backed by data from multiple sessions when possible
- **Contextual**: Includes when the insight applies (and when it doesn't)

Skip insights that are:
- Generic programming advice (not specific to this user/project)
- One-time occurrences that aren't likely to recur
- Already documented in the project's CLAUDE.md or README


## 上游（0.9.18 起优先）

| 场景 | 先做 |
|------|------|
| 单会话失败 / 错误扎堆 | **`deep-analysis`**（error-root-cause）再合成教训 |
| 用户意图不清 | **`deep-analysis`**（intent-classify）再归类经验 |
| 尚无索引 | `/index`，定位走 `skills/common_paths.py` |

## 下游协作

| 触发条件 | 推荐 |
|----------|------|
| 需要解析原始会话数据 | `jsonl-core` |
| 需要结合 git 历史交叉验证 | `git-mining` |
| 提炼出重复模式，需要写入记忆 | `memory-management` |
| 需要趋势验证（某模式是否在变多） | `/trend` |
| 需要自动化差距分析 | `/optimize` |
| 处理 Trae CN 会话中的 learned 项目 | 参考 `references/trae-cn-learned-mapping.md` |

## DO NOT

- 从零创建新 skill → `skill-creator`
- 需要解析 JSONL 原始数据 → `jsonl-core`（synthesis 只做提炼，不做解析）
- 需要管理 MEMORY.md 文件 → `memory-management`
