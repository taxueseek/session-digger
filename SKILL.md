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
version: 0.9.21
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

1. 探测 `SD_ROOT`（见 Path resolution），将 `scripts/` 加入 `sys.path`
2. 拉取用量（family 模式，主/子不双计）：
   - ZCode：`echolib.zcode_aggregate_model_usage(mode="family")`
   - Grok：`echolib.grok_aggregate_model_usage()`（默认 `mode="family"`）
3. 展示按环境 × 模型的摘要表：input / output / cache_read / total / model_calls / sessions / **cache_hit_rate**
4. 缓存命中率口径见下文「Cache hit rate reporting」；无数据的环境标明「无数据」而非 0

```python
import echolib
zcode = echolib.zcode_aggregate_model_usage(mode="family")
grok = echolib.grok_aggregate_model_usage()  # mode="family"
# 每项: {model_id: {input_tokens, output_tokens, cache_read_tokens,
#                   total_tokens, model_calls, sessions, cache_hit_rate}}
```

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

## 子命令

以下命令仍可用，经标志、子命令文件或专项 skill 进入（不必从主表记忆）：

| 用户说的 | 去 |
|---------|-----|
| 模糊浏览会话（fzf） | `/recall-fuzzy` |
| 时间线、项目进展 | `/timeline` |
| **错误根因 / 意图分类 / 这次为啥失败** | **`deep-analysis`**（先于泛化 `/analyze`） |
| 提炼经验、找重复模式 | `experience-synthesis`（错误多时可先 deep-analysis） |
| 管理记忆文件、审计/清理 | `memory-management` 或 `/audit` |
| 解析会话数据 | `jsonl-core`（底层仍是 echolib） |
| 挖掘 git 历史 | `git-mining` |
| 保存分析结果供复用 | `/save-summary` |
| 找错误模式、重试循环、用户修正 | `/analyze` |
| 数据包 → 模型自发分析 → 结论回存（一站式深析） | `/deep-analyze` |
| 趋势分析、周/月环比、工具回归检测 | `/trend` |
| 跨会话技能差距分析、SKILL.md 提案 | `/optimize` |
| 技能资产自检（路由覆盖/硬编码/安装漂移） | `skill-insight`（`scripts/skill-health.py`） |
| 检测未知 agent 格式 | `format-detector.py` |
| 分析后采纳规则写入 CLAUDE.md | `/apply` |
| 建立搜索索引、加速查询 | `/index` |
| 导入外部对话（微信/JSON/CSV/文本） | `/import` |
| 选主题后提取上下文包路由到 taxue-* 技能 | `/topic-scan --topic <编号>` |
| 从会话中提炼持久知识 | `/extract` |
| 交互式清理过期记忆 | `/prune` |
| 群聊参与者画像提取 | `/profiles` |
| 全链路回溯：主题扫描 + 经验提炼 | `/digest` |
| 修复/恢复会话 | `jsonl-core` + `/recall` |
| 技能使用洞察、哪些技能闲置 | `skill-insight` |
| 环境自检、配置检查、跨环境冲突、环境健康诊断 | `env-doctor`（读 capabilities.json 调度原生命令 + 脚本） |
| 环境基础设施巡检、网络连通性、skill 漂移检测 | `env-doctor` |
| 调用各环境原生诊断命令、结构化输出到索引 | `native-diag`（`scripts/native-diag.py --env <claude|codex|grok|kimi|mimo|all>`） |
| 微信聊天记录识别分析（已解密库全史检索/群画像/商机跟进/噪音群治理/成文/语音导出/图片还原） | `wechat-digger` 子技能（`skills/wechat-digger`，用户自备已解密库） |

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

**v0.9.21** — 测量落地轮 + wechat-digger 媒体附加层（v0.0.8.1-pub）

