# Session Digger 命令参考

> 跨环境会话历史挖掘与知识生命周期管理。支持 Claude Code、Grok Build、Kimi Code 三环境，以及微信等外部对话导入。

---

## 快速开始

```bash
# 第一次使用：建立索引（提速 5-10x）
/index

# 搜索历史对话
/recall "认证逻辑"

# 查看最近做了什么
/recap

# 零 API 调用搜索（纯本地）
scripts/recall-lite.sh "认证 bug"
```

---

## 一、对话挖掘

### `/recall` — 搜索历史对话

搜索三个环境中的历史会话，支持关键词、决策点、错误模式三种焦点。

| 用法 | 说明 |
|------|------|
| `/recall "关键词"` | 搜索当前项目的历史会话 |
| `/recall "关键词" --scope all` | 跨所有项目搜索 |
| `/recall "关键词" --agent cross` | 跨三个环境搜索 |
| `/recall "关键词" --decisions` | 只返回决策点，token 消耗降 60-80% |
| `/recall "关键词" --lite` | 零合成模式，返回原始证据 |

三种自动识别的搜索焦点：
- 提到「为什么选」「决策」「原因」→ 决策考古
- 提到「出错」「失败」「bug」→ 错误狩猎
- 其他 → 通用搜索

### `/recap` — 总结近期会话

```bash
/recap          # 默认最近 5 次会话
/recap 10       # 最近 10 次
/recap 7d       # 最近 7 天
```

### `/timeline` — 项目时间线

合并 Claude Code 会话历史和 git 提交记录，按时间排列项目进展。

```bash
/timeline
/timeline --since 2026-06-01
/timeline --limit 30
```

### `/topics` — 话题切分

将长会话按时间间隔和内容相似度切成话题片段。

```bash
/topics                          # 当前项目所有会话的话题
/topics <session-id>             # 单次会话的话题切分
/topics --min-gap 600            # 自定义话题间隔阈值（秒）
```

输出每个话题的时间范围、消息数、关键词。选话题后可路由到对应的 `taxue-*` 技能做深度分析。

---

## 二、记忆管理

### `/import` — 导入外部对话

导入微信、LINE、Telegram、会议记录等外部对话，统一转成可搜索的 JSONL 格式。

```bash
# 自动检测格式
/import ~/Downloads/wechat-export.json

# 显式指定格式
/import ~/Downloads/chat.csv --format csv
/import ~/Downloads/transcript.txt --format transcript
```

**支持的格式：**

| 格式 | 说明 | 自动检测依据 |
|------|------|-------------|
| `wechat` | 微信 4.x JSON/CSV 导出 | 文件名含 wechat/微信，或 JSON 结构匹配 |
| `json` | 通用 `[{sender, time, content}]` 数组 | `.json` 扩展名或 JSON 内容 |
| `csv` | CSV（sender/time/content 列） | `.csv` 扩展名 |
| `transcript` | 纯文本 `HH:MM Name: message` | 行模式匹配 |

导入后自动存入 `~/.claude/projects/_imported/`，下一步执行索引即可搜索：

```bash
/index
/recall "旅行计划"
```

**限制：**
- 微信需先导出明文（不支持解密数据库）
- 自动过滤图片、语音等非文本消息（微信 type 3/34/42/43/47/48/49）
- 单文件上限约 10 万条消息

### `/extract` — 从会话提炼持久知识

双通道知识提取：
- Pass 1：扫描工具调用中的决策和错误
- Pass 2：扫描消息中的纠正模式、认可模式、价值判断和 URL 引用

```bash
/extract                          # 当前会话
/extract <session-id>             # 指定会话
/extract --scope all --agent cross # 跨环境提炼
```

### `/save-summary` — 保存分析结果

手动保存分析摘要，下次 `/recall` 直接命中缓存，跳过全量解析。

```bash
echo "认证问题出在 token 刷新逻辑" | \
  scripts/sd-recall.py save-summary ~/path/session.jsonl \
  --stdin --query "认证bug" --tier periodic \
  --excluded "重启服务;清缓存"
```

**三级时效：**

| 等级 | 含义 | 过期规则 |
|------|------|---------|
| `permanent` | 认知规律、思维模型 | 永不因时间过期 |
| `periodic` | 偏好、阶段性结论 | 7 天后不再注入 |
| `once` | 临时上下文 | 24 小时后失效 |

**否决方向**：保存时用 `--excluded` 记录已排除的方案，下次 recall 直接展示，避免重复走弯路。

