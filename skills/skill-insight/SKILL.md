---
name: skill-insight
description: |
  技能使用洞察与 skill 资产自检。结合 DimCode 原生 usage_skill_events、
  index.db 工具统计与五大主力宿主安装视图，分析技能频次、闲置候选与副本漂移。
  触发：技能使用分析、技能洞察、哪些技能没用过、技能清理建议、
  技能使用习惯、我的技能用得多吗、skill 健康度、路由有没有漏、安装版本漂移
version: 0.3.0
---

# Skill Insight — 技能使用洞察 + 资产自检

> 一站式脚本化：资产自检与跨宿主用量分析统一走 `scripts/skill-health.py`。

## 1. 资产自检（路由 / 脱敏 / combo / 多宿主安装漂移）

```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-$HOME/.agents/skills/session-digger}"
python3 "$SD_ROOT/scripts/skill-health.py"
```

覆盖：
- `SKILL.md` 与 `references/command-map.md` 路由完整性
- 个人路径硬编码检查
- `combo_map.json` 覆盖率
- 跨宿主副本 SHA 漂移（`.agents` / `.claude` / `.gemini/antigravity-cli` / `.dsh` / `.zcode`）

## 2. 跨环境技能用量与工具习惯报告

```bash
python3 "$SD_ROOT/scripts/skill-health.py" --usage [--days 30]
python3 "$SD_ROOT/scripts/skill-gap-finder.py" analyze --min-occurrences 5
```

数据源：
1. **DimCode 原生技能事件表**：`dimcode.sqlite` 的 `usage_skill_events`（精确记录技能调用次数与会话数）
2. **统一索引 `index.db`**：`sessions.tool_usage_json`、`errors / tool_calls`、`avg_messages`
3. **多宿主安装目录**：`~/.agents/skills/`、`~/.claude/skills/`、`~/.gemini/antigravity-cli/skills/`、`~/.dsh/skills/`、`~/.zcode/skills/`

## 输出原则

1. **数据驱动**：每条建议附带具体数字（「X 个会话、Y% 错误率、Z 次原生调用」）
2. **可执行**：归档建议统一移入 `.trash/`，不直接删除
3. **区分优先级**：高错误率 > 闲置技能归档 > 跨宿主同步

## 下游协作

| 触发条件 | 推荐 |
|----------|------|
| 发现闲置技能需归档 | `memory-management` + `/audit` |
| 发现技能差距模式 | `/optimize` |
| 工具错误率高 / 需单会话根因 | `deep-analysis` |

## DO NOT

- 不自动删除技能或修改 `SKILL.md`
- 不在输出中暴露含个人用户名的绝对路径
