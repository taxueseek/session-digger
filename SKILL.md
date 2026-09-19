---
name: session-digger
description: |
  会话历史分析入口。回忆、挖掘、提炼、管理、分析、规则化、多源导入。
  支持 Claude Code、Grok Build、Kimi Code、Codex、ZCode、WorkBuddy、Trae CN、
  DIM、Reasonix 等多环境 + 通用对话导入。
  触发：session-digger、回忆一下、查下历史、之前怎么做的、上次讨论过什么、
  分析会话、挖掘 git 历史、管理记忆、之前那个、之前说的、上次那个、
  之前讨论的、之前的版本、之前的方式、上次提到的、之前不是、
  技能使用分析、技能洞察、哪些技能没用过、技能差距、优化 skill、
  记不记得、之前看过、上次读的、之前写的、之前做的、导入对话、微信导入、
  使用回顾、reflect、usage recap、用了多久、AI 使用习惯、使用报告、
  token 用量、花了多少钱、模型消耗、缓存命中率
version: 0.9.27
---

# session-digger

> 你说了什么、做了什么、学到了什么——全在这。只做路由，不做分析。

支持环境：Claude Code、Grok Build、Kimi Code、Kimix CLI、Codex、Cursor、ZCode、WorkBuddy、Trae CN、DIM、Reasonix + 通用对话导入（`/import`）。路径与数据根均通过环境探测，不绑定本机固定目录。

## 子技能调度协议（0.9.18）

在 **保持四层架构与主命令精炼** 的前提下，子技能是专精增益层，不是第二套内核。

1. **专精优先**：用户意图与某子技能 `description` 高度匹配时，**先加载该子技能**，不要只用泛化主命令空转。  
   例：「错误根因 / 意图分类」→ `deep-analysis`；「环境坏了」→ `env-doctor` / `native-diag`。
2. **索引先行**：分析类子技能前确认索引可用（`/index` 或 `index-builder.py build`）；定位会话走 `index.db`（见 `skills/common_paths.py`）。
3. **combo 收尾**：主命令或子技能完成后读 `combo_map.json` 对应 next，**只提示 1–2 个下一步**，不输出整张路由表。
4. **不塌层**：子技能不私自入库、不替代 echolib；L3 提案（optimize / apply）必须人审。
5. **目标**：新主路由 + 调优子技能 **优于** 新主路由 + 未挂载的老子技能（路径对齐 + combo 可达 + 专精优先）。

## Path resolution

所有命令/脚本使用同一套根目录探测（**禁止**写入个人仓库路径）：

```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
if [[ -z "$SD_ROOT" || ! -f "$SD_ROOT/scripts/sd-recall.py" ]]; then
  for _c in \
    "$HOME/.agents/skills/session-digger" \
    "$HOME/.claude/plugins/session-digger" \
    "$HOME/.claude/skills/session-digger" \
    "$HOME/.grok/skills/session-digger"
  do
    [[ -f "$_c/scripts/sd-recall.py" ]] && SD_ROOT="$_c" && break
  done
fi
```

优先：`SESSION_DIGGER_ROOT` → 宿主插件根 → 相对本文件 → 常见 skill 安装位。多副本并存时务必 export `SESSION_DIGGER_ROOT`。

路由前扫一眼：只有「帮我看看」「查一下」没具体内容的 → 问一句「查什么——会话？时间线？经验？」。有「之前」「上次」但没时间/关键词的 → 问一句「大概什么时间？记得什么关键词？」

## 主命令

| 用户说的 | 去 |
|---------|-----|
| 回忆/搜索特定主题、之前怎么做的、最近会话、话题浏览 | `/recall`（吸收 `/recap`、`/topics`、`/topic-scan`） |
| token 用量 / 花了多少钱 / 模型消耗 | `/usage` |
| 使用回顾 / 时段热力 / 多环境习惯报告 | `/reflect`（吸收 `/trend`、`/optimize`） |
| 找错误模式、重试循环、用户修正、经验教训 | `/analyze`（吸收 `/lessons`） |
| 全局概览、记忆状态 | `/dashboard` |

做完后读 `combo_map.json` 提示下一步。不输出路由过程。

### `/usage` — 跨环境 token 可观测

跨环境汇总各模型账单级 token 与缓存命中率（**非**产品侧 quota 面板）。
取法（family 模式，主/子不双计）与口径：`references/usage.md`。

### `/recall` 常用变体

- 最近一次会话摘要 → `/recap` 或 `/recall --recap`
- 话题切分 / 主题总览 → `/topics`、`/topic-scan`
- 跨环境搜索 → `/recall --agent cross`
- 压缩后恢复决策点 → `/recall --decisions`

### `/reflect` 常用变体

- 周/月环比、工具回归 → `/trend`
- 跨会话技能差距、SKILL.md 提案 → `/optimize`
- HTML 使用报告 → `scripts/reflect-report.py`

### `/analyze` 常用变体

- 经验教训 / 踩坑回顾 → `/lessons`

## 检索预算与停止条件

