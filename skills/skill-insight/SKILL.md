---
name: skill-insight
description: |
  技能使用洞察。基于会话索引分析技能使用情况：哪些技能高频使用、哪些从未被调用、
  环境和习惯层面有什么改进空间。作为 session-digger 的扩展能力，按需触发。
  触发：技能使用分析、技能洞察、哪些技能没用过、技能清理建议、
  技能使用习惯、我的技能用得多吗
version: 0.1.0
---

# Skill Insight — 技能使用洞察

> 复用已有索引，不引入新脚本。只读 SQLite + 扫描 SKILL.md。

## 数据源

1. **会话索引**：`~/.claude/.session-digger/index.db`
   - `sessions.tool_usage_json` — 每个会话的工具调用统计
   - `messages_fts` — 全文搜索，用于检查技能名是否在历史对话中出现
2. **已安装技能**：`~/.claude/skills/` 下所有 `SKILL.md` 的 `name` 和 `description`

## 分析方法

### 1. 技能使用统计

扫描已安装的 SKILL.md 获取技能清单，对每个技能名在 `messages_fts` 中搜索，
统计有多少个会话提到了该技能名。

```bash
# 获取已安装技能列表（name + description）
find ~/.claude/skills -name "SKILL.md" -exec grep -H "^name:" {} \;

# 查某个技能在历史对话中的出现次数
python3 -c "
import sqlite3; from pathlib import Path
conn = sqlite3.connect(str(Path.home() / '.claude/.session-digger/index.db'))
count = conn.execute('SELECT COUNT(DISTINCT session_id) FROM messages_fts WHERE messages_fts MATCH ?', ('技能名',)).fetchone()[0]
print(count)
"
```

### 2. 闲置技能识别

将「从未在任意会话中被提及」的技能列为闲置候选。注意：
- 有些技能通过 `Skill` 工具间接调用，检查 `tool_usage_json` 中的 `"Skill"` 计数
- 季节性技能（如年报相关）可能间歇使用，不急于清理
- 清理建议归档到 `.trash/` 而非直接删除

### 3. 工具使用模式

从 `tool_usage_json` 汇总所有会话的工具调用分布，识别：
- 高频工具：核心工作流（Bash、Read、Write、Edit 等）
- 领域工具：特定技能触发（如 `mcp__*` 前缀的工具）
- 通用工具不纳入技能推荐范围

```bash
# 汇总全局工具使用
python3 -c "
import sqlite3, json; from pathlib import Path
from collections import Counter
conn = sqlite3.connect(str(Path.home() / '.claude/.session-digger/index.db'))
tc = Counter()
for r in conn.execute('SELECT tool_usage_json FROM sessions WHERE tool_usage_json IS NOT NULL AND tool_usage_json != \"{}\"').fetchall():
    tc.update(json.loads(r[0] or '{}'))
for name, count in tc.most_common(20):
    print(f'{name:35s} {count:5d}')
"
```

### 4. 环境与习惯洞察

从索引中按环境分组统计：
- 错误率：`errors / tool_calls`，超过 10% 需关注
- 标记率：有 flags 的会话占比，超过 30% 说明长对话或重试循环多
- 平均消息数：超过 30 条说明会话偏长，建议拆分任务
- 主力环境：会话数最多的环境，优先优化其配置

## 输出原则

1. **数据驱动**：每条建议附带具体数字（「X 个会话、Y% 错误率」）
2. **可执行**：建议能直接操作（「归档到 .trash/」「拆分任务」「检查 SKILL.md」）
3. **不画蛇添足**：没有问题不编造建议，数据不足就直说
4. **区分优先级**：高错误率 > 闲置技能清理 > 使用习惯优化

## 下游协作

| 触发条件 | 推荐 |
|----------|------|
| 发现闲置技能需清理 | `memory-management` + `/audit` |
| 发现技能差距模式 | `/optimize` |
| 发现环境配置问题 | 检查对应环境的 CLAUDE.md |

## DO NOT

- 不创建新脚本——复用 `sd-recall.py`、`index-builder.py` 已有能力
- 不自动删除技能——只提建议，由用户决定
- 不做实时技能推荐——这是分析技能，不是推荐引擎
- 不重复 `/optimize` 的功能——`/optimize` 做差距分析，本技能做使用统计
