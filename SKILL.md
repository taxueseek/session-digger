---
name: session-digger
description: |
  会话历史分析入口。回忆、挖掘、提炼、管理、分析、规则化、多源导入。
  支持 Claude Code、Grok Build、Kimi Code 三个环境 + 通用对话导入。
  触发：session-digger、回忆一下、查下历史、之前怎么做的、上次讨论过什么、
  分析会话、挖掘 git 历史、管理记忆、之前那个、之前说的、上次那个、
  之前讨论的、之前的版本、之前的方式、上次提到的、之前不是、
  技能使用分析、技能洞察、哪些技能没用过、
  记不记得、之前看过、上次读的、之前写的、之前做的、导入对话、微信导入
version: 0.9.0
---

# session-digger

> 你说了什么、做了什么、学到了什么——全在这。只做路由，不做分析。

支持环境：Claude Code（`~/.claude/projects/`）、Grok Build（`~/.grok/sessions/`）、Kimi Code（`~/.kimi-code/sessions/`）+ 通用对话导入（`/import`）。

路由前扫一眼：只有「帮我看看」「查一下」没具体内容的 → 问一句「查什么——会话？时间线？经验？」。有「之前」「上次」但没时间/关键词的 → 问一句「大概什么时间？记得什么关键词？」

| 用户说的 | 去 |
|---------|-----|
| 回忆/搜索特定主题、之前怎么做的 | `/recall`（构建索引后自动走 FTS，极速） |
| 时间线、项目进展 | `/timeline` |
| 全局概览、记忆状态 | `/dashboard` |
| 提炼经验、找重复模式 | `experience-synthesis` |
| 管理记忆文件、审计/清理 | `memory-management` 或 `/audit` |
| 解析会话数据 | `jsonl-core` |
| 挖掘 git 历史 | `git-mining` |
| 最近一次会话 | `/recap` |
| 跨所有环境搜索 | `/recall --agent cross` |
| 保存分析结果供复用 | `/save-summary` |
| 找错误模式、重试循环、用户修正 | `/analyze` |
| 趋势分析、周/月环比、工具回归检测 | `/trend` |
| 跨会话技能差距分析、SKILL.md 提案 | `/optimize` |
| 检测未知 agent 格式 | `format-detector.py` |
| 分析后采纳规则写入 CLAUDE.md | `/apply` |
| 压缩后恢复上下文 | `/recall --decisions` 或 sd-recall.py |
| 话题切分、浏览讨论主题 | `/topics` |
| 建立搜索索引、加速查询 | `/index` |
| 导入外部对话（微信/JSON/CSV/文本） | `/import` |
| 会话主题总览、按主题聚类、成本分布 | `/topic-scan` |
| 选主题后提取上下文包路由到 taxue-* 技能 | `/topic-scan --topic <编号>` |
| 修复/恢复会话 | `jsonl-core` + `/recall` |
| 整理记忆 | `memory-management` + `/audit` |
| 技能使用洞察、哪些技能闲置、使用习惯分析 | `skill-insight` |

做完后读 `combo_map.json` 提示下一步。不输出路由过程。

## Architecture (four-layer model)

Inspired by agent-transcript-analyzer, each layer has a distinct cost and trust level:

| Layer | Script | What it does | Trust level |
|-------|--------|-------------|-------------|
| 0 PARSE | `echolib.py` | Raw transcript → stats (ground truth) | Exact |
| 1 INDEX | `index-builder.py` | Stats → SQLite persistent storage (cache) | Rebuildable |
| 2 TREND | `trend-engine.py` | Index → time-sliced aggregation | Pure arithmetic |
| 3 DECISION | `skill-gap-finder.py` | Patterns → SKILL.md proposals | Judgment call (human-approved) |

Never collapse layers: each has a different cost and a different trust level.

## Speed tier

1. **`/index` 先建索引** → 后续所有搜索走 SQLite FTS5，<50ms
2. **`sd-recall.py`** → 统一 Python 进程，替代 bash 子进程链，5-10x 提速
3. **增量缓存** → 未变化的 session 不再解析（mtime 判断）
4. **`--decisions` 标记** → 只提取决策点，减少 token 消耗 60-80%

## DO NOT

- 从零创建新 skill → `skill-creator`
- 实时监控会话 → Claude Code 原生能力
- 修改/编辑会话 JSONL 文件 → 只读分析，不写原始数据
- 替代 git log → `git-mining` 是补充视角，不是替代
- `/apply` 写规则前未经用户审批 → 必须 y/n/e/a/q 逐条确认
- `/import` 无法自动解密微信数据库 → 先用 wechat-local-vault 导出明文
- `/optimize` 自动编辑 SKILL.md → 只草拟提案，必须用户确认后手动应用
- 趋势分析替代单会话分析 → 趋势看方向，单会话看细节，两者互补

---

*session-digger v0.9.0 — 四层架构 + 趋势分析 + 技能差距分析 + 格式检测 + 自动记忆沉淀 + 技能使用洞察*
