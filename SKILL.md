---
name: session-digger
description: |
  会话历史分析入口。回忆、挖掘、提炼、管理。
  支持 Claude Code、Grok Build、Kimi Code 三个环境。
  触发：session-digger、回忆一下、查下历史、之前怎么做的、上次讨论过什么、
  分析会话、挖掘 git 历史、管理记忆、之前那个、之前说的、上次那个、
  之前讨论的、之前的版本、之前的方式、上次提到的、之前不是、
  记不记得、之前看过、上次读的、之前写的、之前做的
version: 0.5.4
---

# session-digger

> 你说了什么、做了什么、学到了什么——全在这。只做路由，不做分析。

支持环境：Claude Code（`~/.claude/projects/`）、Grok Build（`~/.grok/sessions/`）、Kimi Code（`~/.kimi-code/sessions/`）。

路由前扫一眼：只有「帮我看看」「查一下」没具体内容的 → 问一句「查什么——会话？时间线？经验？」。有「之前」「上次」但没时间/关键词的 → 问一句「大概什么时间？记得什么关键词？」

| 用户说的 | 去 |
|---------|-----|
| 回忆/搜索特定主题、之前怎么做的 | `/recall` |
| 时间线、项目进展 | `/timeline` |
| 全局概览、记忆状态 | `/dashboard` |
| 提炼经验、找重复模式 | `experience-synthesis` |
| 管理记忆文件、审计/清理 | `memory-management` 或 `/audit` |
| 解析会话数据 | `jsonl-core` |
| 挖掘 git 历史 | `git-mining` |
| 最近一次会话 | `/recap` |
| 跨所有环境搜索 | `/recall --agent cross` |
| 保存分析结果供复用 | `/save-summary` |
| 修复/恢复会话 | `jsonl-core` + `/recall` |
| 整理记忆 | `memory-management` + `/audit` |
| 重新评分 | `memory-management` |
| 查看过期记忆 | `memory-management` + `/audit` |

做完后读 `combo_map.json` 提示下一步。不输出路由过程。

## DO NOT

- 从零创建新 skill → `skill-creator`
- 实时监控会话 → Claude Code 原生能力
- 修改/编辑会话 JSONL 文件 → 只读分析，不写原始数据
- 替代 git log → `git-mining` 是补充视角，不是替代

---

*session-digger v0.5.4 — 多环境支持 + 自动缓存 + 跨代理搜索 + 提取去重*
