# session-digger

> 跨环境会话历史挖掘。把对话变成可搜索的知识资产。

分析过一次的会话不再重复解析，每次回顾都比上一次更快。支持 **Claude Code、Codex、ZCode、Grok Build、Kimi Code、Reasonix** 等多环境，以及微信等外部对话导入。零依赖，clone 即用。

---

## 如何安装

### 通用安装方式（适用于所有平台）

```bash
# 方式一：npx 一键安装（推荐）
npx -y skills add taxueseek/session-digger -g --all

# 方式二：git clone
git clone https://github.com/taxueseek/session-digger.git
cd session-digger
```

零依赖，只有 Python 3.6+，无需 pip install。克隆后即可直接运行 `python3 scripts/` 下的所有工具。npx 方式会自动处理依赖和路径配置。

### Claude Code 插件安装

```bash
git clone https://github.com/taxueseek/session-digger.git ~/.claude/plugins/session-digger
```

装完在 Claude Code 中直接使用斜杠命令：`/recall` `/analyze` `/trend` `/import` 等。

### Herdr 插件安装

```bash
herdr plugin install taxueseek/session-digger
```

安装后自动进入 Herdr 插件市场索引（每 30 分钟刷新）。在 Herdr 终端中使用：

| 能力 | 触发 |
|------|------|
| 搜索历史会话 | 动作 `sd-search`（支持 `SD_SEARCH_KEYWORD=xxx` 直接搜索） |
| 模糊搜索会话 | 动作 `sd-fuzzy-search`（fzf，未安装自动降级） |
| 列出最近会话 | 动作 `sd-sessions` |
| 会话统计面板 | 动作 `sd-stats` / 面板 `sd-stats-pane`（持久化仪表板） |
| 话题趋势分析 | 动作 `sd-trend` |
| Skill 差距检测 | 动作 `sd-skill-gap` |
| Skill 资产自检 | 动作 `sd-skill-health` |
| 重建搜索索引 | 动作 `sd-reindex` |

统计面板展示：总览（会话数 / 消息数 / 工具调用 / 错误率）、按环境分布、最近 5 会话、高频工具 TOP 5。工作树创建时自动触发索引重建，面板数据保持实时。

快捷键（需在 `~/.config/herdr/config.toml` 中启用）：

