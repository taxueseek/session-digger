---
name: deep-analysis
description: |
  会话深度分析子技能：错误根因提取 + 用户意图分类。
  基于 echolib 统一解析与 index.db 快速定位，覆盖 Antigravity、DSH、DimCode、
  ZCode、Claude Code 等全部 20+ 环境。
  触发：deep-analysis、错误根因、错误分类、意图分类、
  error root cause、intent classify、analyze session errors、
  这次为啥失败、根因是什么、用户到底想干嘛
version: 0.3.0
---

# deep-analysis

> L2 分析专精：全环境复用 `echolib` + `skills/common_paths.py`，单遍联合诊断不重读会话。

## 在 digger 架构中的位置

| 层 | 本技能 |
|----|--------|
| L0 PARSE | 委托 `echolib.dispatch_parse_session` / `dispatch_extract_*`（支持 JSONL / `.zstd` / `zcode://` / `dimcode://`） |
| L1 INDEX | **只读** `index.db` 的 `jsonl_path` / `first_prompt` 快速通道 |
| L2–L3 | 单遍输出错误根因 + 用户意图（供 `experience-synthesis` / `/optimize` 消费） |

## 脚本

| 脚本 | 输入 | 输出 |
|------|------|------|
| `scripts/intent-classify.py` | `--session` / `--jsonl` / `--text`（可加 `--with-errors` 单遍联合分析） | `{intent, confidence, keywords_matched[], all_scores{}}`（加 `--with-errors` 另附 `errors[]`、`error_summary`） |
| `scripts/error-root-cause.py` | `--session <id>` 或 `--jsonl <path\|uri>` | `{session_path, agent, total_errors, errors[], summary{}}` |

## 意图类型（含英文词边界 + 尾句动作词加权 + 前 3 条消息回退）

| 类型 | 关键词示例 |
|------|-----------|
| ANALYSIS | 评估、分析、看看、诊断、audit、inspect |
| COMPARISON | 对比、区别、优劣、优缺点、vs |
| OPTIMIZATION | 优化、改进、提升、提高、加速、调优 |
| DEVELOPMENT | 开发、创建、增加、实现、构建、add |
| DEBUGGING | 为什么、报错、失败、修复、排查、bug、fix |
| REFLECTION | 回顾、总结、复盘、趋势、统计 |

## 错误类型映射（16 类）

| error_type | 匹配规则 |
|------------|----------|
| SQLITE_LOCK_OR_CORRUPT | database is locked / SQLITE_BUSY / SQLITE_CORRUPT |
| GIT_ERROR | fatal: ... / .git/index.lock / CONFLICT / merge conflict |
| PARSE_OR_FORMAT_ERROR | JSONDecodeError / YAMLError / UnicodeDecodeError |
| POLICY_OR_TOOL_BLOCKED | pre-tool hook / privacy hook / policy blocked |
| COMMAND_NOT_FOUND | command not found / not recognized |
| PERMISSION_DENIED | Permission denied / EACCES / Operation not permitted |
| TIMEOUT | timeout / deadline exceeded / ETIMEDOUT |
| CONTEXT_LIMIT | context length / token limit / prompt too long |
| RATE_LIMIT | rate limit / 429 / quota exceeded |
| EISDIR | EISDIR / is a directory |
| FILE_NOT_FOUND | File does not exist / ENOENT / String to replace not found |
| USER_CANCELLED | User cancelled / Request interrupted |
| TEST_FAILURE | AssertionError / pytest failed |
| SYNTAX_OR_TYPE_ERROR | SyntaxError / TypeError / Traceback |
| NETWORK_ERROR | ECONNREFUSED / SSL / HTTP 4xx/5xx |
| TOOL_ARG_ERROR | invalid argument / schema validation / Target must exactly match |

## 调用方式

```bash
SD_ROOT="$(scripts/resolve_root.sh 2>/dev/null || pwd)"

# 推荐：单遍同时完成意图分类 + 错误根因提取
python3 "$SD_ROOT/skills/deep-analysis/scripts/intent-classify.py" --session <session_id> --with-errors --pretty

# 单独提取错误根因（支持 zcode:// / dimcode:// URI）
python3 "$SD_ROOT/skills/deep-analysis/scripts/error-root-cause.py" --jsonl <path_or_uri> --pretty
```

## DO NOT

- 不自写特定环境解析器（统一走 `echolib`）或私自修改原始会话 / `MEMORY.md`
- 不在无数据时伪造结果——无法解析时返回空数组或明确错误
