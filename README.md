# session-digger

> 跨环境会话历史挖掘。把散落在各处的对话变成可搜索的知识资产。

分析过一次的会话不再重复解析，让每次回顾都比上一次更快。

支持 **Claude Code / Grok Build / Kimi Code / ZCode/ Reasonix** 等多环境会话，以及等外部对话导入。零依赖，开箱即用。

---

## 安装

**作为插件：**
```
/plugin install session-digger
```

**作为独立工具：**
```bash
git clone https://github.com/taxueseek/session-digger.git
cd session-digger

---

## 特色

**零依赖** — 开箱即用。

**本地优先** — 所有解析在本地完成，recall-lite.sh 零 API 调用。LLM 只在需要综合分析时介入。

**跨环境** — SchemaProbe 自动适配已知环境 + 任意未知 JSONL 格式。

**三重过滤** — 缓存读取时执行新鲜度检查（文件 mtime）→ 时效分层过期 → 意图子串匹配，确保返回的分析结果既新鲜又相关。

**知识提取** — 双通道提取：Pass 1 扫描工具调用中的决策和错误，Pass 2 扫描消息中的纠正模式、认可模式、价值判断和 URL 引用。

**四层架构** — PARSE(精确) → INDEX(缓存) → TREND(纯聚合) → DECISION(需人工确认)，每层有独立的成本和可信度，不跨层调用。

---

## 能做什么

- **跨环境搜索** — 在 Claude Code、Grok、Kimi、ZCode 等多环境中同时搜索历史决策、错误、话题
- **导入外部对话** —  JSON/CSV 导出、会议记录、纯文本聊天记录，统一索引
- **极速检索** — SQLite FTS5 索引 + mtime 增量缓存，关键词搜索 <50ms
- **趋势分析** — 基于 SQLite 索引的周/月环比趋势，纯算术聚合无 API 开销
- **知识存证** — 分析过的会话自动缓存，重复查询 token 消耗降 90%+
- **记忆时效分层** — 永久/周期/一次性三级过期，旧偏好不会污染当前判断
- **模式分析** — 检测重试循环、错误模式，生成候选规则写入 CLAUDE.md
- **群聊画像** — 增量提取参与者画像（金句、活跃时段、兴趣领域），append-only 更新不丢失历史信号
- **技能差距分析** — 跨会话挖掘反复出现的痛点，自动匹配已安装技能，输出 SKILL.md 改进提案
- **自动记忆沉淀** — `remember.py` 从 SQLite 索引自动生成 `memory/*.md`，索引数据零成本落地为持久记忆
- **否决方向记录** — 存档已排除的方向，避免重复走弯路

---

## 快速开始

```bash
git clone https://github.com/taxueseek/session-digger.git
cd session-digger

# 查看所有会话
python3 scripts/sd-recall.py sessions --scope all --limit 20

# 搜索（零 API 调用）
scripts/recall-lite.sh "认证 bug"

# 查看全局统计
python3 scripts/sd-recall.py stats

# 导入微信对话
python3 scripts/dialog-adapter.py ~/Downloads/wechat-export.json --format wechat

# 趋势分析
python3 scripts/trend-engine.py period-over-period --period week

# 技能差距分析
python3 scripts/skill-gap-finder.py --min-occurrences 3

# 自动生成记忆文件
python3 scripts/remember.py
python3 scripts/remember.py --dry-run  # 预览
python3 scripts/remember.py --only skill  # 单类
```

安装为 Claude Code 插件后，直接使用斜杠命令：`/recall` `/recap` `/import` `/topics` `/analyze` `/dashboard` `/trend` `/optimize` `/profiles` 等。

---

## 支持的环境

| 环境 | 格式 | 适配方式 |
|------|------|---------|
| Claude Code | JSONL（record type 区分） | 原生 |
| Grok Build | chat_history.jsonl + events.jsonl | SchemaProbe + 双文件关联 |
| Kimi Code | wire.jsonl（turn.prompt/text/tool.call） | SchemaProbe |
| Codex (OpenAI) | response_item + event_msg | SchemaProbe |
| WorkBuddy | parentId 树形结构 | SchemaProbe + providerData |
| Trae CN (ByteDance) | LLM 摘要层 | 专用适配器 |
| ZCode | SQLite DB | echolib zcode_db_* 直连 |
| DIM | memory 目录 | SchemaProbe |
| Reasonix | sessions 目录 | SchemaProbe |
| 任意聊天记录 | JSON/CSV/纯文本 | `dialog-adapter.py` 导入后索引 |

SchemaProbe 自动适配未知 JSONL 格式——新环境无需写适配器。

---

## 与其他技能的协作

session-digger 只负责解析和路由，不做分析本身。通过 `combo_map.json` 定义连招映射，与周边技能形成三层协作：

**数据层** — `jsonl-core` 提供规范的 JSONL 解析基础设施，`experience-synthesis` 在其上做经验提炼，`git-mining` 将会话与 git 提交交叉关联。三者共享同一套 schema 和 FTS 索引。

**路由层** — 每个命令执行完毕后自动提示下一步。`/analyze` → `/apply` → `/audit` 形成闭环；`/trend` → `/optimize` 从趋势发现问题到产出改进提案。

**语义层** — `topic_classify.py` 将会话自动归类为投资分析、内容创作、技能开发等 9 大主题，每个主题路由到对应 `taxue-*` 技能做深度分析。`skill-insight` 提供技能使用洞察——哪些技能高频使用、哪些闲置未用。

---

## 命令参考

### 对话挖掘

| 命令 | 用途 |
|------|------|
| `/recall <topic>` | 搜索历史对话中的主题、决策或错误 |
| `/recap [N]` | 总结近期会话 |
| `/timeline` | 时间线（会话 + git 提交） |
| `/lessons [topic]` | 从历史对话中提取经验教训 |

### 趋势与分析

| 命令 | 用途 |
|------|------|
| `/trend` | 周/月环比趋势分析，工具回归检测 |
| `/optimize` | 跨会话技能差距分析 → SKILL.md 改进提案 |
| `/analyze` | 检测重试循环、错误模式、用户修正 |
| `/topics` | 话题切分与浏览 |

### 记忆管理

| 命令 | 用途 |
|------|------|
| `/dashboard` | 全局记忆概览 |
| `/audit [--deep]` | 审计记忆过期状态 |
| `/extract` | 从会话中提炼持久知识 |
| `/save-summary` | 保存分析结果为摘要 |
| `/prune [--dry-run]` | 交互式清理过期记忆 |
| `/apply` | 交互式规则审批写入 |

### 索引与导入

| 命令 | 用途 |
|------|------|
| `/index` | 构建/更新 SQLite FTS5 索引 |
| `/import` | 导入外部对话（微信/JSON/CSV/文本） |
| `/profiles` | 群聊参与者画像提取 |

### 独立工具

```bash
python3 scripts/sd-recall.py search <keyword>               # 搜索会话
python3 scripts/sd-recall.py sessions                        # 列出会话
python3 scripts/sd-recall.py stats                           # 全局统计
python3 scripts/sd-recall.py session-stats <path>            # 单会话统计
python3 scripts/sd-recall.py messages <path>                 # 提取对话
python3 scripts/sd-recall.py tools <path>                    # 提取工具调用
python3 scripts/sd-recall.py save-summary <path> [--stdin]   # 保存摘要
python3 scripts/trend-engine.py period-over-period --period week  # 趋势
python3 scripts/skill-gap-finder.py --min-occurrences 3      # 差距分析
python3 scripts/format-detector.py <path>                    # 格式识别
python3 scripts/remember.py                                  # 自动记忆
```

---

## 版本历史

### v0.9.0 — 自动记忆沉淀 + 技能使用洞察 + 子进程消除

**自动记忆沉淀**

新增 `scripts/remember.py`，从 SQLite 索引自动写入 `memory/*.md` 文件：

- `memory/auto-stats.md` — 全局统计（会话数、消息数、工具调用、错误率）
- `memory/auto-env-habits.md` — 各环境使用习惯（会话数、错误率、平均消息量）
- `memory/auto-skill-usage.md` — 高频工具 Top10 + 闲置技能清单
- `memory/auto-errors.md` — 跨环境错误模式

支持 `--dry-run` 预览和 `--only` 单类写入。

**技能使用洞察（skill-insight）**

新增 `skill-insight` 技能，复用已有 SQLite 索引做技能使用分析——直接定位闲置技能、高频领域工具、环境习惯。

**内部重构**

优化体验，降低代码错误。

### v0.8.0 — 四层架构 + 趋势分析 + 技能差距分析 + 格式检测

**架构升级（四层模型）**

```
Layer 0: PARSE     echolib.py           原始会话 → 统计（精确，ground truth）
Layer 1: INDEX     index-builder.py     统计 → SQLite 缓存（可重建，毫秒级查询）
Layer 2: TREND     trend-engine.py      索引 → 聚合（纯算术，可重复运行）
Layer 3: DECISION  skill-gap-finder.py  模式 → 提案（需人工审批）
```

**新命令**

- `/trend` — 基于 SQLite 索引的周/月环比趋势分析，三种模式：period-over-period、by-theme、regressions
- `/optimize` — 跨会话技能差距分析，匹配已安装技能，输出 SKILL.md 改进提案
- `/profiles` — 群聊参与者增量画像提取，append-only 不丢失历史信号
- `/apply` — 交互式规则审批写入（y/n/e/a/q）
- `/import` — 外部对话导入（微信/JSON/CSV/文本），自动适配

**新工具**

- `trend-engine.py` — 纵向趋势聚合，只读 SQLite 索引
- `skill-gap-finder.py` — 从索引挖掘反复出现的痛点，输出提案
- `format-detector.py` — 签名匹配格式检测，覆盖 Claude Code / Grok / Kimi Code / Cline / Aider / 通用 markdown
- `chat-profiles.py` — 群聊参与者增量画像
- `topic_classify.py` — 9 大主题自动归类，topic-scan 从 964 行 heredoc 重构为 290 行 Python 调用

**环境扩展**

新增 ZCode、DIM（小米）、Reasonix 支持。

---

### v0.6.0 — 架构重构 + SchemaProbe + 适配器注册表

**SchemaProbe 自动格式发现**

采样 30 条记录，自动推断 JSONL 结构。已知格式（Claude/Grok/Kimi/Codex/WorkBuddy）100% 覆盖，未知格式自动检测字段映射，即开即用。



**统一 CLI：sd-recall.py**

10 个子命令，替代原来 11 个 bash 脚本：

| 子命令 | 用途 | 替代 |
|--------|------|------|
| `search` | 关键词搜索 | — |
| `sessions` | 列出会话 | `list-sessions.sh` |
| `session-stats` | 单会话统计 | `session-stats.sh` |
| `messages` | 提取对话 | `extract-messages.sh` |
| `tools` | 提取工具调用 | `extract-tools.sh` |
| `files` | 文件变更历史 | `extract-files-changed.sh` |
| `schema` | JSONL 格式检测 | `parse-jsonl.sh` |
| `stats` | 全局统计 | — |
| `save-summary` | 保存分析摘要 | `save-summary.sh` |
| `extract-knowledge` | 知识提取 | `extract-knowledge.sh` |

**架构精简**

| 指标 | v0.5.1 | v0.6.0 |
|------|:------:|:------:|
| 总代码行数 | 8597 | **7011** (-18%) |
| Bash 脚本 | 17 | **6** (-65%) |
| env-adapters.py (1917 行) | 存在 | **已合并入 echolib** |

### v0.5 系列 — 分析存证 + 记忆分层 + 跨代理搜索

**分析结果存证（v0.5.1）**

分析过的会话自动缓存摘要。下次 recall 直接读缓存，跳过全量解析 + LLM 分析，重复查询 token 消耗降 90%+。

**时效分层记忆（v0.5.1）**

| 等级 | 含义 | 过期规则 |
|------|------|----------|
| `permanent` | 认知规律、思维模型 | 永不因时间过期 |
| `periodic` | 偏好、阶段性结论 | 7 天后不再注入 |
| `once` | 临时上下文 | 24 小时后失效 |

**否决方向记录（v0.5.1）**

```bash
sd-recall.py save-summary <path> --stdin --query "技术选型" --tier permanent \
  --excluded "Rust维护成本高;Go生态不足" <<< "Python 是最优选择"
```

下次 recall 直接展示「已否决方向」，避免重复走弯路。

**跨代理搜索（v0.5.4）**

`recall-lite.sh --agent cross` 跨所有环境搜索。`--agent auto` 当前项目无会话时自动回退到跨环境搜索。

**自动缓存（v0.5.4）**

搜索完会话自动存摘要，下次命中 `[CACHED]`，零额外操作。

**会话断层检测（v0.5.4）**

检测 `history.jsonl` 有记录但 JSONL 已被清理的会话，提示恢复 prompt 文本。


```

## License

ISC
