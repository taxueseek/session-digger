# session-digger

跨环境会话历史挖掘与知识生命周期管理。支持 20+ 编码环境的对话分析、模式提炼、记忆审计。

分析过一次的会话不再重复解析，让每次回顾都比上一次更快。

## 安装

### 作为 Claude Code 插件

```
/plugin install session-digger
```

### 从 GitHub 安装

```bash
git clone https://github.com/taxueseek/session-digger.git ~/.claude/plugins/cache/taxue/session-digger/0.6.0
```

### 作为独立工具

```bash
git clone https://github.com/taxueseek/session-digger.git
cd session-digger

# 查看全局统计
python3 scripts/sd-recall.py stats

# 列出所有会话
python3 scripts/sd-recall.py sessions --scope all --limit 10
```


## v0.6.0 新特性 — 架构重构 + SchemaProbe

### SchemaProbe 自动格式发现

不再逐个适配——采样 30 条记录，自动推断 JSONL 结构：

```
已知格式 → Claude / Grok / Kimi Code / Codex / WorkBuddy → 100% 覆盖
未知格式 → 自动检测 type/role/content/timestamp 字段映射 → 即开即用
```

只需 150 行代码，替代了原来 1000+ 行的逐个硬编码适配器。

### 适配器注册表

```python
from echolib import register_adapter

# 新增一个环境适配器，只需要一行
register_adapter("my_tool", "My Tool", list_sessions=..., ...)
```

不再需要改 3 个文件，不再有 if/else 分发票。

### 统一 CLI：sd-recall.py

10 个子命令，替代原来 11 个 bash 脚本：

| 子命令 | 用途 | 替代 |
|--------|------|------|
| `search` | 关键词搜索会话 | — |
| `sessions` | 列出会话 | `list-sessions.sh` |
| `session-stats` | 单会话统计 | `session-stats.sh` |
| `messages` | 提取对话 | `extract-messages.sh` |
| `tools` | 提取工具调用 | `extract-tools.sh` |
| `files` | 文件变更历史 | `extract-files-changed.sh` |
| `schema` | JSONL 格式检测 | `parse-jsonl.sh` |
| `stats` | 全局统计 | — |
| `save-summary` | 保存分析摘要 | `save-summary.sh` |
| `extract-knowledge` | 知识提取 | `extract-knowledge.sh` |

### 架构精简

| 指标 | v0.5.1 | v0.6.0 | 变化 |
|------|:------:|:------:|:----:|
| 总代码行数 | 8597 | **7011** | **-18%** |
| Python 文件 | 7 | **6** (env-adapters 合并) |
| Bash 脚本 | 17 | **6** | **-65%** |
| env-adapters.py | 1917 行 | **已删除，合并入 echolib** |
| 代码重复 | SessionMeta 等重复 4 处 | **0** |


## v0.5 新特性

### 分析结果存证（Memoization）

分析过一次的会话，结果自动存为摘要。下次 recall 直接读缓存，跳过全量解析 + LLM 分析。重复查询的时间和 token 消耗降低 90%+。

```bash
# 第一次搜索：全量解析
recall-lite.sh "为什么选 SQLite"

# 分析完成后，结果自动缓存（无需手动操作）

# 第二次搜索同一会话：直接读缓存，标记 [CACHED]
recall-lite.sh "数据库选型"
```

### 时效分层记忆

借记忆时效设计，分析结果按重要性分三个等级自动过期：

| 等级 | 含义 | 过期规则 |
|------|------|----------|
| `permanent` | 认知规律、思维模型、方向判断 | 永不因时间过期 |
| `periodic` | 偏好、阶段性结论（默认） | 7 天后不再注入 |
| `once` | 临时上下文、单次任务状态 | 24 小时后失效 |

核心原则：**旧偏好不如没有偏好**。过期的结论不会污染当前判断。

### 否决方向记录

存档理念，保存分析结果时可记录已排除的方向。下次 recall 时直接展示，避免重复走弯路。

```bash
sd-recall.py save-summary ~/.../abc.jsonl --stdin \
  --query "技术选型" --tier permanent \
  --excluded "Rust维护成本高;Go生态不足" <<< "Python 是最优选择"
```

输出效果：

```
=== [CACHED] 分析意图: 技术选型 ===
  分析时间: 2026-06-30T21:00:00
  时效等级: permanent (永久)
  已否决方向:
    - Rust维护成本高
    - Go生态不足
```

### 跨代理搜索（Cross-agent Search，v0.5.4）

`recall-lite.sh --agent cross` 现在真正跨所有环境搜索——Claude Code、Grok Build、Kimi Code 一网打尽。新增 `--agent auto` 模式，当前项目没会话时自动回退到跨环境搜索，不用手动改参数。

### 自动缓存（Auto-caching，v0.5.4）

搜索完会话自动存摘要，下次 recall 直接命中 `[CACHED]`，零额外操作。

### 会话断层检测（CLI History Gap，v0.5.4）

自动检测 `~/.claude/history.jsonl` 有记录但 JSONL 文件已被清理的会话，提示恢复 prompt 文本。

## 独到之处