- **测量框架落地**（PR #1/#2 方向）：`scripts/quality_corpus.py` 固定评估语料（7 场景 14 文件：短/长/重复话题/噪音工具轨迹/缺元数据/多语言 CJK/近重复，证据标记全埋点）+ `scripts/footprint.py` 九项足迹指标（摄取/解析/检索耗时、候选数、重复率、召回、证据覆盖、上下文规模、峰值内存、端到端延迟）+ `tests/test_quality_baseline.py` 质量门禁 8 项（基线包络冻结，改动须显式重录）。**首轮量化发现**：检索兜底路径 50KB 头窗口外深埋证据召回 0（语料 s2-long 实测）；近重复副本未去重（dup_rate 0.667）。两 PR 文档已合并进 `docs/engineering/` 并补实施落点
- **测试基线修复**：`test_grok_quality.py` 4 项失败归因 = v0.9.14 适配器迁移加严 `sessionUpdate` 过滤后夹具未跟进（实测本机 271/271 真实账单行 100% 带 `sessionUpdate`，生产语义正确）；夹具对齐真实格式并新增「非 turn_completed 不计账单」回归锁。`test_combo_coverage.py` 2 项失败 = 校验器盲区（漏扫子技能脚本目录与包目录），补扫修复
- **`skills/wechat-digger/` → v0.0.8.1-pub**：`extras` 命令族（语音元数据/SILK 导出、转账红包、好友申请）+ V2 `.dat` 图片离线还原（`images-discover`/`images-decrypt`，pycryptodome 为可选依赖，核心分析层保持零 pip）+ vault `--start/--end` 日期窗；边界修订=媒体文件离线解码不属于被剔除的「密钥提取/SQLCipher 解密」，解密栈仍不分发（契约测试锁死）；README 增「导出与转写口径」（JSON/Markdown 内置，PDF 走外部转换，语音转写接外部转写器）
- 测试基线：主套件 184 项全绿，wechat-digger 196 项通过/21 跳过

**v0.9.20** — 双线合并 + wechat-digger 子技能收录

- **`skills/wechat-digger/`**（发布版 v0.0.8.0-pub）：微信本地数据识别与分析子技能——全史检索/群画像/商机跟进/噪音群治理/跨会话成文；只含分析层与只读查询件，密钥提取与 SQLCipher 解密栈不分发，`keys`/`decrypt`/`refresh` 如实报 `tool_missing`；数据接入 = 用户自备已解密 vault / wx-cli / 导出文件三来源；`skills/wechat-digger/SYNC.md` 记录拷贝白名单、脱敏对照表（真实会话锚点→虚构名）与校验清单，供后续版本同步
- **双线合并**：主线（0.9.13→0.9.19）与 GPT 工作区旁线（CJK 中文检索、`/deep-analyze`、DSH+ZCode v2 适配、适配器修复、5 个子技能收录）在 07-15 分叉后各自演进，版本号曾双向撞车（旁线的 0.9.14/0.9.17/0.9.18 与主线不同内容）；本版把旁线工作全部并入主线，旁线条目原文保留于文末「旁线」段
- README 徽章与 frontmatter 同步至 0.9.20
**v0.9.19** — Kimix CLI 适配

- 新增 `kimix` 环境适配器（`_adapters_kimix.py`）：复用 Grok Build 适配器，session 格式几乎一致
- `ENV_REGISTRY` 添加 `kimix`（`~/.kigi/sessions/`）
- 支持 `/usage` 跨环境汇总 Kimix CLI token 用量
- Kimix 是基于 Grok Build 的非官方 Kimi Code CLI 社区构建版

**v0.9.18** — 子技能调优：专精优先协议 + deep-analysis 挂 combo + `common_paths`（index.db 定位）；目标「新主路由+调优子技能 > 新主路由+老子技能」。归档 `archive/pre-subskill-tune-20260717`。

**v0.9.17** — 三瓶颈硬化：hub 边界 / usage policy / adapter tier

- **B0 卫生**：`_knowledge` 补 `time`；Grok `_empty_stats("grok")`；ZCode 死代码删除；DimCode 异常打 debug；文档 `echolib/` 对齐
- **瓶颈①**：`_empty_stats` → `_helpers`；`ENV_REGISTRY` / `KNOWN_UNADAPTED` / `scan_*` → `_registry_data`；拆分适配器不再惰性依赖 hub
- **瓶颈②**：新增 `_policy.PROVIDER_POLICY`；`attach_cache_hit_rates` 可按 agent 解析口径；`finalize_session_stats`
- **瓶颈③**：`ADAPTER_TIER` + `tier_supports`；`build_cache_hit_tables(enforce_usage_tier=True)` 半适配不进 usage 主表；`scripts/smoke-multi-env.py`
- 测试：`tests/test_provider_policy.py`；全量 137 passed

