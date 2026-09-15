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
