---
name: deep-analysis
description: |
  会话深度分析子技能：错误根因提取 + 用户意图分类。
  从 JSONL 原文中提取具体错误消息并归类根因，
  对用户第一条消息做意图路由。
  触发：deep-analysis、错误根因、错误分类、意图分类、
  error root cause、intent classify、analyze session errors、
  这次为啥失败、根因是什么、用户到底想干嘛
version: 0.1.1
---

# deep-analysis

> 分析层专精：不解析全库、不写记忆。优先消费 digger **index.db** 定位会话。

## 在 digger 架构中的位置

| 层 | 本技能 |
|----|--------|
| L0 PARSE | 不替代 echolib / jsonl-core |
| L1 INDEX | **只读** `jsonl_path` 定位文件 |
| L2–L3 | 错误分类 + 意图标签（给 experience-synthesis / optimize 用） |

## 功能

| 脚本 | 输入 | 输出 |
|------|------|------|
| `scripts/error-root-cause.py` | session_id / jsonl 路径 | 错误根因 JSON 数组 |
| `scripts/intent-classify.py` | session_id / jsonl / 文本 | 意图分类 JSON |

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
SD_ROOT="${SESSION_DIGGER_ROOT:-$HOME/.agents/skills/session-digger}"
# 建议先有索引：python3 "$SD_ROOT/scripts/index-builder.py" build

# 错误根因（--session 优先走 index.db）
python3 "$SD_ROOT/skills/deep-analysis/scripts/error-root-cause.py" --session <session_id>
python3 "$SD_ROOT/skills/deep-analysis/scripts/error-root-cause.py" --jsonl <path>

# 意图分类
python3 "$SD_ROOT/skills/deep-analysis/scripts/intent-classify.py" --session <session_id>
python3 "$SD_ROOT/skills/deep-analysis/scripts/intent-classify.py" --jsonl <path>
python3 "$SD_ROOT/skills/deep-analysis/scripts/intent-classify.py" --text "分析这个项目的性能问题"
```

路径解析见 `skills/common_paths.py`：`SESSION_DIGGER_DATA_DIR` → index.db → 目录扫描降级。

## 错误类型映射

| error_type | 匹配规则 |
|------------|----------|
| COMMAND_NOT_FOUND | command not found / not recognized |
| PERMISSION_DENIED | Permission denied / EACCES |
| TIMEOUT | timeout / deadline exceeded |
| EISDIR | EISDIR / is a directory |
| FILE_NOT_FOUND | File does not exist / ENOENT |
| RATE_LIMIT | rate limit / 429 |
| UNKNOWN | 其他 |

## 调度优先级（优于泛化 /analyze 空转）

当用户明确要「根因 / 意图 / 这次为什么失败」时：**先跑本技能**，再决定是否 `/analyze` 全文或 experience-synthesis。

## 下游协作

| 发现 | 推荐 |
|------|------|
| 错误根因明确，需固化规则 | `/apply`（人审）或 `/optimize` |
| 意图 DEBUGGING 且错误密 | `experience-synthesis` + `/lessons` |
| 意图 REFLECTION 跨会话 | `experience-synthesis` + `/trend` |
| 环境配置类错误扎堆 | `env-doctor` / `native-diag` |
| 找不到 jsonl | 先 `/index`，再查 session id |

## DO NOT

- 不替代 jsonl-core / echolib 的解析入库
- 不修改用户 JSONL / 不写 MEMORY.md
- 不输出表情符号
- 不在无索引且 session 无法解析时假装有结果——返回空数组或明确错误
