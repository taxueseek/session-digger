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

## 主会话 vs 子代理（session_role）

缓存波动常与 **auto / 多 agent** 有关，报表应能分列：

| `session_role` | 含义 |
|----------------|------|
| `main` | 主对话线程 |
| `subagent` | 子代理 / 子智能体独立会话 |
| `unknown` | 尚无法判定 |

| 环境 | 判定 |
|------|------|
| DimCode | id：`sess_*` → main，`subagent_*` → subagent |
| Grok | `summary.session_kind=subagent` 或出现在父目录 `subagents/*/meta.json` 的 child |
| Claude | 路径含 `/subagents/` → subagent（索引默认仍可只收录 main） |
| ZCode | `sess_subagent` / `task_type=subagent_child` |
| Kimi Code | `agents/main` vs `agents/agent-*` |

API：`classify_session_role`；`build_cache_hit_tables(..., split_role=True)` 或 `role_filter="main"`。

Grok 多 agent 还有 **rollup**（父 updates 含子女用量）风险：子会话若同时入库，应用 role 过滤或 `grok_family_usage_report` 做家族去重。

## 禁止再犯

- 把「全程无 cache 的大模型」并进环境加权/简单平均当「环境很差」  
- 默认输出双列「会话均 + 加权」却不解释  
- 适配器 live 有命中率、索引 null 却仍用索引下结论（先查 single-pass 门控）  
- 主会话与子代理混成一行环境均，却归因成「模型缓存波动」