**v0.9.16** — 主命令精简 + `/usage` 跨环境 token 可观测

- 路由表收敛为 5 主命令：`/recall`、`/usage`、`/reflect`、`/analyze`、`/dashboard`
- 新增 `/usage`：`zcode_aggregate_model_usage(mode="family")` + `grok_aggregate_model_usage()`；按模型展示 token 与 cache_hit_rate
- `/recap`/`/topics`/`/topic-scan` → `/recall`；`/trend`/`/optimize` → `/reflect`；`/lessons` → `/analyze`
- 其余入口下沉「子命令」表，仍可通过 flags / 命令文件 / 专项 skill 调用

**v0.9.15** — 主对话 / 子代理分列

- 对用户只展示「主对话」「子代理」；内部标记与路径不进报表
- `session_role_label` + `build_cache_hit_tables(split_role=True | role_filter=主对话)`
- 索引可区分时：DimCode / Grok 等主对话与子代理命中率分列，避免混算掩盖波动

**v0.9.14** — 缓存报表口径固化 + WorkBuddy 入库门控

- **报表契约**：主表仅会话均命中率；异常单独列；禁止默认加权污染理解
- **`build_cache_hit_tables` / `mean_cache_hit_rate` / `cache_rate_eligible`**
- **WorkBuddy** 强制适配器路径（修复 total 有数、cache/model 全丢）
- `references/cache-report-rules.md`；trend 聚合同步丢 rate≤0

**v0.9.13** — 增量索引与缓存命中语义：一行修一类数据准确性

- **`input_includes_cache` 显式语义**：`compute_cache_hit_rate` / `attach_cache_hit_rates` 支持适配器声明 token 口径；Claude/Kimi Code=非缓存 leg，Grok/ZCode/DimCode/Codex=总量含缓存，消灭自动推断在边界命中率上的误分类
- **Codex token 真源修复**：读 `total_token_usage` 嵌套字段 + 累计快照取 max（非 sum）— 此前大量会话 input/cache 恒为 0
- **Single-pass 边界门**：ZCode/Grok/Codex/Kimi wire 强制走适配器，杜绝单遍 JSONL 误算/漏算 token
- **DimCode 按会话指纹**：不再用整库 mtime 作全员失效键；任意会话写入不再触发 ~N 全量重索引
- **增量批处理**：指纹/tags 一次加载；`_PARSER_EPOCH` 解析器世代，兼容性修复后自动一次性重解析
- **Kimi standalone** 重新挂入 ENV/适配器；wire 路径接受文件或目录；StatusUpdate token 提取
- 单测：`tests/test_cache_and_incremental.py`；全量 114 passed
### 旁线条目（GPT 工作区开发线 2026-07→09，编号曾与主线撞车，原文保留）

**旁线 v0.9.18** — 子技能收录 wechat-digger（脱敏发布版）+ 版本统一

- **`skills/wechat-digger/`**：微信本地数据识别与分析子技能（发布版基线 v0.0.8.0-pub）。全史文本检索、群画像、关系网络、商机跟进、噪音群治理、跨会话成文；**只含分析层与只读查询件**——密钥提取（extract_keys）与 SQLCipher 解密栈不分发，`keys`/`decrypt`/`refresh` 子命令如实报 `tool_missing`，数据接入=用户自备已解密 vault / wx-cli / 导出文件三来源。`skills/wechat-digger/SYNC.md` 记录拷贝白名单、脱敏对照表（真实会话锚点→虚构名）与校验清单，供后续版本同步
- 主路由表与 DO NOT 同步更新（`/import` 不做微信解密 → 指向 wechat-digger）；`tests/` 208 通过基线上再加 wechat-digger 发布版 209 用例（live 锚点默认跳过）
- 版本号统一：README 徽章与 frontmatter 同步至 0.9.18（此前徽章停在 0.9.6、frontmatter 停在 0.9.14）

