# env-master — 统一环境自检系统

## 设计哲学

### 第一性原理

问题的本质不是「每个环境各自诊断」，而是：
> 用户需要一个**跨所有 AI 编码环境的统一健康视图**——一个命令、一个分数、一份行动清单。

各环境原生命令是**权威数据源**（不可重写），但存在三个结构性盲区：
1. **跨环境冲突** — 单环境不感知其他环境的存在（PATH 优先级、skill 漂移、变量覆盖）
2. **API 可达性** — 每个环境只看自己的链路，无法对比
3. **历史模式** — 原生命令无法访问历史会话中的已知问题模式

统一命令的价值 = **路由到正确原生命令** + **补充盲区检查** + **量化评分** + **优先级排序**

### MECE 分解（七维度，D7 为主机级）

| 维度 | 权重 | 数据来源 | 检查项示例 |
|------|------|----------|-----------|
| D1 安装健康 | 13% | 原生命令 + `which` | 版本一致性、重复安装、PATH 位置 |
| D2 配置完整 | 22% | 原生命令 + 静态扫描 | settings/config 语法、权限、废弃项 |
| D3 认证有效 | 18% | 原生命令 + 静态检查 | API key 存在性、OAuth 状态、token 过期 |
| D4 网络可达 | 14% | 脚本（跨环境） | base_url 可达性、代理健康、延迟 |
| D5 扩展状态 | 13% | 原生命令 + 脚本 | skills/hooks/MCP 注册、版本一致性 |
| D6 跨环境协调 | 10% | 脚本（跨环境） | PATH 冲突、skill 漂移、变量冗余 |
| D7 存储健康 | 10% | 脚本（storage_check，主机级） | 磁盘余量、缓存大户、编译产物、会话数据、trash 超期 |

> D7 是主机级维度，不属于任何环境：由 `storage_check.py` 单独评分，全局分 = 环境均分 × 0.9 + 存储分 × 0.1。`--env storage` 可单独只查存储。

### 量化思维

- **单一分数**：每个环境 0-100 健康分，全局加权平均
- **扣分制**：critical -25, warning -10, info -5，下限 0
- **维度雷达**：六维度独立评分，可视化短板
- **趋势追踪**：与历史分数对比，发现退化

## 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    入口层 (Entry)                        │
│  /env-master (Claude 斜杠) │ env-master CLI │ --format  │
└─────────────────────────┬───────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────┐
│                 编排层 (Orchestrator)                    │
│  1. 环境发现（哪些已安装）                               │
│  2. 并行调度原生命令 + 盲区脚本                          │
│  3. 汇总结果 → 评分 → 报告                              │
└──────┬──────────────┬──────────────┬────────────────────┘
       │              │              │