**零依赖** — 纯 Python 3.6+ stdlib，无 pip install，无编译，clone 即用。

**本地优先** — 所有解析在本地完成，recall-lite.sh 零 API 调用。LLM 只在需要综合分析时介入。

**跨环境** — SchemaProbe 自动适配六种已知环境 + 任意未知 JSONL 格式。

**三重过滤** — 缓存读取时执行新鲜度检查（文件 mtime）→ 时效分层过期 → 意图子串匹配，确保返回的分析结果既新鲜又相关。

**知识提取** — 双通道提取：Pass 1 扫描工具调用中的决策和错误，Pass 2 扫描消息中的纠正模式、认可模式、价值判断和 URL 引用。


## 快速开始

```bash
# 1. 本地搜索历史会话（零 API 调用）
scripts/recall-lite.sh "认证 bug"

# 2. 查看会话统计
python3 scripts/sd-recall.py session-stats ~/.claude/projects/.../session.jsonl

# 3. 提取对话内容
python3 scripts/sd-recall.py messages ~/.claude/projects/.../session.jsonl --role user --limit 10

# 4. 保存分析摘要
echo "认证问题出在 token 刷新逻辑" | \
  python3 scripts/sd-recall.py save-summary ~/.claude/projects/.../session.jsonl \
  --stdin --query "认证bug" --tier periodic

# 5. 下次搜索同一会话——直接读缓存
scripts/recall-lite.sh "认证"
```

## 命令

> 作为插件安装时，命令以 `/session-digger:<command>` 形式出现。

### 对话挖掘

| 命令 | 用途 |
|------|------|
| `/recall <topic> [--scope current\|all\|auto] [--limit N] [--agent claude\|grok\|kimi_code\|cross\|auto] [--lite]` | 搜索历史对话中的主题、决策或错误 |
| `/recap [N-sessions\|duration] [--detail low\|medium\|high]` | 总结近期会话 |
| `/timeline [--limit N] [--since YYYY-MM-DD]` | 按时间排列的项目历史，合并会话和 git 提交 |
| `/lessons [topic] [--scope current\|all] [--category decisions\|mistakes\|patterns\|all]` | 从历史对话中提取经验教训 |

### 记忆管理

| 命令 | 用途 |
|------|------|
| `/dashboard` | 全局记忆概览 |
| `/audit [project] [--deep]` | 审计记忆过期状态 |
| `/extract [session-id] [--scope current\|all] [--agent claude\|cross]` | 从会话中提炼持久知识（带去重） |
| `/save-summary <session-path> <analysis-text> [query-intent] [memory-tier]` | 保存分析结果为摘要 |
| `/prune [project] [--dry-run]` | 交互式清理过期记忆 |

### CLI 工具

```bash
python3 scripts/sd-recall.py search <keyword>              # 搜索会话
python3 scripts/sd-recall.py sessions                       # 列出会话
python3 scripts/sd-recall.py session-stats <path>           # 单会话统计
python3 scripts/sd-recall.py messages <path>                # 提取对话
python3 scripts/sd-recall.py tools <path>                   # 提取工具调用
python3 scripts/sd-recall.py files <path>                   # 文件变更历史
python3 scripts/sd-recall.py schema <path>                  # JSONL 格式检测
python3 scripts/sd-recall.py stats                          # 全局统计
python3 scripts/sd-recall.py save-summary <path> [--stdin]  # 保存摘要
python3 scripts/sd-recall.py extract-knowledge <path>       # 知识提取
```

## 支持的环境

| 环境 | 会话格式 | 适配方式 |
|------|---------|---------|
| Claude Code | JSONL（record type 区分） | 原生解析 |
| Grok Build | chat_history.jsonl + events.jsonl | SchemaProbe + 双文件工具关联 |
| Kimi Code | wire.jsonl（turn.prompt/text/tool.call） | SchemaProbe |
| Codex (OpenAI) | response_item + event_msg | SchemaProbe |
| WorkBuddy | parentId 树形结构 | SchemaProbe（消息）+ providerData 解析 |
| Trae CN (ByteDance) | LLM 摘要层（intent→actions→outcome→learned） | 专用解析 |
| 任意未知环境 | 任意 JSONL | SchemaProbe 自动发现 |

## 架构

```
sd-recall.py (CLI) → echolib.py → SchemaProbe → 7 适配器
                      (核心库)    (自动格式发现)  (注册表分发)
```

| 层级 | 职责 |
|------|------|
| `sd-recall.py` | 10 个子命令，统一 CLI |
| `echolib.py` | 核心库：Record、SessionMeta、SchemaProbe、适配器注册表 |
| `ADAPTER_REGISTRY` | 7 适配器通过注册表分发，新增环境只需 `register_adapter()` |
| `SchemaProbe` | 采样→推断→定向提取，覆盖所有 JSONL 变种 |
| 脚本 | 6 个 bash（analyze / apply / git / hook / recall-lite）|

bash 脚本从 v0.5 的 17 个精简到 6 个。所有数据提取操作统一走 sd-recall.py 或 echolib.py。

## 前置条件

- Python 3.6+（仅 stdlib）
- bash
- git（可选，用于时间线和文件历史）

## License

ISC