| 快捷键 | 动作 |
|--------|------|
| `prefix+d` | 打开会话统计面板 |
| `prefix+s` | 模糊搜索会话（需安装 [fzf](https://github.com/junegunn/fzf)） |
| `prefix+t` | 话题趋势分析 |

模糊搜索在 fzf 未安装时自动降级为普通列表模式。

---

## 能做什么

- **跨环境搜索** — 在 Claude Code、Grok、Kimi、ZCode 等多环境中同时搜索历史决策、错误、话题
- **导入外部对话** — 微信 JSON/CSV 导出、会议记录、纯文本聊天记录，统一索引
- **极速检索** — SQLite FTS5 索引 + mtime 增量缓存，关键词搜索 <50ms
- **趋势分析** — 周/月环比趋势，纯算术聚合无 API 开销
- **知识存证** — 分析过的会话自动缓存，重复查询 token 消耗降 90%+
- **记忆时效分层** — 永久/周期/一次性三级过期，旧偏好不污染当前判断
- **模式分析** — 检测重试循环、错误模式，生成候选规则写入 CLAUDE.md
- **群聊画像** — 增量提取参与者画像，append-only 不丢失历史信号
- **技能差距分析** — 跨会话挖掘反复出现的痛点，输出 SKILL.md 改进提案
- **自动记忆沉淀** — `remember.py` 从索引自动生成 `memory/*.md`，统计数据零成本落地
- **使用回顾报告** — `/reflect` 生成本机多环境习惯 HTML（用时 / 环境 / 任务 / Token·模型偏好 / 时段热力）
- **否决方向记录** — 存档已排除的方向，避免重复走弯路
- **Herdr 终端集成** -- 8 个动作 + 1 个统计面板，工作树创建时自动重建索引

---

## 快速开始

```bash
# 1. 建索引（一次即可，后续自动增量更新）
python3 scripts/index-builder.py build

# 2. 搜索历史会话
scripts/recall-lite.sh "认证 bug"

# 3. 查看全局统计
python3 scripts/sd-recall.py stats

# 4. 趋势分析
python3 scripts/trend-engine.py period-over-period --unit week

# 5. 自动生成记忆文件
python3 scripts/remember.py

# 6. 使用回顾 HTML（多环境习惯 / 时段 / Token·模型）
python3 scripts/reflect-report.py --months 1 --open
```

---

## 命令参考

### 搜索与回顾

| 命令 | 用途 |
|------|------|
| `/recall <topic>` | 搜索历史对话中的主题、决策或错误 |
| `/recap [N]` | 总结近期会话 |
| `/reflect [--months 1\|3\|6\|12]` | **使用回顾**：本地多环境习惯报告 HTML |
| `/timeline` | 时间线（会话 + git 提交） |
| `/lessons [topic]` | 从历史对话中提取经验教训 |
| `/topics` | 话题切分与浏览 |

### 趋势与分析

| 命令 | 用途 |
|------|------|
| `/trend` | 周/月环比趋势分析，工具回归检测 |
| `/optimize` | 跨会话技能差距分析 → SKILL.md 改进提案 |
| `/analyze` | 检测重试循环、错误模式、用户修正 |

### 索引与导入

| 命令 | 用途 |
|------|------|
| `/index` | 构建/更新 SQLite FTS5 索引 |
| `/import` | 导入外部对话（微信/JSON/CSV/文本） |
| `/profiles` | 群聊参与者画像提取 |

### 记忆管理

| 命令 | 用途 |
|------|------|
| `/dashboard` | 全局记忆概览 |
| `/audit [--deep]` | 审计记忆过期状态 |
| `/extract` | 从会话中提炼持久知识 |
| `/save-summary` | 保存分析结果为摘要 |
| `/prune [--dry-run]` | 交互式清理过期记忆 |
| `/apply` | 交互式规则审批写入 |

### 独立工具（无需插件）

```bash
python3 scripts/sd-recall.py search <keyword>               # 搜索会话
python3 scripts/sd-recall.py sessions                        # 列出会话
python3 scripts/sd-recall.py stats                           # 全局统计
python3 scripts/trend-engine.py period-over-period --unit week  # 趋势
python3 scripts/skill-gap-finder.py --min-occurrences 3      # 差距分析
python3 scripts/format-detector.py <path>                    # 格式识别
python3 scripts/remember.py                                  # 自动记忆
python3 scripts/reflect-report.py --months 1 --open          # 使用回顾 HTML
```

报告默认写出 `~/.claude/.session-digger/reports/reflect-YYYY-MM-DD.html`（可用 `-o` / `$SESSION_DIGGER_DATA_DIR` 覆盖）。数据来自本机索引，不上网。

---

## 支持的环境

| 环境 | 格式 |
|------|------|
| Claude Code | JSONL |
| Grok Build | chat_history.jsonl + events.jsonl |
| Kimi Code | wire.jsonl |
| Codex (OpenAI) | response_item + event_msg |
| WorkBuddy | parentId 树形结构 |
| Trae CN (ByteDance) | LLM 摘要层 |
| ZCode | SQLite DB + transcript.jsonl |
| DIM | memory 目录 |
| Reasonix | sessions 目录 |
| 任意聊天记录 | JSON/CSV/纯文本（通过 `dialog-adapter.py` 导入） |

未知 JSONL 格式由 SchemaProbe 自动探测适配，新环境无需写适配器。

---

## 与其他技能的协作

session-digger 只负责解析和路由，不做分析本身。通过 `combo_map.json` 定义连招：

```
数据层:  jsonl-core（解析）→ experience-synthesis（提炼）→ git-mining（交叉验证）
路由层:  /analyze → /apply → /audit（发现→写入→验证闭环）
语义层:  topic_classify（主题归类）→ taxue-*（深度分析）
         skill-insight（技能使用洞察）
```

---

## 版本历史

### v0.9.3 — 使用回顾 /reflect + 索引字段补全 + 报告准确性

- **新增 `/reflect`**：本地多环境 AI 使用回顾 HTML（用多少 / 花在哪 / 什么时候 / 该不该收手；附录含 Token·模型与完整度）
- **索引 enrich**：`summary` / `first_prompt` / `model` / `total_tokens` 跨 Claude·Codex·Kimi·ZCode 等补全；schema 增加 `model` 列
- **报告分析层**：`profiles`（字段覆盖率）+ `insights`（Token / 模型偏好 / 按月）+ 混合任务分类 + 可读话题标题
- **准确性修复**：模型名归一（如 `deepseek-flash`→`deepseek-v4-flash`）；话题过滤探测/中断噪声；Token 图表单会话截顶避免极端环境主导
- **前端**：主叙事折叠附录、单 click 路由、pool/cov 缓存；热力/时段/习惯变化
- **工程**：echolib 适配器拆分（workbuddy/zcode 子模块）；CI smoke + reflect/builder 单测扩展

```bash
python3 scripts/index-builder.py build --rebuild   # 刷新 model/tokens/summary
python3 scripts/reflect-report.py --months 1 --open
```

### v0.9.2 — 可移植路径 + 证据脱敏 + skill 自检

- 清除 commands 中个人路径硬编码，统一 `SESSION_DIGGER_ROOT` / 插件根 / 常见 skill 安装位探测
- `skill-gap-finder` 默认脱敏（无用户名、默认不输出绝对路径）；基线工具不再误报 retry；WebFetch 归一
- 新增 `scripts/skill-health.py`：路由覆盖 / 硬编码 / 安装漂移 / Herdr 接线自检
- 修复 Herdr `sd-skill-gap` 缺少 `analyze` 子命令；修复 stats pane `for/else` 误报空工具
- README 趋势参数 `--period` 更正为 `--unit`

### v0.2.1-herdr — 可移植 / 隐私 / 跨环境 / skill 自检

- fuzzy 消息预览改走 sessions PATH（跨 Grok/Codex/ZCode 等），不再只扫 Claude projects
- event 日志脱敏（仅 basename 段）；真预热 sessions+stats；多环境探测
- stats pane 索引路径脱敏；新增 `sd-skill-health` 动作
- 脚本统一 `HERDR_PLUGIN_ROOT` / `SESSION_DIGGER_ROOT`；state 优先 `HERDR_PLUGIN_STATE_DIR`

### v0.2.0-herdr — 模糊搜索 + 快捷键 + 工作树预热

- 新增 `sd-fuzzy-search` 动作：基于 fzf 交互式筛选会话（fzf 未安装自动降级）
- 新增 3 个快捷键：`prefix+d` 统计 / `prefix+s` 搜索 / `prefix+t` 趋势
- `worktree.created` 事件增强：除索引重建外，自动探测该目录及 grok/kimi 环境的活跃会话并预热

### v0.1.0-herdr — Herdr 插件集成

- 新增 `herdr-plugin.toml` 清单，加入 Herdr 插件市场索引
- 7 个动作：搜索、列会话、统计、趋势、Skill 差距、重建索引
- 1 个持久化统计面板（总览 + 环境分布 + 最近会话 + 高频工具）
- 2 个工作树生命周期钩子：创建时自动重建索引、移除时记录日志
- 零外部依赖，纯 stdlib Python；与 Claude Code Skill 体系完全共存

### v0.9.1 — echolib 重构 + zcode-adapter 瘦身

- `echolib.py` 提取 4 项共享 helper，消除 30+ 处重复解析模式
- `zcode-adapter.py` 从 483 行削到 72 行，查询逻辑内迁 echolib
- `topic_classify.py` 去 subprocess，直接调用 echolib
- 新增 `test_baseline.py`、`test_perf.py`
- 新增 `digest.md`、`topic-scan.md` 命令文档

### v0.9.0 — 自动记忆沉淀 + 技能使用洞察

- `remember.py` 从索引自动生成 `memory/*.md` 记忆文件
- `skill-insight` 技能使用洞察，定位闲置技能和高频工具
- zcode-adapter 瘦身 95%，子进程消除

### v0.8.0 — 四层架构 + 趋势分析

- 四层模型：PARSE → INDEX → TREND → DECISION
- `/trend` 趋势分析，`/optimize` 技能差距分析
- `/import` 外部对话导入，`/profiles` 群聊画像
- format-detector 自动识别未知 Agent 格式
- 新增 ZCode、DIM、Reasonix 支持

### v0.6.0 — 架构重构 + SchemaProbe

- SchemaProbe 自动识别 JSONL 格式，新环境无需写适配器
- sd-recall.py 统一 CLI，10 子命令替代 11 个 bash 脚本
- 总代码精简 18%，脚本减少 65%

### v0.5 系列 — 分析存证 + 记忆分层 + 跨代理搜索

- 分析结果自动缓存，重复查询 token 降 90%+
- 记忆分永久/周期/一次性三级过期
- 否决方向记录
- 跨代理搜索，Claude Code / Grok / Kimi 一网打尽

## License

ISC
