# session-digger

跨环境会话历史挖掘与知识生命周期管理。支持 20+ 编码环境的对话分析、模式提炼、记忆审计。

分析过一次的会话不再重复解析，让每次回顾都比上一次更快。

## 安装

### 一键安装（推荐）

```bash
npx -y skills add taxueseek/session-digger -g
```

### 手动安装

```bash
cd ~/.claude/skills
git clone https://github.com/taxueseek/session-digger.git temp
cp -r temp/session-digger . && rm -rf temp
```

### 作为 Claude Code 插件

```
/plugin install session-digger
```

### 作为独立工具

```bash
git clone https://github.com/taxueseek/session-digger.git ~/session-digger

# 扫描所有环境
python3 ~/session-digger/scripts/env-adapters.py scan

# 查看会话统计
python3 ~/session-digger/scripts/env-adapters.py stats /path/to/session.jsonl

# 提取消息
python3 ~/session-digger/scripts/env-adapters.py messages /path/to/session.jsonl
```

> 安装完成后即可使用 `/recall`、`/recap`、`/timeline`、`/lessons` 等命令。完整命令列表见下方。

## v0.5 新特性

### 分析结果存证（Memoization）

分析过一次的会话，结果自动存为摘要。下次 recall 直接读缓存，跳过全量解析 + LLM 分析。重复查询的时间和 token 消耗降低 90%+。

```bash
# 第一次搜索：全量解析
recall-lite.sh "为什么选 SQLite"

# 分析完成后，保存结果
save-summary.sh ~/.claude/projects/.../abc.jsonl "选 SQLite 因为零依赖、嵌入式" "技术选型"

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
save-summary.sh ~/.../abc.jsonl "Python 是最优选择" "技术选型" \
  claude permanent "Rust维护成本高;Go生态不足"
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

## v0.5.4 新特性

### 跨代理搜索（Cross-agent Search）

`recall-lite.sh --agent cross` 现在真正跨所有环境搜索——Claude Code、Grok Build、Kimi Code 一网打尽。新增 `--agent auto` 模式，当前项目没会话时自动回退到跨环境搜索，不用手动改参数。

### 自动缓存（Auto-caching）

以前搜完会话要手动 `save-summary.sh` 才存缓存。现在 **解析完自动存摘要**，下次 recall 直接命中 `[CACHED]`，零额外操作。

### 提取去重（Extract Dedup）

`/extract` 命令新增去重检查：
- 同名记忆已存在 → 标记 `[UPDATE]`，不是新建
- 内容 >60% 相似 → 标记 `[DUPLICATE]`，推荐合并
- 支持 Merge 操作，追加 source 引用即可

### 会话断层检测（CLI History Gap）

自动检测 `~/.claude/history.jsonl` 有记录但 JSONL 文件已被清理的会话，提示你用 `parse-jsonl.sh` 恢复 prompt 文本——再也不会"我记得讨论过，但查不到"。

### 记忆新增 insight 类型

跨会话提炼的模式（如"X 工具在 Y 场景效率最高"）不再塞进不合适的类型。新增 `insight` 类型，半衰期 180 天，不与旧记忆冲突。

### 其它改进

- **时效分层更精准**：缓存过期判定从文件 mtime 改为分析时间，避免 touch 会话文件导致缓存误过期
- **插件兼容性增强**：所有脚本改用 `SD_ROOT` 路径发现，独立安装和 Claude Code 插件都兼容
- **新增 `/save-summary` 命令**：对话中直接调用保存分析结果

## 独到之处

**零依赖** — 纯 Python 3.6+ stdlib，无 pip install，无编译，clone 即用。

**本地优先** — 所有解析在本地完成，recall-lite.sh 零 API 调用。LLM 只在需要综合分析时介入。

**跨环境** — 一套工具适配 Claude Code、Grok Build、Kimi Code、Codex、WorkBuddy、Trae CN 六种环境，未知环境自动探测 JSONL 结构兜底。

**三重过滤** — 缓存读取时执行新鲜度检查（文件 mtime）→ 时效分层过期 → 意图子串匹配，确保返回的分析结果既新鲜又相关。

**知识提取** — 双通道提取：Pass 1 扫描工具调用中的决策和错误，Pass 2 扫描消息中的纠正模式、认可模式、价值判断和 URL 引用。

## 快速开始

```bash
# 1. 搜索历史会话（本地，零 API）
scripts/recall-lite.sh "认证 bug"

# 2. 分析完成后保存结果
scripts/save-summary.sh ~/.claude/projects/.../session.jsonl \
  "认证问题出在 token 刷新逻辑" "认证bug" claude periodic "OAuth2流程没问题"

# 3. 下次搜索同一会话——直接读缓存
scripts/recall-lite.sh "认证"

# 4. 查看归档索引
scripts/summary-index.sh --list
scripts/summary-index.sh --stats
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
| `/save-summary <session-path> <analysis-text> [query-intent] [memory-tier]` | 保存分析结果为摘要（v0.5.4 新增正式命令） |
| `/prune [project] [--dry-run]` | 交互式清理过期记忆 |

### 分析结果存证（v0.5 新增）

| 脚本 | 用途 |
|------|------|
| `save-summary.sh <session> <analysis> [intent] [agent] [tier] [excluded]` | 保存分析结果为摘要 |
| `recall-lite.sh <keyword> [--no-summary]` | 搜索会话（摘要优先） |
| `summary-index.sh [--list\|--stats\|--rebuild\|--export\|--prune]` | 归档索引管理 |

`save-summary.sh` 参数说明：

```
session_path    原始会话文件路径
analysis_text   分析结果文本（用 - 从 stdin 读取）
query_intent    本次查询意图（如 "投资"、"skill优化"）
agent_type      来源环境（claude/grok/kimi_code/codex，默认自动检测）
memory_tier     时效等级（permanent/periodic/once，默认 periodic）
excluded        已否决方向，分号分隔（如 "Rust不适合;Go生态不足"）
```

## 支持的环境

| 环境 | 会话格式 |
|------|---------|
| Claude Code | JSONL（record type 区分） |
| Grok Build | chat_history.jsonl + events.jsonl |
| Kimi Code | wire.jsonl（turn.prompt/text/tool.call） |
| Codex (OpenAI) | response_item + event_msg |
| WorkBuddy | parentId 树形结构 |
| Trae CN (ByteDance) | LLM 摘要层（intent→actions→outcome→learned） |
| 任意未知环境 | 自动探测 JSONL 结构 |

## 架构

```
命令 → 代理 → 技能（领域知识）
              → 脚本（数据提取 + 结果存证）
```

| 层级 | 职责 |
|------|------|
| 命令 | 用户入口点（recall / recap / timeline / lessons / dashboard / audit / extract / prune / save-summary） |
| 代理 | 执行分析（recall / analyze / file-historian / schema-scout / memory-auditor） |
| 技能 | 领域知识（jsonl-core / git-mining / experience-synthesis / memory-management） |
| 脚本 | Python + bash 解析，结果存证，归档索引。零 pip 依赖 |

## 前置条件

- Python 3.6+（仅 stdlib）
- bash
- git（可选，用于时间线和文件历史）

## License

ISC
