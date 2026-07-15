---
name: deep-analysis
description: |
  会话深度分析子技能：错误根因提取 + 用户意图分类。
  从 JSONL 原文中提取具体错误消息并归类根因，
  对用户第一条消息做意图路由。
  触发：deep-analysis、错误根因、错误分类、意图分类、
  error root cause、intent classify、analyze session errors
version: 0.1.0
---

# deep-analysis

## 功能

| 脚本 | 输入 | 输出 |
|------|------|------|
| `scripts/error-root-cause.py` | session_id / jsonl 路径 / 文本 | 错误根因 JSON |
| `scripts/intent-classify.py` | session_id / jsonl 路径 / 文本 | 意图分类 JSON |

## 意图类型

| 类型 | 关键词示例 |
|------|-----------|
| ANALYSIS | 评估、分析、看看、怎么样、诊断 |
| COMPARISON | 对比、区别、优劣、优缺点、哪个 |
| OPTIMIZATION | 优化、改进、提升、修复、提高 |
| DEVELOPMENT | 开发、创建、增加、实现、构建 |
| DEBUGGING | 为什么、报错、失败、问题、错误 |
| REFLECTION | 回顾、总结、关键词、趋势、统计 |

## 调用方式

```bash
# 错误根因分析
python3 scripts/error-root-cause.py --session <session_id>
python3 scripts/error-root-cause.py --jsonl <path>

# 意图分类
python3 scripts/intent-classify.py --session <session_id>
python3 scripts/intent-classify.py --jsonl <path>
python3 scripts/intent-classify.py --text "分析这个项目的性能问题"
```

## 错误类型映射

| error_type | 匹配规则 |
|------------|----------|
| COMMAND_NOT_FOUND | command not found / No such file / not recognized |
| PERMISSION_DENIED | Permission denied / EACCES / access denied |
| TIMEOUT | timeout / timed out / deadline exceeded |
| EISDIR | EISDIR / is a directory |
| FILE_NOT_FOUND | File does not exist / No such file or directory |
| RATE_LIMIT | rate limit / 429 / too many requests |
| UNKNOWN | 其他 |

## Path resolution

```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
```

## 下游协作

| 发现 | 推荐 |
|------|------|
| 错误根因明确，需修复 | apply-rules / env-doctor |
| 意图为 DEBUGGING 且错误率高 | jsonl-core + experience-synthesis |
| 意图为 REFLECTION 且跨会话 | experience-synthesis |

## DO NOT

- 不替代 jsonl-core 的解析能力（deep-analysis 只做分析层）
- 不在输出中修改用户原文
- 不输出表情符号
