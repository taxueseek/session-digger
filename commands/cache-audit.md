---
name: cache-audit
description: |
  跨环境与模型缓存命中体检与诊断。
  分析特定环境或模型的会话均命中率、Token加权命中率、零缓存异常、主对话与子代理分布，并生成综合健康评分。
  触发词: cache-audit, 缓存体检, 缓存命中体检, 缓存命中率分析, 模型缓存水平, 环境缓存分析, 缓存诊断
argument-hint: [--env <name>] [--model <pattern>] [--live] [--json]
allowed-tools: Bash, Read
---

# /cache-audit — 缓存命中体检与诊断

跨环境与模型深度体检缓存命中水平（严格执行 `references/cache-report-rules.md` 强制口径）。

参数：`$ARGUMENTS`

```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
if [[ -z "$SD_ROOT" || ! -f "$SD_ROOT/scripts/cache-audit.py" ]]; then
  for _c in \
    "$HOME/.agents/skills/session-digger" \
    "$HOME/.claude/plugins/session-digger" \
    "$HOME/.claude/skills/session-digger" \
    "$HOME/.grok/skills/session-digger"
  do
    [[ -f "$_c/scripts/cache-audit.py" ]] && SD_ROOT="$_c" && break
  done
fi

python3 "$SD_ROOT/scripts/cache-audit.py" $ARGUMENTS
```

## 报告结构

1. **环境整体体检（Table A）**：有效会话、会话均命中率、Token加权、主对话与子代理分列、体检评级、诊断意见。
2. **模型缓存水平（Table B）**：模型名称归一、所处环境、有效会话数、会话均命中率、总消耗 Token。
3. **排除与异常清单（Exclusions & Anomalies）**：零缓存会话、无 usage 字段、有效会话 < 3 样本。
4. **诊断结论与优化建议**：高能效环境标记、特定环境（Antigravity / DSH / ZCode / Claude）体检专评、优化行动建议。