**旁线 v0.9.17** — deep-analyze：取数 → 模型自发分析 → 结论回存的一站式命令

- **`/deep-analyze` 命令**（`commands/deep-analyze.md` + `scripts/deep_analyze.py`）：一条命令产出有界数据包（默认 ≤12KB ≈ 3K token）——全局概况（全库+时间窗双口径、环境分布、按日趋势）+ 重点会话（规模/主题/用户消息样本/工具错误画像）+ 已有分析结论标注（避免重复分析）。模型拿到数据包后按四视角框架自发分析（工作主线/摩擦模式/决策转向/盲区建议），结论经 `save-summary` 回存摘要缓存形成复利，虚拟 scheme 会话（dimcode://）跳过回存
- **索引读取层** `index_builder/_reader.py`：`quick_stats_from_index`/`evidence_from_index` 从 sd-recall.py 提升为 canonical 层（sd-recall 改 import，删除本地副本），新增 `recent_sessions`（时间窗/环境/FTS 关键词/质量排序选会话）、`global_aggregates`（totals+按环境+按日一次连接出齐）、`has_summary_cache`（.summary.jsonl 探测）；`DECISION_PATTERNS` 单一真源迁入，sd-recall 反向引用
- **数据包卫生**：用户消息样本过滤系统注入（`<notification>`/`<system-reminder>`/`Image read from`/continuation 摘要）；`tool_errors_json` 进入选会话查询列（修复错误画像恒空的字段缺失）
- **测试**：`tests/test_deep_analyze.py` 新增 7 用例（选会话各分支/聚合形态/注入过滤/预算截断）；全套 137 通过
- **严谨性双门**（评审后补强）：①已有分析结论不再自动跳过——数据包列出结论条数/最新时间/要点，并用 `source_mtime` vs 索引 `jsonl_mtime` 判定「分析之后会话是否有新内容」，命令纪律改为**先向用户展示已有结论并询问是否重新分析**；②索引新鲜度门禁——取数前检查 `last_build`（默认 >6h 超龄自动增量重建，`--max-age-hours` 可调/-1 跳过），实测门禁捞回并行会话新产生的 369 个会话与 dsh/zcode_v2 新环境数据；③`global_aggregates` 时间窗口径修复（此前 totals 未按窗口过滤，窗口行显示全库数字）。全套 139 通过

**旁线 v0.9.16** — 中文检索从 0 到 1：CJK 分词 + 全文召回窗口 + 索引并行化

