# P1 measurement: session retrieval footprint

## Problem redefinition

For session analysis, the expensive path is potentially repeated parsing, broad retrieval, duplicate evidence, and unnecessary context expansion. Measure these separately before optimizing retrieval or prompt construction.

## Hypothesis

Long sessions and repeated topics may cause the same material to be parsed, retrieved, or expanded more than once. The relevant P1 target is repeated work that does not increase evidence coverage or answer quality.

## Fixed corpus

Include short and long sessions, repeated topics, noisy tool traces, missing metadata, multilingual text, and near-duplicate messages.

Record:

- ingestion time
- parsing time
- retrieval time
- candidate count
- duplicate/near-duplicate candidate rate
- evidence coverage and recall/precision where measurable
- final context size
- peak memory
- end-to-end latency

## Ablation

Compare baseline against one simplification at a time: reuse parsed representations, deduplicate retrieval candidates, or cap redundant context expansion. Verify that evidence coverage and analysis quality do not regress.

## P1 gate

Optimize only a stage that is material to end-to-end cost. A lower candidate count is not sufficient if relevant evidence is lost.

This PR is measurement-only and preserves current behavior.

## 实施落点（2026-09-16）

- 测量实现：`scripts/footprint.py`（九项指标全落地；检索段量的是生产兜底路径——注册表文件扫描 + 50KB 头窗口）
- **首轮量化发现（P1 机会）**：50KB 头窗口外的深埋证据召回为 0（语料 s2-long 实测），近重复副本未去重（dup_rate 0.667）。两者均已冻结进 `tests/test_quality_baseline.py` 基线包络，任何优化须按 P1 规则单变量消融后显式重录。

## P1 消融结果（2026-09-16，单变量两轮）

**消融 A — 流式全文检索（采纳）**：`retrieval_utils.stream_contains` 替换 50KB 头窗口单次读——头窗口快路语义不变，未命中后按字节流式扫描剩余内容（每文件上限 2MB、跨块缝重叠 carry、命中即退）。接入 `sd-recall.find_sessions` 兜底路径。实测：recall 0.9 → **1.0**（深埋证据找回），retrieval_ms 2.52 → 2.97（+0.45ms），e2e 6.5 → 6.9ms，峰值内存 0.43 → 0.6MB。证据覆盖零回退。

**消融 B — 近重复折叠（部分拒绝）**：keep-longest 启发式（同前缀签名组保最大文件）**被门禁拒绝**——语料 s7 实测原件（含证据标记）比填充副本更小，折叠把原件证据丢了，触发 PR#1 拒绝准则（降候选数但降证据覆盖 = 拒绝）。收敛为 `collapse_near_dups` 仅折叠**字节级完全相同**的真拷贝（构造上零证据风险）；伪重复（同前缀、各自有增量）不折叠，duplicate_rate 残留 0.667 作为记录在案的已知状态。拒绝逻辑用回归测试钉死（`tests/test_retrieval_p1.py::test_keep_longest_heuristic_stays_banned`）。

消融后基线包络已重录（recall_mean 冻结 1.0，允许未命中清空）。
