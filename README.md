# session-digger

<p align="center">
  <img src="assets/readme/hero.svg" alt="session-digger — 跨环境会话历史挖掘" width="100%" />
</p>

<p align="center">
  <a href="https://github.com/taxueseek/session-digger/releases"><img src="https://img.shields.io/badge/version-0.9.6-b34f32?style=flat-square" alt="version 0.9.6" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-ISC-6f675e?style=flat-square" alt="ISC license" /></a>
  <a href="#支持的环境"><img src="https://img.shields.io/badge/envs-9%2B-1d4ed8?style=flat-square" alt="9+ environments" /></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/python-3.6%2B%20stdlib-4a433c?style=flat-square" alt="Python 3.6+ stdlib" /></a>
</p>

跨环境会话历史挖掘。把 Claude Code、Grok、Kimi、Codex 等对话变成**可搜索的知识资产**，并生成本机 **AI 使用回顾 HTML**。

分析过一次的会话不再重复解析；索引增量更新，检索走 SQLite FTS5。**零 pip 依赖**，clone 即用。

---

<p>
  <img src="assets/readme/section-install.svg" alt="如何安装" width="100%" />
</p>

### 通用安装（推荐）

```bash
# 方式一：npx 一键安装
npx -y skills add taxueseek/session-digger -g --all

# 方式二：git clone
git clone https://github.com/taxueseek/session-digger.git
cd session-digger
```

仅需 Python 3.6+ 标准库。克隆后可直接运行 `python3 scripts/` 下工具。

### Claude Code 插件

```bash
git clone https://github.com/taxueseek/session-digger.git ~/.claude/plugins/session-digger
```

装完可用：`/recall` `/analyze` `/trend` `/reflect` `/import` 等。

### Herdr 插件

```bash
herdr plugin install taxueseek/session-digger
```

| 能力 | 触发 |
|------|------|
| 搜索历史会话 | `sd-search` |
| 模糊搜索 | `sd-fuzzy-search`（fzf，可降级） |
| 最近会话 | `sd-sessions` |
| 统计面板 | `sd-stats` / `sd-stats-pane` |
| 话题趋势 | `sd-trend` |
| Skill 差距 / 自检 | `sd-skill-gap` / `sd-skill-health` |
| 重建索引 | `sd-reindex` |

---

<p>
  <img src="assets/readme/section-use.svg" alt="快速开始" width="100%" />
</p>

```bash
# 1. 建索引（一次即可，后续增量）
python3 scripts/index-builder.py build

# 2. 搜索
scripts/recall-lite.sh "认证 bug"

# 3. 全局统计
python3 scripts/sd-recall.py stats

# 4. 本机使用回顾 HTML（浏览器打开）
python3 scripts/reflect-report.py --months 3 --open

# 5. 趋势
python3 scripts/trend-engine.py period-over-period --unit week
```

**第一次成功：** 索引建好 → `recall-lite` 搜到历史句 → `reflect-report` 看到本机用时与各家分布。

---

<p>
  <img src="assets/readme/section-reflect.svg" alt="本机使用回顾" width="100%" />
</p>

`/reflect` 生成**自包含 HTML**（不上网），适合复盘「用了多久、花在哪、什么时候忙」。

**v0.9.6 亮点：**

| 能力 | 说明 |
|------|------|
| 首页用量总览 | 用时概况 + Token 消耗 + 模型偏好（双栏） |
| 单环境隔离 | 进入 Kimi 等子页时，洞察按当前 list 现算，不混 Claude 等全库汇总 |
| 主题 | 跟随系统 / 奶油暖色 / 深褐 / 纯黑 / 冷蓝 / 墨纸 |
| 环境标记 | 侧栏色条标记当前家；主题名与强调色不被劫持 |
| 读数诚实 | 有记录才画；错误率非绝对正确率；中文标签「要盯 / 留意 / 提一嘴」 |

```bash
python3 scripts/reflect-report.py --months 3 --open
# 输出默认：~/.claude/.session-digger/reports/reflect-YYYY-MM-DD.html
```

---

## 能做什么