### `/dashboard` — 全局记忆概览

跨所有项目的记忆状态：数量、行数、过期数、token 消耗。发现异常时提示执行 `/audit` 或 `/prune`。

### `/audit` — 记忆过期审计

```bash
/audit                    # 快速审计所有项目
/audit --deep             # 深度内容感知审计
/audit <project-name>     # 审计指定项目
```

### `/prune` — 交互式清理

```bash
/prune                    # 交互式清理过期记忆
/prune --dry-run          # 只看不删
```

逐条确认，y/n 决定记忆去留。

---

## 三、分析与规则

### `/index` — 建立搜索索引

```bash
/index                          # 增量更新当前项目
/index --rebuild                # 全量重建
/index --agent cross            # 索引三个环境
```

首次建索引后，关键词搜索从 O(n) 降到 <50ms（SQLite FTS5）。

### `/analyze` — 会话模式分析

检测重试循环、错误模式、用户纠正，生成候选规则。

```bash
/analyze                          # 分析当前会话
/analyze <session-id>             # 分析指定会话
/analyze --all --limit 10         # 跨会话聚合分析
```

输出顺序：概览 → 重试模式 → 错误分布 → 用户纠正 → 候选规则。完成后提示执行 `/apply`。

### `/apply` — 审阅并持久化规则

交互式审阅 `/analyze` 生成的候选规则，逐条决定：

```
[y] approve   — 写入目标文件
[n] skip      — 跳过
[e] edit      — 编辑后写入
[a] approve all — 全部写入
[q] quit      — 停止审阅
```

目标位置：`--target CLAUDE.md`（默认） / `memory` / `docs`。

### `/lessons` — 经验教训提炼

按类别提取历史经验：决策、错误、模式。

```bash
/lessons
/lessons "数据库选型" --category decisions
/lessons --scope all --category mistakes
```

---

## 四、直接运行脚本（无需作为插件安装）

所有脚本在 `scripts/` 目录下，纯 Python 3.6+ stdlib，零依赖。

```bash
# 搜索会话
python3 scripts/sd-recall.py search "关键词" --scope all --limit 10

# 列出会话
python3 scripts/sd-recall.py sessions

# 单次会话统计
python3 scripts/sd-recall.py session-stats ~/.claude/projects/.../session.jsonl

# 提取对话
python3 scripts/sd-recall.py messages ~/.claude/projects/.../session.jsonl --role user

# 提取工具调用
python3 scripts/sd-recall.py tools ~/.claude/projects/.../session.jsonl --errors-only

# 文件变更历史
python3 scripts/sd-recall.py files ~/.claude/projects/.../session.jsonl

# JSONL 格式检测
python3 scripts/sd-recall.py schema unknown-session.jsonl

# 全局统计
python3 scripts/sd-recall.py stats

# 导入外部对话
python3 scripts/dialog-adapter.py ~/Downloads/wechat.json --format wechat

# 零 API 调用搜索
bash scripts/recall-lite.sh "认证 bug"
```

---

## 五、跨环境支持

| 环境 | 会话位置 | 自动识别 |
|------|---------|---------|
| Claude Code | `~/.claude/projects/<encoded>/<uuid>.jsonl` | ✅ |
| Grok Build | `~/.grok/sessions/.../chat_history.jsonl` | ✅ |
| Kimi Code | `~/.kimi-code/sessions/.../agents/main/wire.jsonl` | ✅ |
| 微信等外部 | 任意 JSONL/CSV/文本 | 导入后自动 |
| 未知格式 | 任意 JSONL | SchemaProbe 自动发现 |

跨环境搜索用 `--agent cross` 参数，或直接用 CLI 的 `sd-recall.py search`（默认跨环境）。

---

## 六、性能速查

| 操作 | 首次 | 后续 |
|------|------|------|
| 索引构建 | 5-30s（一次性） | 增量，仅变化文件 |
| 关键词搜索 | — | <50ms（FTS5 命中） |
| 重复查询 | 全量解析 + LLM | 读缓存 [CACHED]，token 降 90%+ |
| `--decisions` 模式 | — | token 降 60-80% |
| `--lite` 模式 | — | 零 API 调用 |

七、设计原则

- **索引优先**：先 `/index`，后续所有操作 5-10x 加速
- **缓存优先**：分析过的会话自动存摘要，重复查询读缓存
- **本地优先**：recall-lite.sh 零 API 调用，LLM 只在需要综合分析时介入
- **零依赖**：纯 Python 3.6+ stdlib，无 pip install，clone 即用
