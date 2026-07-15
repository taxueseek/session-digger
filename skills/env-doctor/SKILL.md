---
name: env-doctor
description: |
  跨 AI 编码环境自检诊断工具箱 v0.2。
  核心思路：充分运用每个环境的原生诊断命令 + 薄脚本补盲区 + 模型推理综合。
  对未知环境使用通用降级探测。
  触发：env-doctor、环境自检、环境诊断、体检、环境冲突、配置检查、
  doctor、自检、环境健康、env health、infrastructure check、环境巡检
version: 0.2.0
---

# env-doctor

> 原生命令为主、薄脚本补盲区、模型推理综合诊断。

## 核心原则

1. **原生命令优先** — 每个环境的 built-in 诊断是最权威的，不重写
2. **脚本只做盲区** — 原生命令做不到的事才用脚本（跨环境、网络、历史模式）
3. **模型是大脑** — 调度策略由模型动态决定，不写死路由
4. **未知环境降级** — 遇到未注册的 agent 工具，用通用探测发现能力

## 调度策略（模型决策参考）

```
用户请求自检
    │
    ▼
 capabilities.json → 确认目标环境有无注册
    │
    ├── 已知环境（claude/codex/grok/kimi）
    │       │
    │       ▼
    │   调度原生命令（第一优先级，权威数据）
    │       │
    │       ├── 原生命令覆盖的维度 → 直接信任
    │       │
    │       └── 原生命令的 blind_spots → 调度对应脚本补充
    │               │
    │               ▼
    │           脚本数据 + 原生数据 → 模型综合推理 → 报告
    │
    └── 未知环境（未注册）
            │
            ▼
        unknown_env_fallback 通用探测
            │
            ▼
        which / --version / --help / 配置目录 / 配置文件
            │
            ▼
        输出原始数据 + 建议模型是否值得深入
```

## 各环境的原生命令

| 环境 | 命令 | 能查什么 | 输出格式 |
|------|------|---------|---------|
| Claude Code | `/doctor` | 安装、hooks、PATH、settings、skills、MCP | text |
| Claude Code | `/context` | token 占比、上下文分类 | visual grid |
| Claude Code | `/hooks` | 注册列表、来源、matcher | list |
| Claude Code | `/permissions` | allow/deny 规则 | list |
| Claude Code | `/mcp` | MCP 服务器 + OAuth 状态 | list |
| Codex | `codex doctor --json` | 18 项：网络、auth、config、install、MCP、更新 | structured JSON |
| Codex | `codex debug models` | 模型目录 | JSON |
| Grok | `grok inspect [--json]` | 版本、skills、hooks、agents、plugins、MCP | tree / JSON |
| Kimi Code | `kimi doctor config` | config.toml 校验 | text |
| Kimi Code | `kimi doctor tui` | tui.toml 校验 | text |

## 脚本（只做原生命令的盲区）

| 脚本 | 文件路径 | 何时调用 |
|------|---------|---------|
| `probe-network.sh` | `scripts/probe-network.sh` | 所有环境的 API 可达性（原生只看自己的） |
| `cross-path-audit.sh` | `scripts/cross-path-audit.sh` | PATH 冲突（OS 级，单环境不感知） |
| `drift-detector.py` | `scripts/drift-detector.py` | Skill 版本漂移（跨目录，单环境不感知） |
| `history-match.py` | `scripts/history-match.py` | 历史模式匹配（需会话索引，原生无） |

## 调用示例

### 全量巡检（模型决策）

```
1. 读 capabilities.json 确认可用环境
2. 并行或串行调度各环境原生命令
3. 从输出中提取 blind_spots 列表
4. 对每个 blind_spot 调用对应脚本
5. 综合所有数据生成报告
```

```bash
# 各环境原生诊断
codex doctor --json                           # Codex
grok inspect --json                           # Grok
kimi doctor config                            # Kimi Code

# 脚本补充
python3 scripts/probe-network.sh              # API 可达性
python3 scripts/cross-path-audit.sh           # PATH 冲突
python3 scripts/drift-detector.py             # Skill 漂移
python3 scripts/history-match.py              # 历史模式
```

### 针对性检查

```bash
# 只看网络
python3 scripts/probe-network.sh

# 只看 Claude 相关的历史模式
python3 scripts/history-match.py --env claude

# 只看 warning 级别的问题
python3 scripts/history-match.py --severity warning
```

### 未知环境降级探测

当遇到未注册的 agent 工具（如新的 XYZ Code）：

```bash
which xyz           # PATH 中是否存在
xyz --version       # 版本号
xyz --help 2>&1 | grep -iE 'doctor|check|status|config|debug'  # 发现诊断入口
ls -la ~/.<xyz>/    # 配置目录结构
cat ~/.<xyz>/config.*  # 配置健康度
ls ~/.<xyz>/skills/ # skill 安装数量
```

通用探测的目标：**发现该环境的自诊断入口，输出原始数据供模型决定是否深入，不强行判断。**

## 严重程度

| 级别 | 含义 | 行动 |
|------|------|------|
| CRITICAL | 需立即处理 | 影响核心功能（API 不可达、auth 过期、config 解析失败） |
| WARNING | 建议处理 | 已知会降低效率（hook 过多、skill 漂移、path 冲突） |
| INFO | 提示信息 | 可择机优化（环境变量冗余、API 链路分叉） |
| PASS | 正常 | 无需操作 |

## 报告格式

```
=== env-doctor 诊断报告 ===
环境覆盖: Claude Code / Codex / Grok / Kimi
数据源: /doctor + codex doctor --json + grok inspect + kimi doctor + 4 脚本

CRITICAL (N)
---
WARNING (N)
---
INFO (N)
---
PASS (N)
---

总结: [一句话评估]
建议: [按优先级排列的修复建议]
```

## Path resolution

```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-${CLAUDE_PLUGIN_ROOT:-${HERDR_PLUGIN_ROOT:-}}}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
```

## 下游协作

| 发现 | 推荐 |
|------|------|
| Skill 漂移 / 闲置 | → skill-insight + memory-management |
| 会话错误率异常 | → /analyze + /recall |
| 配置问题需修复 | → /apply（用户确认后） |
| 长会话过多 | → /compact 或拆分任务 |

## DO NOT

- 不重写原生命令已有的检查逻辑——调用它、信任它
- 不让脚本做模型能推理的事——脚本只采集原始数据
- 不在输出中暴露 API Key / token 完整值（只显示前 8 位 + 省略号）
- 不替代各环境原生的诊断命令——是补充和跨环境扩展
- 不对未知环境强行输出结论——只输出原始数据，让模型判断