- **中文 FTS 修复**（最大瓶颈，实测「迁移」「数据库」等查询命中率为 0）：FTS5 unicode61 tokenizer 把连续 CJK 串当整块 token，「迁移」永远命中不了「数据库迁移完成」。修复 = 写入侧 `split_cjk`（CJK 逐字切分进 FTS）+ 查询侧 `build_match_query`（查询词对称重建为逐字 phrase，英文 token 加前缀 `*`），两函数同在 `index_builder/_cjk.py`，写入/查询/读回展示（`uncjk`）三方契约由 `tests/test_cjk_search.py` 金标门禁钉死。重建索引后真实数据验证：「去AI味」63 会话、「半调海报」「数据库迁移」均秒级命中（旧版全 miss）
- **查询串安全化**：旧 `safe_kw` 只处理引号冒号，`NEAR`/`OR`/括号等 FTS5 语法直通用户输入（遇特殊字符即静默报错回退）。`build_match_query` 只输出引号包裹的字面 token（布尔关键字也被字面化），注入不可能
- **召回窗口 500→16k**：各 adapter 的 `text[:500]` 硬截断（zcode/grok/workbuddy/cursor/universal 共 30+ 处，claude 不截——语义分裂）全部撤除，`extract_messages` 统一返回全文；FTS 端单点截断 `index_builder._schema.FTS_TEXT_CAP = 16000`。此前 13350 字符的回复只有前 500 字符可检索。展示层（`[:150]`/`[:300]`）本就自带截断，行为不变。索引 db 91→149MB
- **FTS 三查询点统一**：`sd-recall._fts_search`、`index-builder.search_fts`、`remember` skill 统计全部接入 `build_match_query`；`search_fts` 读回文本经 `uncjk` 还原
- **语义修正**：FTS 无命中返回 `[]`（不再回退全盘文件头扫描——那是几秒的无谓 IO 且结果更差）；索引缺失/查询失败返回 `None` 才回退，且 stderr 显式提示（旧 `except: return None` 静默吞错，故障表现为「搜不到」）
- **检索提速：索引数据复用**：`cmd_search` 命中路径不再逐会话重新解析 JSONL 文件（旧路径每会话 2 次全文件解析）——stats/消息/错误聚合一次 SQL 从 `sessions`/`messages_fts` 表直读，仅索引外的会话与 `--deep` 模式回源文件；`cmd_stats` 改纯 SQL 全量统计（旧版最多只统计 1000 个且逐文件解析），并标注 errors 口径（工具调用失败数，非解析错误）
- **索引构建并行化**：单会话计算提取为 `_compute_session`（编排/计算分离），`ProcessPoolExecutor` 并行（`SESSION_DIGGER_JOBS` 可调/强制串行，<24 会话自动串行）。zcode 314 会话实测 13.44s→3.85s（3.5×）；cross 全量 3758 会话 51.9s（旧代码同口径折算 ≈96s+，且那还是未加全文窗口的旧版）
- **timestamp 防御**：`cmd_search` 四处 `ts[:19]` 对 None/非字符串崩溃 → 统一 `_ts19()`
- **测试**：`tests/test_cjk_search.py` 新增 13 用例（分词往返/查询构造/注入安全/FTS 语义/金标查询）；全套 127 通过

**已知边界（记录待办）**：zcode transcript 断流重连（`stream_recovery_anchor_created`）后 streaming 重组可能丢中段文本，权威 `model_complete` 又被防重复逻辑跳过——真实案例：某会话 12601 字符回复的第 9740 字符处「周报」不可检索。修复需 2-pass 重构（先收集 complete 再流式），未纳入本版。「verdaccio」类词 0 命中为数据源边界（该词仅存在于 thinking/tool_use 块，非对话文本）。dimcode 虚拟会话（`dimcode://`）的索引 project_path 是截断产物（`dimcode:`），真实 cwd 未入索引，`session_in_cwd` 对 `://` 路径一律 False——`--scope current` 下 dimcode 会话（最大环境）永远 miss；修复需 scan/worker/row/cwd 判定 5 处契约联动，独立成轮。

**旁线 v0.9.15** — 契约一致性修复：extract_tools 全线打通 + kimix 路径查找纠错

- **kimix extract_tools 修复**（生产级 TypeError）：`kimix_extract_tools` 原本只有 `(path)` 一个参数，dispatch 层按通用四参契约调用即抛 `TypeError: kimix_extract_tools() got an unexpected keyword argument 'tool_filter'`；且内部错误地调用了 `_claude.extract_tools(path, agent="kimix")`（该函数无 `agent` 参数，二次 TypeError）。现改为全签名 `(path, tool_filter, errors_only, limit)` 并委托 `grok_extract_tools`（双文件 events+chat_history 关联），接受会话目录或 .jsonl 文件路径。真实 ~/.kimix 606 个会话全部可提取工具调用
- **删 dispatch 特判**：`dispatch_extract_tools` 的 `if agent == "grok"` 特判删除——`grok_extract_tools` 内部新增文件→目录自适应，契约统一为「四参 + 文件路径」；grok 真实路径 642 会话验证无回归
- **kimix_session_path 纠错**：原实现委托 `_grok_find_session_dir`，在 `~/.grok/sessions` 里找 kimix 会话（永远 miss）；重写为扫描 `~/.kimix/sessions/<group>/<sid>`，签名对齐 `(cwd, session_id=None)`，cwd 精确匹配 summary.cwd 优先
- **reasonix_list_sessions 返回类型对齐**：原返回 dict 列表，是 cross_tool 中 `isinstance(s, dict)` 双分支的唯一内置来源；改为返回 `SessionMeta`，cross_tool 的 dict 分支降级为仅兜底外部插件并更新注释
- **死代码清理**：zcode `limit*3` 空 if+pass、kimix 未用 `model` 变量、kimix_extract_tools 错误 docstring
- **测试**：`tests/test_kimix_adapters.py` 新增 3 用例（extract_tools 委托、dispatch 文件路径全链路、session_path 解析）；全套 113 通过

