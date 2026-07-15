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
  使用回顾、reflect、usage recap、用了多久、AI 使用习惯、使用报告
version: 0.9.8
---

# session-digger

> 你说了什么、做了什么、学到了什么——全在这。只做路由，不做分析。

支持环境：Claude Code、Grok Build、Kimi Code、Codex、Cursor、ZCode、WorkBuddy、Trae CN、DIM、Reasonix + 通用对话导入（`/import`）。路径与数据根均通过环境探测，不绑定本机固定目录。

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

| 用户说的 | 去 |
|---------|-----|
| 回忆/搜索特定主题、之前怎么做的 | `/recall`（构建索引后自动走 FTS，极速） |
| 模糊浏览会话（fzf） | `/recall-fuzzy` |
| 时间线、项目进展 | `/timeline` |
| 全局概览、记忆状态 | `/dashboard` |
| 提炼经验、找重复模式 | `experience-synthesis` |
| 管理记忆文件、审计/清理 | `memory-management` 或 `/audit` |
| 解析会话数据 | `jsonl-core` |
| 挖掘 git 历史 | `git-mining` |
| 最近一次会话 | `/recap` |
| 使用回顾 / 时段热力 / 多环境习惯报告 | `/reflect`（`scripts/reflect-report.py`） |
| 提炼经验教训、回顾之前踩过的坑 | `/lessons` |
| 跨所有环境搜索 | `/recall --agent cross` |
| 保存分析结果供复用 | `/save-summary` |
| 找错误模式、重试循环、用户修正 | `/analyze` |
| 趋势分析、周/月环比、工具回归检测 | `/trend` |
| 跨会话技能差距分析、SKILL.md 提案 | `/optimize` |
| 技能资产自检（路由覆盖/硬编码/安装漂移） | `skill-insight`（`scripts/skill-health.py`） |
| 检测未知 agent 格式 | `format-detector.py` |
| 分析后采纳规则写入 CLAUDE.md | `/apply` |
| 压缩后恢复上下文 | `/recall --decisions` 或 sd-recall.py |
| 话题切分、浏览讨论主题 | `/topics` |
| 建立搜索索引、加速查询 | `/index` |
| 导入外部对话（微信/JSON/CSV/文本） | `/import` |
| 会话主题总览、按主题聚类、成本分布 | `/topic-scan` |
| 选主题后提取上下文包路由到 taxue-* 技能 | `/topic-scan --topic <编号>` |
| 从会话中提炼持久知识 | `/extract` |
| 交互式清理过期记忆 | `/prune` |
| 群聊参与者画像提取 | `/profiles` |
| 全链路回溯：主题扫描 + 经验提炼 | `/digest` |
| 修复/恢复会话 | `jsonl-core` + `/recall` |
| 整理记忆 | `memory-management` + `/audit` |
| 技能使用洞察、哪些技能闲置 | `skill-insight` |
| 环境自检、配置检查、跨环境冲突、环境健康诊断 | `env-doctor`（读 capabilities.json 调度原生命令 + 脚本） |
| 环境基础设施巡检、网络连通性、skill 漂移检测 | `env-doctor` |
| 调用各环境原生诊断命令、结构化输出到索引 | `native-diag`（`scripts/native-diag.py --env <claude|codex|grok|kimi|mimo|all>`） |

做完后读 `combo_map.json` 提示下一步。不输出路由过程。

## Architecture (four-layer model)

| Layer | Script | What it does | Trust level |
|-------|--------|-------------|-------------|
| 0 PARSE | `echolib.py` | Raw transcript → stats (ground truth) | Exact |
| 1 INDEX | `index-builder.py` | Stats → SQLite persistent storage (cache) | Rebuildable |
| 2 TREND | `trend-engine.py` | Index → time-sliced aggregation | Pure arithmetic |
| 3 DECISION | `skill-gap-finder.py` + `skill-health.py` | Patterns / asset health → SKILL.md proposals | Judgment call (human-approved) |

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
- 在命令/文档中硬编码个人机器路径（如某用户家目录下的私有仓库）→ 只用 Path resolution
- `/optimize` 输出默认不应含绝对路径或用户名 → `skill-gap-finder` 已脱敏；需要路径时显式 `--include-paths`

---

## Changelog

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

*session-digger v0.9.6 — 跨环境会话挖掘 + 本机使用回顾报告*
