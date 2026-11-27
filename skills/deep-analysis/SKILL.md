---
name: deep-analysis
description: |
  会话深度分析子技能：错误根因提取 + 用户意图分类。
  基于 echolib 统一解析与 index.db 快速定位，覆盖 Antigravity、DSH、DimCode、
  ZCode、Claude Code 等全部 20+ 环境。
  触发：deep-analysis、错误根因、错误分类、意图分类、
  error root cause、intent classify、analyze session errors、
  这次为啥失败、根因是什么、用户到底想干嘛
version: 0.2.0
---

# deep-analysis

> L2 分析专精：全环境复用 `echolib` + `index.db`，不自写解析器、不写记忆。

## 在 digger 架构中的位置

| 层 | 本技能 |
|----|--------|
| L0 PARSE | 委托 `echolib.dispatch_*`（支持 JSONL / `.zstd` / `zcode://` / `dimcode://`） |
| L1 INDEX | **只读** `index.db` 的 `jsonl_path` / `first_prompt` 快速通道 |
| L2–L3 | 错误根因分类 + 用户意图标签（供 `experience-synthesis` / `/optimize` 消费） |

## 脚本

| 脚本 | 输入 | 输出 |
|------|------|------|
| `scripts/error-root-cause.py` | `--session <id>` 或 `--jsonl <path\|uri>` | `{session_path, agent, total_errors, errors[], summary{}}` |
| `scripts/intent-classify.py` | `--session <id>` / `--jsonl <path\|uri>` / `--text <str>` | `{intent, confidence, keywords_matched[], all_scores{}}` |

## 意图类型

| 类型 | 关键词示例 |
|------|-----------|
| ANALYSIS | 评估、分析、看看、怎么样、诊断 |
| COMPARISON | 对比、区别、优劣、优缺点、哪个 |
| OPTIMIZATION | 优化、改进、提升、修复、提高 |
| DEVELOPMENT | 开发、创建、增加、实现、构建 |
| DEBUGGING | 为什么、报错、失败、问题、错误 |
| REFLECTION | 回顾、总结、关键词、趋势、统计 |

## 错误类型映射

| error_type | 匹配规则 |
|------------|----------|
| COMMAND_NOT_FOUND | command not found / not recognized |
| PERMISSION_DENIED | Permission denied / EACCES |
| TIMEOUT | timeout / deadline exceeded / ETIMEDOUT |
| CONTEXT_LIMIT | context length / token limit / prompt too long |
| RATE_LIMIT | rate limit / 429 / quota exceeded |
| EISDIR | EISDIR / is a directory |
| FILE_NOT_FOUND | File does not exist / ENOENT |
| USER_CANCELLED | User cancelled / Request interrupted |
| TEST_FAILURE | AssertionError / pytest failed |
| SYNTAX_OR_TYPE_ERROR | SyntaxError / TypeError / Traceback |
| NETWORK_ERROR | ECONNREFUSED / SSL / Redirect changed host |
| TOOL_ARG_ERROR | invalid argument / schema validation |
| UNKNOWN | 其他 |

## 调用方式

```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-$HOME/.agents/skills/session-digger}"

# 错误根因（--session 优先走 index.db；--jsonl 支持文件或 zcode:// / dimcode:// URI）
python3 "$SD_ROOT/skills/deep-analysis/scripts/error-root-cause.py" --session <session_id> --pretty
python3 "$SD_ROOT/skills/deep-analysis/scripts/error-root-cause.py" --jsonl <path_or_uri> --pretty

# 意图分类
python3 "$SD_ROOT/skills/deep-analysis/scripts/intent-classify.py" --session <session_id> --pretty
python3 "$SD_ROOT/skills/deep-analysis/scripts/intent-classify.py" --jsonl <path_or_uri> --pretty
python3 "$SD_ROOT/skills/deep-analysis/scripts/intent-classify.py" --text "分析这个项目的性能问题"
```

## DO NOT

- 不自写特定环境 JSONL 解析器（统一走 `echolib`）
- 不修改原始会话或写 `MEMORY.md`
- 不在无数据时伪造结果——无法解析时返回空数组或明确错误
