# session-digger

> 跨环境会话历史挖掘。把对话变成可搜索的知识资产。

分析过一次的会话不再重复解析，每次回顾都比上一次更快。

支持 **Claude Code、Codex、ZCode、Grok Build、Kimi Code、Reasonix** 等多环境，以及微信等外部对话导入。零依赖，clone 即用。

---

## 安装

```bash
git clone https://github.com/taxueseek/session-digger.git
cd session-digger
```

**两种使用方式：**

**作为 Claude Code 插件** — 克隆到插件目录后即可使用斜杠命令：
```bash
# 将项目克隆到 Claude Code 插件目录
git clone https://github.com/taxueseek/session-digger.git ~/.claude/plugins/session-digger
```
之后在 Claude Code 中直接用 `/recall` `/analyze` `/trend` 等命令。

**作为独立工具** — 直接运行 Python 脚本，零依赖，无需安装：
```bash
# 搜索历史会话
python3 scripts/sd-recall.py search "认证 bug"

# 查看全局统计
python3 scripts/sd-recall.py stats
```

---

## 特色

**零依赖** — 开箱即用。

**本地优先** — 所有解析在本地完成，recall-lite.sh 零 API 调用。LLM 只在需要综合分析时介入。

**跨环境** — 10 个内置适配器覆盖主流编码环境（Claude Code / Grok Build / Kimi Code / Codex / WorkBuddy / Trae CN / ZCode / DIM / Reasonix），未知 JSONL 格式由 SchemaProbe 通用适配器自动探测适配。

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
- **否决方向记录** — 存档已排除的方向，下次 recall 展示记录供参考，避免重复走弯路

---

## 快速开始

```bash
# 先建索引（一次即可，后续自动增量更新）
python3 scripts/index-builder.py build

# 搜索历史会话
scripts/recall-lite.sh "认证 bug"

# 查看全局统计
python3 scripts/sd-recall.py stats

# 趋势分析
python3 scripts/trend-engine.py period-over-period --period week

# 自动生成记忆文件
python3 scripts/remember.py
```

作为插件安装后，所有功能通过斜杠命令使用：`/recall` `/trend` `/analyze` `/import` 等。

---

## 支持的环境

| 环境 | 格式 | 适配方式 |
|------|------|---------|
| Claude Code | JSONL（record type 区分） | 原生 |
| Grok Build | chat_history.jsonl + events.jsonl | 专用适配器 |
| Kimi Code | wire.jsonl（turn.prompt/text/tool.call） | 专用适配器 |
| Codex (OpenAI) | response_item + event_msg | 专用适配器 |
| WorkBuddy | parentId 树形结构 | 专用适配器 |
| Trae CN (ByteDance) | LLM 摘要层 | 专用适配器 |
| ZCode | SQLite DB + transcript.jsonl | 专用适配器 + SQLite 直连 |
| DIM | memory 目录 | 专用适配器 |
| Reasonix | sessions 目录 | 专用适配器 |
| 任意聊天记录 | JSON/CSV/纯文本 | `dialog-adapter.py` 导入后索引 |

未知 JSONL 格式由 **SchemaProbe** 通用适配器自动探测。SchemaProbe 通过采样分析字段结构自动适配，新环境无需写适配器即可读取。

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

### v0.5 系列 — 分析存证 + 记忆分层 + 跨代理搜索

- 分析结果自动缓存，重复查询 token 消耗降 90%+
- 记忆分永久/周期/一次性三级，过期结论不污染当前判断
- 否决方向记录，存档已排除的方案，避免重复走弯路
- 跨代理搜索，Claude Code / Grok / Kimi 一网打尽
- 自动检测 JSONL 已被清理但 history 仍有记录的「断层会话」

### v0.6.0 — 架构重构 + SchemaProbe

- SchemaProbe 自动识别 JSONL 格式，新环境无需写适配器
- 适配器注册表替代 if/else 硬编码分发
- sd-recall.py 统一 CLI，10 个子命令替代 11 个 bash 脚本
- 总代码精简 18%，脚本数量减少 65%

### v0.8.0 — 四层架构 + 趋势分析

- 四层模型：PARSE（精确解析）→ INDEX（毫秒级缓存）→ TREND（纯算术聚合）→ DECISION（人工审批）
- `/trend` 周/月环比趋势分析，`/optimize` 技能差距分析产出 SKILL.md 提案
- `/import` 外部对话导入，`/profiles` 群聊画像，`/apply` 规则审批
- format-detector 自动签名识别未知 Agent 格式
- 新增 ZCode、DIM（小米）、Reasonix 环境支持

### v0.9.1 — echolib 重构 + zcode-adapter 瘦身

- `echolib.py` 提取 4 项共享 helper（`_iter_jsonl` / `_strip_system_reminder` / `_extract_content_text` / `_match_call_results`），消除 30+ 处重复解析模式
- `zcode-adapter.py` 从 483 行削到 72 行，查询逻辑内迁 echolib，子进程调用消除
- `topic_classify.py` 去 subprocess，直接调用 echolib
- 新增 `test_baseline.py` 覆盖核心调用，`test_perf.py` 性能基准
- 新增 `digest.md`、`topic-scan.md` 命令文档
- 15 个命令文件细节优化

### v0.9.0 — 自动记忆沉淀 + 技能使用洞察

- `remember.py` 从 SQLite 索引自动生成 `memory/*.md` 记忆文件，零成本沉淀统计数据
- `skill-insight` 技能使用洞察，直接定位闲置技能和高频领域工具
- zcode-adapter 瘦身 95%，子进程调用消除，速度和可靠性提升

## License

ISC
