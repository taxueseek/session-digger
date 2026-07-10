---
name: skill-insight
description: |
  技能使用洞察与 skill 资产自检。基于会话索引分析技能使用情况：哪些技能高频使用、
  哪些从未被调用；并对 session-digger 自身做路由覆盖/硬编码/安装漂移检查。
  触发：技能使用分析、技能洞察、哪些技能没用过、技能清理建议、
  技能使用习惯、我的技能用得多吗、skill 健康度、路由有没有漏、安装版本漂移
version: 0.2.0
---

# Skill Insight — 技能使用洞察 + 资产自检

> 会话侧只读 SQLite；资产侧用 `scripts/skill-health.py`（改进 skill 的 skill）。

## 0. 先跑资产自检（改进 session-digger 自身）

```bash
python3 $SD_ROOT/scripts/skill-health.py
python3 $SD_ROOT/scripts/skill-gap-finder.py analyze --min-occurrences 5
```

`skill-health` 检查：命令是否进路由表、是否含个人路径硬编码、combo_map 覆盖、安装副本 SHA 漂移、Herdr skill-gap 接线。
`skill-gap-finder` 检查：跨会话痛点 → SKILL 提案（默认脱敏，无用户名/绝对路径）。

## 数据源

1. **会话索引**：`~/.claude/.session-digger/index.db`（或 `SESSION_DIGGER_DATA_DIR` 若已配置）
   - `sessions.tool_usage_json` — 每个会话的工具调用统计
   - `messages_fts` — 全文搜索，用于检查技能名是否在历史对话中出现
2. **已安装技能**（多根探测，勿只扫单一目录）：
   - `~/.agents/skills/`
   - `~/.claude/skills/`
   - `~/.grok/skills/`（若存在）

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

- 不自动删除技能——只提建议，由用户决定
- 不做实时技能推荐——这是分析技能，不是推荐引擎
- 不与 `/optimize` 抢职责——`/optimize`=会话痛点；本技能=使用统计 + 资产健康度
- 不在输出中粘贴含用户名的绝对路径——优先 session id