┌──────▼──────┐ ┌─────▼──────┐ ┌────▼───────────────────┐
│ 原生适配器   │ │ 盲区脚本   │ │ 评分引擎               │
│ Adapters    │ │ Scripts    │ │ Scoring Engine         │
│             │ │            │ │                        │
│ • claude    │ │ • network  │ │ • 维度扣分             │
│ • codex     │ │ • path     │ │ • 加权平均             │
│ • grok      │ │ • drift    │ │ • 趋势对比             │
│ • kimi      │ │ • history  │ │ • 优先级排序           │
│ • mimo      │ │ • storage  │ │ • 主机级存储并入       │
│ • deepseek  │ │  (D7 新增) │ │                        │
│ • cursor    │ │            │ │                        │
│ • aider     │ │            │ │                        │
└─────────────┘ └────────────┘ └────────────────────────┘
```

> storage_check.py 为 D7 新增模块：收集磁盘/缓存/编译产物/会话/trash 数据 → 生成可清理计划（cleanable_items）→ 评分。只读，不执行删除。

## 与现有技能的兼容性

| 现有技能 | 关系 | 改造方式 |
|----------|------|----------|
| `native-diag` | 被整合 | 变为 `env-master` 的薄 wrapper，内部调用统一引擎 |
| `env-doctor` | 被整合 | 调度逻辑上移到 `env-master`，脚本层保留复用 |
| `/doctor` | 被扩展 | 仍可直接调用，但 `/env-master` 是更全面的超集 |

## 输出格式

### JSON Schema（机器可读）

```json
{
  "schema_version": "1.0",
  "timestamp": "ISO8601",
  "global_score": 78,
  "global_status": "warn|ok|fail",
  "environments": [
    {
      "env": "claude",
      "label": "Claude Code",
      "installed": true,
      "version": "2.1.220",
      "score": 85,
      "status": "ok",
      "dimension_scores": {
        "install": 90, "config": 85, "auth": 95,
        "network": 80, "extensions": 85, "cross_env": 75
      },
      "issues": [
        {
          "severity": "warning",
          "dimension": "cross_env",
          "summary": "...",
          "remediation": "...",
          "check_id": "..."
        }
      ],
      "metrics": {}
    }
  ],
  "storage": {
    "score": 85,
    "issues": [...],
    "cleanable_items": [
      {
        "path": "...",
        "size_human": "18.6G",
        "category": "build|cache|session|download|trash",
        "risk": "safe|recompile|user_data",
        "action": "..."
      }
    ],
    "metrics": {
      "disk": {...},
      "total_cleanable_human": "19.4G",
      "cleanable_count": 2
    }
  },
  "cross_env_findings": [],
  "remediation_priority": []
}
```

### Human Report（终端可读）

```
╔══════════════════════════════════════════════════════════╗
║           env-master 统一环境自检报告                     ║
╠══════════════════════════════════════════════════════════╣
║  全局健康分: 78/100  [■■■■■■■■■■□□□]  WARN             ║
║  环境覆盖: 4/5 (Claude ✓  Codex ✓  Grok ⚠  Kimi ✓)      ║
╠══════════════════════════════════════════════════════════╣
║  D1 安装  ████████░░  80                                  ║
║  D2 配置  █████████░  85                                  ║
║  D3 认证  ██████████  95                                  ║
║  D4 网络  ■■■■■■■□□□  70  ← 短板                        ║
║  D5 扩展  ████████░░  80                                  ║
║  D6 协调  ■■■■■■■□□□  72                                  ║
╠══════════════════════════════════════════════════════════╣
║  CRITICAL (0)                                             ║
║  ───────────────────────────────────────────────────────  ║
║  WARNING (3)                                              ║
║  ⚠ [network]  api.longcat.chat 延迟 5.2s (>3s)           ║
║  ⚠ [cross]    claude 在 PATH 中出现 2 次                  ║
║  ⚠ [config]   settings.json 含废弃字段 'old_key'          ║
║  ───────────────────────────────────────────────────────  ║
╠══════════════════════════════════════════════════════════╣
║  优先修复:                                                ║
║  1. 清理 PATH 中重复的 claude (影响: 命令路由不确定性)    ║
║  2. 检查 api.longcat.chat 代理配置 (影响: 请求延迟)       ║
╚══════════════════════════════════════════════════════════╝
```

## 支持环境矩阵

| 环境 | 原生命令 | 输出格式 | 已验证 | 盲区 |
|------|----------|----------|--------|------|
| Claude Code | /doctor + /context + /hooks + /mcp | text (会话内) | ✅ | cross_env, network |
| Codex | codex doctor --json | structured_json | ✅ | cross_env, drift |
| Grok | grok inspect --json | structured_json | ✅ | cross_env, network |
| Kimi Code | kimi doctor config\|tui | text | ✅ | cross_env, network, install |
| MiMo Code | mimo debug config\|paths | text | ✅ | cross_env, network, auth |
| DeepSeek | deepseek --check (推断) | text | 推断 | 全部 |
| Cursor | cursor --check (推断) | text | 推断 | 全部 |
| Aider | --version + 配置文件 | text | 推断 | 全部 |

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 全部 ok（或仅 info） |
| 1 | 存在 warning |
| 2 | 存在 critical / fail |
| 3 | 执行异常（脚本错误） |
