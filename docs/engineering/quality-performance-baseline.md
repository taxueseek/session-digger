# Session Analysis Quality and Performance Baseline

## Problem redefinition

Session analysis is useful only when retrieval and synthesis preserve the evidence needed to explain what happened. The engineering objective is:

> Maximize evidence-backed insight per unit of scan time, memory, and model context while minimizing missed or duplicated signals.

## MECE dimensions

1. **Ingestion**: parsing coverage, malformed records, ordering, and timestamp normalization.
2. **Retrieval**: relevant-session recall, duplicate suppression, filtering precision, and search latency.
3. **Analysis**: clustering, pattern detection, chronology, and evidence linkage.
4. **Output quality**: factual traceability, actionable findings, uncertainty handling, and omission rate.
5. **Resource efficiency**: scan time, memory, context size, and repeated parsing/computation.

## Fixed evaluation corpus

Keep representative sessions covering short/long histories, repeated topics, noisy tool traces, missing metadata, multilingual text, and near-duplicate events.

## Quantitative gates

Measure retrieval recall/precision where labels exist, duplicate rate, evidence coverage, end-to-end latency distribution, peak memory, context footprint, and analysis regression rate.

## P1 optimization gate

Before changing parsing, retrieval, or analysis, establish that the target dominates cost or failure rate. Then make one change, run an ablation, and verify evidence coverage and output correctness remain stable.

## Experiment loop

`fixed corpus -> baseline -> profile -> hypothesis -> single-variable change -> ablation -> evidence regression -> scaling test -> retain/revert`

## 实施落点（2026-09-16）

- 固定语料：`scripts/quality_corpus.py`（7 场景 14 文件，证据标记全埋点）
- 测量 harness：`scripts/footprint.py`（`--json` 出全指标）
- 质量门禁：`tests/test_quality_baseline.py`（基线包络冻结在 `ENVELOPE`，改动需显式重录）
