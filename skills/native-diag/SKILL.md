---
name: native-diag
description: |
  调用各 AI 编码环境的原生诊断命令（codex doctor / grok inspect /
  kimi doctor / mimo debug），将输出结构化为统一 JSON 供 session-digger 索引。
  对 CLI 无法调用的命令（如 Claude /doctor）给出 fallback 提示。
  触发：native-diag、原生诊断、环境doctor、native doctor、
  调用原生命令、环境体检、native health
version: 0.1.0
---

# native-diag

> 一行调用、统一 JSON、可被索引。只做原生命令的 wrapper，不重写检查逻辑。

## 适用场景

- env-doctor 需要权威原生数据时
- 会话索引里要打入「环境健康度」维度时
- 用户问「某某环境是否正常」「能不能排查各环境的配置问题」时

## 各环境的原生命令

| 环境 | 命令 | 类型 | 输出 | 可被 CLI 调用 |
|------|------|------|------|--------------|
| Claude Code | `/doctor` | 会话内斜杠 | text | 否（仅 fallback） |
| Codex | `codex doctor --json` | CLI flag | structured_json | 是 |
| Grok | `grok inspect --json` | CLI flag | structured_json | 是 |
| Kimi Code | `kimi doctor config\|tui` | CLI subcommand | text | 是 |
| MiMo Code | `mimo debug config\|paths` | CLI subcommand | text | 是（组合调用） |

## 调用方式

```bash
# Path resolution（与现有子技能一致）
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"

# 诊断单个环境
python3 "$SD_ROOT/skills/native-diag/scripts/native-diag.py" --env codex

# 诊断全部环境
python3 "$SD_ROOT/skills/native-diag/scripts/native-diag.py" --env all

# 输出给 jq / env-doctor 消费
python3 "$SD_ROOT/skills/native-diag/scripts/native-diag.py" --env all | jq .
```

## 统一输出 schema

```json
{
  "env": "codex",
  "native_cmd": "codex doctor --json",
  "overall_status": "fail|warn|ok",
  "issues": [
    {
      "severity": "critical|warning|ok",
      "summary": "...",
      "remediation": "...",
      "check_id": "..."
    }
  ],
  "metrics": {
    "total_checks": 18,
    "fail_count": 3,
    "warn_count": 2
  },
  "notes": "可选说明"
}
```

`--env all` 时输出包一层汇总：

```json
{
  "overall_status": "fail|warn|ok",
  "summary": {"total": 5, "fail": 1, "warn": 1, "ok": 3},
  "environments": [<单环境结果>]
}
```

## 严重程度

| 级别 | 含义 | 来源 |
|------|------|------|
| critical | 需立即处理 | codex doctor status=fail / kimi doctor FAIL |
| warning | 建议处理 | codex doctor status=warn / hooks 过多 |
| ok | 提示/正常 | 静态 / CLI 无法调用时的说明 |

## Fallback 规则

- **Claude /doctor**：CLI 无法调用 → 只探测 `~/.claude` 目录是否存在，`overall_status=warn`，在 issues 里提示用户手动运行 `/doctor`
- **环境未安装**：命令不在 PATH → `overall_status=warn`，issue 提示安装
- **JSON 解析失败**：降级 `overall_status=warn`，在 issue 里给出版本排查建议

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 全部 ok 或仅 warn |
| 1 | 存在 critical / fail |
| 2 | 环境未安装（本版本未使用，留给未来扩展） |

## 路由：用户说什么 → 调哪个

| 用户说的 | 动作 |
|---------|------|
| 「原生诊断」「native doctor」 | `python3 native-diag.py --env all` |
| 「codex 是不是正常的」 | `python3 native-diag.py --env codex` |
| 「grok 环境查一下」 | `python3 native-diag.py --env grok` |
| 「kimi 配置有问题吗」 | `python3 native-diag.py --env kimi` |
| 「mimo 的状态」 | `python3 native-diag.py --env mimo` |
| 「所有环境都跑一遍」 | `python3 native-diag.py --env all` |
| 「claude 能不能查」 | 提示用户运行 `/doctor`，说明 native-diag 对 claude 仅作 fallback |

## 和 env-doctor 的分工

- **native-diag**：调用原生命令、结构化输出、存入索引
- **env-doctor**：读取 capabilities.json → 调度 native-diag + 盲区脚本 → 综合报告

env-doctor 是 native-diag 的上游调度者；native-diag 是 env-doctor 的数据采集器。

## DO NOT

- 不重写原生命令的检查逻辑 —— 调用它、信任它
- 不在输出中暴露 API Key / token 完整值（remediation 可能含路径时截断）
- 不自行判断「hooks 过多」的阈值超过 20 —— 直接输出数值让模型决定
- 不承诺能发现所有问题 —— 只做原生命令能做到的事
- 不对 Claude /doctor 给「已检查」假象 —— 必须提示用户手动运行
