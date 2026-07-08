---
name: topic-scan
description: |
  Scan all sessions, cluster by topic, show cost distribution, and route to corresponding taxue-* skills.
  Triggers: "topic-scan", "主题扫描", "话题聚类", "成本分布", "会话主题", "topic overview",
  "按主题分类", "哪些主题花最多钱", "topic clustering".
argument-hint: [--days N] [--format text|json] [--limit N] [--topic <编号>] [--session <path>]
allowed-tools: Bash, Read
---

Scan all conversation sessions and cluster them by topic, showing cost distribution and recommended routing to taxue-* skills.

Arguments: $ARGUMENTS

**Script path discovery:**
```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
[[ -z "$SD_ROOT" ]] && [[ -d "$HOME/.agents/skills/session-digger" ]] && SD_ROOT="$HOME/.agents/skills/session-digger"
```

## Mode detection

Parse $ARGUMENTS for mode:

1. **Overview mode** (default) — scan all sessions and show topic clusters:
```bash
bash "$SD_ROOT/scripts/topic-scan.sh" [--days N] [--format text|json] [--limit N]
```

2. **Topic detail mode** — extract context package for a specific topic:
```bash
bash "$SD_ROOT/scripts/topic-scan.sh" --topic <编号>
```

3. **Single session mode** — analyze one session:
```bash
bash "$SD_ROOT/scripts/topic-scan.sh" --session <path>
```

## Topic categories

Sessions are classified into 9 topics by `topic_classify.py`:

| ID | Topic | Routed Skill |
|----|-------|-------------|
| 1 | 投资分析 | `taxue-industry` |
| 2 | 内容创作 | `taxue-content` |
| 3 | 技能开发 | `taxue-skill` |
| 4 | 学习 | `taxue-learn` |
| 5 | 商业判断 | `taxue-business` |
| 6 | 职业发展 | `taxue-career` |
| 7 | 沟通表达 | `taxue-relate` |
| 8 | 流量增长 | `taxue-traffic` |
| 9 | 系统运维 | `taxue-meta` |
| 0 | 未分类 | `taxue-solve` |

## Output format

### text (default)
```
=== 会话主题总览 ===

  共 42 次会话 | 总成本 $87.50 | 2026-01-01 ~ 2026-07-01

请选择你想深入分析的主题（使用 `--topic <编号>`）：

[1] 投资分析
    15 次会话 | $45.20 (52%) | 2026-01-01 ~ 2026-07-01
    涉及：eastmoney, invest-stock, financial-report-analyst
    标的：000001, 600519
    建议路由技能：`taxue-industry`
...
```

### json
```json
{
  "total_sessions": 42,
  "total_cost": 87.50,
  "topics": [...]
}
```

## Next steps

After browsing topics, use `--topic <编号>` to extract a context package, then route to the corresponding taxue-* skill for deep analysis.
