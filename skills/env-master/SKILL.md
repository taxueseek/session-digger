---
name: env-master
description: 统一环境自检系统——一个命令查看所有 AI 编码环境的健康视图。原生命令优先、脚本只补盲区（跨环境、网络、历史模式）、0-100 量化评分七维雷达、统一路由自动发现已安装工具。触发：环境自检、环境健康检查、AI 工具体检、环境诊断、env-master。
---

# env-master — 统一环境自检系统

## 一句话定位

**一个命令，所有 AI 编码环境的健康视图。**

## 核心理念

1. **原生命令优先** — 每个环境的 built-in 诊断是最权威的，不重写
2. **脚本只做盲区** — 原生命令做不到的事才用脚本（跨环境、网络、历史模式）
3. **量化评分** — 0-100 健康分，七维度雷达，趋势可追踪
4. **统一路由** — 一个入口调度所有环境，自动发现已安装的工具
5. **存储健康（D7）** — 主机级维度：磁盘、缓存、编译产物、会话数据，只生成可清理计划、不执行删除

## 适用场景

• 用户问「我的开发环境是否正常」「帮我检查所有 AI 工具的配置」
• 用户问「哪个环境有问题」「环境健康度怎么样」
• 新机器初始化后快速验证所有环境
• 排查跨环境冲突（PATH、skill 版本、API 链路）
• 定期体检（结合 cron 追踪健康趋势）

## 调用方式

### CLI 直接调用

```bash
# 全量检查（默认）
python3 env-master.py

# 只检查某个环境
python3 env-master.py --env claude

# JSON 输出（可管道）
python3 env-master.py --format json | jq '.global_score'

# 快速模式（跳过跨环境检查）
python3 env-master.py --no-cross

# 只检查指定维度
python3 env-master.py --dimensions config,auth

# 与上次结果对比
python3 env-master.py --history
```

### Claude Code 斜杠命令

在会话内输入 `/env-master` 触发（需注册为 slash command）。

## 支持环境

| 环境 | 原生命令 | 状态 |
|------|----------|------|
| Claude Code | /doctor, /context, /hooks, /mcp, /permissions | ✅ 已验证 |
| Codex | codex doctor --json | ✅ 已验证 |
| Grok Build | grok inspect --json | ✅ 已验证 |
| Kimi Code | kimi doctor config/tui | ✅ 已验证 |
| MiMo Code | mimo debug config/paths | ✅ 已验证 |
| DeepSeek | deepseek --check | 🔍 推断实现 |
| Cursor | cursor --check | 🔍 推断实现 |
| Aider | aider --version + 配置 | 🔍 推断实现 |

## 评分算法

- **扣分制**：critical -25, warning -10, info -5，下限 0
- **七维度**：install(13%), config(22%), auth(18%), network(14%), extensions(13%), cross_env(10%), storage(10%)
- **存储维度（D7）**：主机级，单独评分；全局分 = 环境均分 × 0.9 + 存储分 × 0.1
- **全局分**：各环境加权平均 + 存储分
- **状态阈值**：≥85 ok, ≥60 warn, <60 fail

## 存储健康检查（D7）

```bash
# 只查存储维度（快速，不跑环境诊断）
python3 env-master.py --env storage --format table

# 全量检查时自动包含存储
python3 env-master.py --env all
```

存储检查覆盖：
- **磁盘**：剩余空间（<20% 警告，<10% 严重）
- **编译产物**：工作区 `*/target`（>500MB 列出，标注「需重编」风险）
- **应用缓存**：`~/Library/Caches` 大户（>500MB）
- **会话/下载**：`~/.kimix` 的 memtrace、旧版安装包、sessions 归档建议
- **trash**：超 30 天未清理文件

安全原则：**只生成可清理计划（cleanable_items），绝不执行删除**。每项带风险分级：
`可直接清`（可再生缓存）/ `需重编`（编译产物）/ `需确认`（用户数据）。删除由用户确认后按项目规则移入 `.trash/`。

扫描根可配置：`ENV_MASTER_CACHE_ROOT` / `ENV_MASTER_WORKSPACE_ROOTS` / `ENV_MASTER_DATA_HOME`。

## 与现有技能的关系

| 现有技能 | 关系 | 说明 |
|----------|------|------|
| native-diag | 被整合 | 其逻辑已纳入 env-master 的适配器层 |
| env-doctor | 被整合 | 其调度逻辑和盲区脚本已纳入 env-master |
| /doctor | 被扩展 | /doctor 仍可用，/env-master 是其超集 |

**向后兼容**：现有 native-diag.py 和 env-doctor 脚本仍可直接调用，内部委托给 env-master 引擎。

## 输出示例

```
╔══════════════════════════════════════════════════════════╗
║           env-master 统一环境自检报告                     ║
╠══════════════════════════════════════════════════════════╣
║  全局健康分: 82/100  [■■■■■■■■■■□□□]  OK               ║
║  环境覆盖: 3/5 (Claude ✓  Codex ✓  Grok ⚠)              ║
╠══════════════════════════════════════════════════════════╣
║  D1 安装  ████████░░  80                                  ║
║  D2 配置  █████████░  90                                  ║
║  D3 认证  ██████████  95                                  ║
║  D4 网络  ■■■■■■■□□□  70  ← 短板                        ║
║  D5 扩展  ████████░░  85                                  ║
║  D6 协调  ■■■■■■■□□□  72                                  ║
╠══════════════════════════════════════════════════════════╣
║  CRITICAL (0)                                             ║
║  WARNING (1)                                              ║
║  ⚠ [network]  api.longcat.chat 延迟 5.2s                  ║
╠══════════════════════════════════════════════════════════╣
║  优先修复: 检查代理配置降低 API 延迟                       ║
╚══════════════════════════════════════════════════════════╝
```

## Path Resolution

```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
```

## DO NOT

• 不重写原生命令已有的检查逻辑——调用它、信任它
• 不让脚本做模型能推理的事——脚本只采集原始数据
• 不在输出中暴露 API Key / token 完整值
• 不替代各环境原生的诊断命令——是补充和跨环境扩展
• 不对未知环境强行输出结论——只输出原始数据，让模型判断