- **跨环境搜索** — Claude / Grok / Kimi / ZCode / Codex 等一网打尽
- **导入外部对话** — 微信 JSON/CSV、会议记录、纯文本
- **极速检索** — FTS5 + mtime 增量缓存
- **趋势分析** — 周/月环比，纯算术聚合
- **使用回顾** — 本机 HTML：热力日历、任务结构、Token / 模型
- **记忆时效** — 永久 / 周期 / 一次性分层
- **技能差距** — 跨会话痛点 → SKILL.md 提案（需人工确认）
- **自动记忆** — `remember.py` 从索引生成 `memory/*.md`
- **Herdr 集成** — 动作 + 统计面板 + 工作树预热

---

## 命令参考

### 搜索与回顾

| 命令 | 用途 |
|------|------|
| `/recall <topic>` | 搜索历史决策、错误、话题 |
| `/recap [N]` | 总结近期会话 |
| `/reflect` | 本机多环境使用回顾 HTML |
| `/timeline` | 会话 + git 时间线 |
| `/lessons [topic]` | 提取经验教训 |
| `/topics` | 话题切分 |

### 趋势与分析

| 命令 | 用途 |
|------|------|
| `/trend` | 周/月环比、工具回归 |
| `/optimize` | 技能差距 → 提案 |
| `/analyze` | 重试循环、错误模式 |

### 索引 · 导入 · 记忆

| 命令 | 用途 |
|------|------|
| `/index` | 构建/更新 FTS 索引 |
| `/import` | 导入外部对话 |
| `/dashboard` `/audit` `/extract` `/prune` `/apply` | 记忆生命周期 |

### CLI（无需插件）

```bash
python3 scripts/sd-recall.py search <keyword>
python3 scripts/sd-recall.py sessions
python3 scripts/sd-recall.py stats
python3 scripts/reflect-report.py --months 3 --open
python3 scripts/trend-engine.py period-over-period --unit week
python3 scripts/skill-gap-finder.py --min-occurrences 3
python3 scripts/remember.py
```

---

<p>
  <img src="assets/readme/section-envs.svg" alt="支持的环境" width="100%" />
</p>

| 环境 | 格式要点 |
|------|----------|
| Claude Code | JSONL |
| Grok Build | chat_history.jsonl + events.jsonl |
| Kimi Code | wire.jsonl |
| Codex | response_item + event_msg |
| WorkBuddy | parentId 树 |
| Trae CN | LLM 摘要层 |
| ZCode | SQLite + transcript.jsonl |
| DIM / Reasonix | memory / sessions 目录 |
| 任意聊天 | JSON/CSV/文本（`dialog-adapter.py`） |

未知 JSONL 由 SchemaProbe 探测；路径经环境变量探测，不绑定本机固定目录。

---

## 架构（四层）

| 层 | 脚本 | 作用 |
|----|------|------|
| 0 PARSE | `echolib/` | 原文 → 统计 |
| 1 INDEX | `index-builder.py` | SQLite 持久化 / 增量 |
| 2 TREND | `trend-engine.py` | 时间切片聚合 |
| 3 DECISION | `skill-gap-finder` 等 | 提案（人工审批） |

---

## 版本历史

### v0.9.6 — Reflect 可视化 + 单环境数据隔离

- 首页用量总览：用时 / Token / 模型偏好
- 洞察按当前时段与环境现算；子页不混全库汇总
- 主题对比度校准；环境色与主题名解耦
- 中文发现标签与读数免责

### v0.9.5 — 工厂模式 + dispatch 统一

- `FIND_JSONL_REGISTRY` 数据驱动，消除重复 find_jsonl
- 适配器路由统一入参；`ENV_REGISTRY` 补全

### v0.9.4 — 路径匹配边界 + 适配器语义

- `_session_in_cwd` 段边界修复，避免 `bar` 误匹配 `bar-baz`

### v0.9.2 — 可移植路径 + 证据脱敏 + skill 自检

- 清除个人路径硬编码；`skill-health.py` 自检
- `skill-gap-finder` 默认脱敏

### 更早版本

见 git 历史：`v0.9.1` echolib 重构 · `v0.9.0` 自动记忆 · `v0.8.0` 四层架构 · Herdr `v0.2.x` 插件集成。

---

## License

[ISC](LICENSE)

---

<p align="center">
  <sub>session-digger v0.9.16 · 本机优先 · 只读会话 · 不上网报告</sub>
</p>