读会话历史是最容易失控的动作：一次查询的命中会成片增长，而每一条都要进上下文。默认按下面的额度走。

| 动作 | 默认额度 | 停止条件 |
|------|---------|---------|
| `search` | `--limit 5` | 定位到会话即停，不为一句话翻遍全部命中 |
| `sessions` | `--limit 20` | 在时间线上定位到目标即停 |
| 逐条读消息 | 单会话 ≤ 50 条 | 连续两页没有新证据即停 |
| 跨会话统计 | 先聚合再取样 | 不为统计逐会话翻页 |

- **正文与工具输出分两层**：默认只匹配人写的正文。工具输出是独立分面（`facet=TOOL`），`--include-tools` 才并入，专查用 `index-builder.py search <词> --tools-only`。实测依据：并入默认检索时，一个只在命令输出里出现过的词会让 top-20 里的 7 行变成工具摘要（占 35% 版面），而换回的独有命中只有 10%。
- **空结果不是结论**：一次查询为空只说明「这个范围里没有」。空结果会打印作用域、索引状态、以及同词在更大范围里的命中数——按提示换范围再问一次，然后才下结论。

## 子命令

以下命令仍可用，经标志、子命令文件或专项 skill 进入（不必从主表记忆）：

查表：`references/command-map.md`（子命令 / 专项 skill 的完整入口，主表没命中时再读）。

### 子技能一览（`skills/`）

| 子技能 | 层 | 何时用 |
|--------|----|--------|
| `jsonl-core` | L0–L1 | 解析/恢复/格式 |
| `deep-analysis` | L2 | 单会话错误根因 + 意图 |
| `experience-synthesis` | L3 | 跨会话教训提炼 |
| `git-mining` | 旁路 | 会话 ↔ git |
| `memory-management` | L3 | 记忆生命周期 |
| `skill-insight` | L3 | 技能用量 + 资产自检 |
| `env-doctor` | 运维 | 跨环境综合诊断 |
| `native-diag` | 运维 | 原生命令采集（供 env-doctor） |

共享路径：`skills/common_paths.py`（`SESSION_DIGGER_DATA_DIR` / index.db）。

## Architecture (four-layer model)

| Layer | Script / skill | What it does | Trust level |
|-------|----------------|-------------|-------------|
| 0 PARSE | `echolib/` + `jsonl-core` | Raw transcript → stats (ground truth) | Exact |
| 1 INDEX | `index-builder.py` | Stats → SQLite（`jsonl_path` 供子技能定位） | Rebuildable |
| 2 TREND | `trend-engine` + **`deep-analysis`** | 聚合 + 单会话深挖 | Pure / extractive |
| 3 DECISION | skill-gap + skill-health + **experience-synthesis** | 提案须人审 | Judgment call |

Never collapse layers: each has a different cost and a different trust level.

## Speed tier

1. **`/index` 先建索引** → 后续所有搜索走 SQLite FTS5，<50ms
2. **`sd-recall.py`** → 统一 Python 进程，替代 bash 子进程链，5-10x 提速
3. **增量缓存** → 未变化的 session 不再解析（mtime 判断）
4. **`--decisions` 标记** → 只提取决策点，减少 token 消耗 60-80%

## DO NOT

- 从零创建新 skill → `skill-search`（全生命周期主干：创建/审计/优化/发布）
- 实时监控会话 → Claude Code 原生能力
- 修改/编辑会话 JSONL 文件 → 只读分析，不写原始数据
- 替代 git log → `git-mining` 是补充视角，不是替代
- `/apply` 写规则前未经用户审批 → 必须 y/n/e/a/q 逐条确认
- `/import` 不做微信解密 → 微信数据识别分析走 `wechat-digger` 子技能（自带只读查询；密钥提取与解密实现不分发，需用户自备已解密库）
- `/optimize` 自动编辑 SKILL.md → 只草拟提案，必须用户确认后手动应用
- 趋势分析替代单会话分析 → 趋势看方向，单会话看细节，两者互补
- 在命令/文档中硬编码个人机器路径（如某用户家目录下的私有仓库）→ 只用 Path resolution
- `/optimize` 输出默认不应含绝对路径或用户名 → `skill-gap-finder` 已脱敏；需要路径时显式 `--include-paths`

---

## Cache hit rate reporting（强制口径）

面向用户的缓存表 / 环境对比，**只报会话均命中率**（`rate > 0` 的会话算术平均）。

- **不要**默认输出 Token 加权命中率（除非用户明确要求「按 token 加权」）
- **零缓存 / 无字段 / 未标注模型 / 有效会话 &lt; 3** → 进排除清单，不进主表
- API：`echolib.build_cache_hit_tables` / `mean_cache_hit_rate` / `cache_rate_eligible`
- 全文：`references/cache-report-rules.md`
## Changelog

版本历史（v0.3 → 当前，含旁线条目）在 `CHANGELOG.md`。此处不再内联：它占 79% 的篇幅，
而本文件每次激活都会整体进上下文。要查「改了什么、为什么改」读那里。