**旁线 v0.9.14** — Kimix 0.1.16 数据根收敛 + 每请求缓存指标数据源

- **数据根收敛**：`ENV_REGISTRY`/`_helpers.KIMIX_DIR` 统一为 `~/.kimix`（此前指向 `~/.kigi`，那是独立 CLI Kigi 的数据根，导致 Kimix 会话全部索引错位）
- **缓存命中率进索引**：schema v1→v2 新增 `cache_hit_rate` 列，`index-builder` 落库（此前只有 latest 分支有此能力，真源丢失）
- **每请求缓存指标数据源**：新增 `kimix_cache_metrics()`（读 `~/.kimix/metrics/cache_hit-*.jsonl`，按日聚合）与 `kimix_unified_cache_index()`（读 `~/.kimix/logs/unified.jsonl` 的 `shell.turn.inference_done`，带 sid 会话归属，mtime 缓存）；`kimix_session_stats` 在 updates.jsonl 缺 usage 时用 unified 精确数据回填，并打 `cache_source` 标记
- **索引构建**：`build_index` 对带 `cache_metrics` 的适配器自动写 `env_cache_metrics_*` 到 index_meta
- **路径路由修复**：`_env_path_markers()` 加 `~/.kimix/sessions` 标记——此前 Kimix 的 `chat_history.jsonl` 被文件名/结构线索误判为 grok，dispatch stats 全空
- **Grok 注册接线**：`ADAPTER_REGISTRY["grok"]` 改用 `_adapters_grok.py` 增强版（signals + updates.jsonl 计费 usage + 缓存命中），删除 `_adapters.py` 内 3 个无 usage 解析的旧副本；`<user_query>` 剥离逻辑补回增强版
- **Kigi 走通用适配器**：`.kigi` 不进 `ADAPTER_REGISTRY`（无专用适配器），仅加入 `KNOWN_UNADAPTED` 轻发现；`_scan_via_adapter` 对 universal 按环境根目录限定扫描（此前全屋扫描 500 条截断，`.kigi` 永远扫不到），sid 从路径派生（会话 uuid，避免 stem 碰撞）；`universal_list_sessions` 过滤 grok 家族辅助文件（updates/events/rewind_points/…）
- **重复注册表清理**：`_adapters.py` 末尾重复的 `ENV_REGISTRY`（缺 kimi/kimix，覆盖真源）删除，单一真源归 `_registry_data.py`
- **normalize 修复**：`_scan_via_adapter` 适配器返回会话目录时经 `normalize_session_path` 落到具体 JSONL（此前 fingerprint mtime=None 导致数百条 error）
- **测试**：新增 `tests/test_kimix_adapters.py`（6 用例）与 `tests/test_kigi_universal.py`（3 用例）；全套 110 通过。重建后 errors 579→14

**旁线 v0.9.13** — 工程质量修复：崩溃 bug + 重复循环 + 数据准确性

- **`_knowledge.py` 崩溃修复**：补齐 `import time as _time`（`save_analysis_result` / `load_analysis_result` / `build_summary_index` 调用即崩溃）
- **`_knowledge.py` `build_summary_index` 数据驱动化**：硬编码 4 环境路径 → `ENV_REGISTRY` 驱动（随注册表自动覆盖全部已知环境）
- **`_adapters_zcode.py` `zcode_session_path` 修复**：合并双层搜索为单层，消除每次 `sess_dir` 迭代后重复扫描全部 agent_dir 的 N×M 冗余
- **`_adapters.py` `_grok_session_stats`**：`_empty_stats("unknown")` → `_empty_stats("grok")`；移除冗余 `import os`；移除 `_grok_extract_messages` 内冗余 `import re`
- **SKILL.md**：四层模型 `echolib.py` → `echolib/`（v0.9.7 拆包后遗留）
- 单测：`tests/test_improvements.py` 新增崩溃防护 + adapter 注册完整性

