# `/usage` — 跨环境 token 可观测（SKILL.md 搬出）

> 逐字搬移，未做改写。只有用户问「花了多少钱 / 用了多少 token / 缓存命中率」时才需要读这一册。

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
