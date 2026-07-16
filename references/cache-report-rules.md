# 缓存命中率报表口径（强制）

面向用户的缓存表、reflect/trend 摘要、会话答疑，**一律遵守**。违反即视为报表错误。

## 主表只报什么

| 指标 | 是否进主表 | 说明 |
|------|------------|------|
| **缓存命中率**（会话算术平均） | **是** | 仅对「有效会话」：`0 < cache_hit_rate ≤ 1` |
| Token 加权命中率 | **否**（默认） | 接近成本体感，但易被当成账单；除非用户明确要求「按 token 加权」 |
| 账单金额 | 否 | 账单 = 各请求分项 token × 单价，不是命中率字段 |

公式（主表唯一默认）：

```text
cache_hit_rate_table = mean( rate_i  for sessions where rate_i > 0 )
```

## 必须排除、单独列表

下列进 **附录 / 排除清单**，**不得**拉低主表均命中率：

| 原因 | 典型例子 |
|------|----------|
| 无命中率字段 (`null`) | 解析失败、Cursor/Trae 未落 usage |
| 命中率 = 0 | Kimi Code × LongCat 有大量 input、`cache_read=0` |
| 模型未标注 | dimcode / kimi 等 model 空，仅可参与**环境**层平均，不进**模型×环境**主表 |
| 有效会话过少 | 默认 `< 3` 条有效会话的模型×环境组合 |
| **能力层不足** | 适配器 tier 低于账单级（T2+：如 Cursor/Trae/纯 SchemaProbe）——契约在但不可当账单 |

## Policy 表真源

`input_tokens` 是否含 cache **只**由 `echolib._policy.PROVIDER_POLICY[agent].input_includes_cache` 解释。

| API | 用途 |
|-----|------|
| `get_token_policy(agent)` | 取单环境口径 |
| `adapter_tier(agent)` / `tier_supports(agent, "usage")` | 深度门控 |
| `finalize_session_stats(stats, agent)` | 盖章 agent/tier + attach 命中率 |
| `build_cache_hit_tables(..., enforce_usage_tier=True)` | 主表默认剔除非账单级适配器 |

两 regime 公式：

```text
input_includes_cache=False  →  rate = cache_read / (input + cache_read)   # Claude / Kimi
input_includes_cache=True   →  rate = cache_read / input                 # Grok / ZCode / Codex
```

## 展示结构

1. **表 A：按环境** — 有效会话数、会话均命中率  
2. **表 B：模型 × 环境** — 有效会话 ≥ 3、已标注模型  
3. **表 C：排除清单** — 环境 / 模型 / 原因 / 会话数 / token（可选）  

## 实现入口

| API | 用途 |
|-----|------|
| `echolib.cache_rate_eligible(rate)` | 单会话是否进排名 |
| `echolib.mean_cache_hit_rate(rates)` | 会话均（无加权） |
| `echolib.build_cache_hit_tables(rows)` | 一次产出主表 + 排除表 |
| `filter_cache_models(..., require_cache=True)` | 按模型整组丢零缓存 |

索引层：WorkBuddy 等必须走适配器，禁止 single-pass 吞掉 `providerData.usage`。

## 主对话 vs 子代理（对用户怎么说）

缓存波动常与 **auto / 多 agent** 有关，报表应能分列。

### 对用户只允许出现

| 展示文案 | 含义 |
|----------|------|
| **主对话** | 用户与主 agent 的那条会话 |
| **子代理** | 子智能体独立跑的会话 |
| （可不展示） | 分不清时：不进「角色分列」表，或归入环境总表 |

### 禁止出现在用户表里

- 内部标记：`main` / `subagent` / `session_role` / `unknown`
- 文件路径、目录结构、`sess_*`、`/subagents/`、wire 路径等实现细节
- 「硬编码路径」式说明（判定逻辑写在代码注释/本文件，不进报表）

路径/id 前缀只用于**内部分类**（和读 usage 字段一样），不是业务展示字段。

### 代码（实现侧，非展示）

- 内部 token：`main` | `subagent` | `unknown`（索引列）
- 展示：`session_role_label()` → 主对话 / 子代理
- API：`build_cache_hit_tables(split_role=True)` 或 `role_filter="主对话"`

Grok 多 agent 还有 **rollup**（父用量含子女）风险：分列或家族报告去重，勿父子简单相加。

## 禁止再犯

- 把「全程无 cache 的大模型」并进环境加权/简单平均当「环境很差」  
- 默认输出双列「会话均 + 加权」却不解释  
- 适配器 live 有命中率、索引 null 却仍用索引下结论（先查 single-pass 门控）  
- 主会话与子代理混成一行环境均，却归因成「模型缓存波动」