**v0.9.12** — 一行修一类：scope 边界回归修复 + 发现层统一走注册表

- **`session_in_cwd` 抽到 `echolib._helpers`**：Claude dash / Grok URL 统一段边界；`$HOME` 永不命中全库；`bar` 不再误匹配 `bar-baz`（旁支 292ad0a 修复此前未合入 main）
- **`sd-recall find_sessions` 去硬编码**：不再只扫 claude/grok/kimi；`--agent` 动态取自 `ADAPTER_REGISTRY`（codex/cursor/zcode/…）
- **`cross_tool_list_sessions.agent` 改用 registry id**（`agent_display` 保留展示名），消灭下游对显示名的脆弱依赖
- **`normalize_session_path`**：适配器返回目录时落到 `chat_history.jsonl` 等具体文件
- **UX**：`sessions --scope current` 空结果时 stderr 提示 `--scope all`；`format-detector --help` 不再当路径
- **子技能回填**：`env-doctor` / `native-diag` / `deep-analysis`（SKILL 路由已写但 main 安装缺失）；native-diag 路径拼接补 `/`
- 单测：`tests/test_session_scope.py`

**v0.9.11** — Universal SchemaProbe 智能化：一通百通未知环境

- **路由置信度门控**：禁止用正文里的「claude/sonnet」等子串劫持专用适配器；路径优先 → 结构签名 → universal
- **SchemaProbe 家族探测**：`nested_message` / `nested_payload` / `flat_role` / `history_display` / `summary_card`
- **通用解析**：`<user_query>` 剥离、system/env 噪声过滤、display 历史日志、摘要卡 intent/actions
- **KNOWN_UNADAPTED** 扩展 newmax/proma/iflow/deepcode/codebuddy/commandcode 等发现位
- 单测：`tests/test_universal_probe.py`

**v0.9.10** — WorkBuddy / Trae CN 解析对齐 Claude·Codex·Cursor 水准

- **WorkBuddy**：修复 Exit Code 正则双转义（错误永不计数）；`_empty_stats` 占位 model 可覆盖；`<user_query>` 剥离；`reasoning` 思考块；cwd 过滤；ai-title → summary
- **Trae CN**：字面 `\\n` → 真换行；slug 去 `session_memory_`；时间 ISO 化；多日 shard 合并取最新 path；项目 slug 解码；outcome 失败软标错误
- 单测：`tests/test_workbuddy_trae_adapters.py`

**v0.9.9** — ZCode / Kimi Code 解析对齐 Claude·Codex·Cursor 水准

- **ZCode**：`modelRef` 字典解析（不再把 toolName 当 model）；`model_streaming` text_delta 重装；用量 camelCase；`tool_batch_complete` 错误；SessionMeta 列表 + DB 标题；slug=agent_*
- **Kimi Code**：assistant 只计 text 回合（think 不灌水）；`llm.request`/`usage.record` 取 model；slug=session uuid；按 turnId 合并 content.part；`workDir` cwd 过滤；单遍 tool join
- 单测：`tests/test_zcode_kimi_adapters.py`

**v0.9.8** — 借鉴 Grok Build resume-session：Claude/Codex/Cursor 适配增强

- **Cursor 适配器上线**：`agent-transcripts` JSONL + Desktop `state.vscdb`；`<user_query>` 剥离、`tool_use` 计数
- **路径表驱动 `detect_agent_type`**：恢复目录 marker + 文件名线索 + 内容签名；Cursor/Codex 不再误入 universal
- **Codex**：`CODEX_HOME` 双根扫描、完整 UUID 提取、`.jsonl.zst` 透明读取、`local_shell_call` 工具识别
- **一行消一类问题**：`_iter_jsonl` 透传 zstd；`_fast_find_jsonl` 返回 list（消灭 `len(generator)`）；`cross_tool` 默认排除 universal + 按环境 round-robin
- **修复**：`GROK_SEARCH_DB` 未定义导致 Grok list 崩溃；`output_text` 块提取回归

**v0.9.7** — 工程质量：死代码清理 + JSONL 解析归一 + 预存 bug 修复

- 5 模块共 28 个未使用 import 清理（`_helpers`/`_adapters`/`_claude`/`_models`/`_knowledge`）
- `iter_records()` + `session_stats()` 复用 `_iter_jsonl()`，消除 JSONL 解析路径重复
- 修复 `_adapters.py` 的 `SessionStats` 类型注解未导入（添加 `from __future__ import annotations`）
- 修复 `session_stats()` 中嵌套 `is_error` 永远不被计数（error 检测移到 `continue` 之前）
- 21 个公共函数补全 docstring
- CLAUDE.md 架构描述更新（`echolib.py` → `echolib/`）

**v0.9.6** — Reflect 使用回顾：可视化升级 + 单环境数据隔离 + 主题对比度
- `reflect-report` 首页用量总览（用时 / Token / 模型偏好双栏），借鉴数据报告呈现
- 核心发现按**当前时段 + 当前环境**现算，进入 Kimi 等子页不再混入 Claude 等全库汇总
- 主题：跟随系统 / 奶油暖色 / 深褐 / 纯黑 / 冷蓝 / 墨纸；环境色只标侧栏，不劫持主题名
- 字色与强调色对比度校准（约 4.5:1）；中文标签（要盯/留意…）与读数免责
- Hallmark 可视化层：design-tokens 主题体系 + 自包含 HTML 报告

**v0.9.5** — 工厂模式消除 10 个重复 find_jsonl 函数 + dispatch 特化分支消除
- `FIND_JSONL_REGISTRY` 数据驱动：`_project_based_find_jsonl()` + `_tiered_find_jsonl()` 两个工厂
  替代 10 个重复的 `_xxx_find_jsonl()` 函数（-88 行，-35%）
- `dispatch_extract_tools()` 消除 `if agent == "grok"` 特化分支：适配器内部统一入参
- `dispatch_extract_messages()` 消除 `no_tools` 参数含义分歧：适配器路由不依赖 Clsude 专用参数
- `KNOWN_UNADAPTED` 消除冗余：5 个已适配环境移至 `ENV_REGISTRY`，不再与适配器表并列维护
- `ENV_REGISTRY` 补全 5 个新适配环境 + 双目录同步机制

**v0.9.4** — 路径匹配边界检查 + 适配器解析语义修复
- `_session_in_cwd()` 全面边界修复：`$HOME/bar` 不再误匹配 `$HOME/bar-baz` 的会话
  - 移除 `dash[1:] in ps` 冗余条件、`dash in ps` 和 `encoded_cwd in ps` 增加段边界检查（后一字符须为 `/` 或 `.`）
  - basename fallback 只保留带明确路径分隔符的标记（`/bar/`、`%2Fbar%2F`），移除 `-bar-`、`_bar_` 等会在 segment 名称内部误匹配的标记
- `detect_agent_type(path=None)` 不再返回 `"both"`（非有效 adapter 名），改为返回 `existing[0]`（最具体的环境，因 `_ENV_PATH_MARKERS` 按特异性降序排列）
- 根因：路径编码中 `-` 既是 segment 分隔符，也是 segment 名称的合法字符（如 `bar-baz`），简单 substring 匹配无法区分

**v0.9.3** — 高杠杆工程优化
- `detect_agent_type()` 数据驱动重构：13个重复 if-block → `_ENV_PATH_MARKERS` 单一表驱动，新增环境零改核心代码
- `format-detector.detect_one()` 惰性读取 + 提前终止：仅读前40行（非全文），高置信度(≥8)立即返回
- `iter_records()` 异常安全加固：OSError 不再导致未处理崩溃
- `_make_simple_list_sessions()` 性能提升：filesystem mtime 替代 JSONL 首行解析（O(1) vs O(N)）

*session-digger v0.9.19 — 跨环境会话挖掘 + 子技能编排 + 本机使用回顾 + Kimix CLI 适配*
