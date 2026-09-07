# Trigger And Eval Playbook

How to test whether a skill actually works before shipping it. The smoke test (one with-skill vs one baseline subagent) always comes first; the full pipeline is for skills that need real evidence.

## Table of contents

1. Trigger-first checks
2. Why paired testing
3. Test-case design (7:2:1)
4. The paired subagent flow
5. Grading and benchmark aggregation
6. Result analysis
7. Maintenance and iteration

--------

## 1. Trigger-first checks

Start with the trigger boundary before anything else:

- Would the user naturally say this? Does `description` contain the key terms?
- Does it misfire on neighboring skills? Are there should-trigger, should-not-trigger, and near-neighbor cases?

```bash
python3 scripts/trigger_eval.py . --cases evals/trigger_cases.json --output reports/trigger-eval.json
```

Then check output quality and maintainability (below).

## 2. Why paired testing

A skill that makes the agent produce good output is not obviously better than the baseline — the model might already do fine. You need a **with-skill vs without-skill** comparison on the same prompts to see the delta. If the delta is zero on most prompts, the skill is violating Gate 1 (the model already does this).

This is "forward testing": launch subagents that don't know they're being tested, on real task prompts (never meta-prompts like "test this skill"). Fresh context per run, real artifacts, clean up between iterations.

### Smoke test (do this first, always)

Spawn two subagents on one realistic prompt — one **with** the skill content, one **without**. Don't tell either it's being tested. Verify the with-skill output differs from baseline. If it doesn't, the skill is adding nothing and you've found a Gate-1 violation.

## 3. Test-case design (7:2:1)

Write 10–20 test prompts in this ratio:

- **70% common** — the realistic everyday use. Casual phrasing, typos, missing context. "ok my boss sent me this xlsx and wants a profit margin column, revenue is col C costs col D I think"
- **20% edge** — boundary conditions. Empty input, huge input, ambiguous phrasing, missing files.
- **10% anomalous** — hostile or broken input. Malformed files, contradictory instructions, requests outside the skill's scope (to test exclusions).

Each case specifies three things:

```json
{
  "id": 0,
  "name": "messy-csv-to-xlsx",
  "prompt": "the realistic user message",
  "expectations": ["output includes X", "output does Y", "does NOT do Z"],
  "should_trigger": true
}
```

Include `should_trigger: false` cases (near-misses that should NOT fire the skill) — these test your exclusions.

Bad test prompt: "Format this data." (too clean, no model would fail this).
Good test prompt: "so this csv my intern made is a mess, headers are in row 3 not row 1, can you just... fix it and make it an xlsx? also it's like 50k rows if that matters" (real messiness).

Share the test set with the user before running: "Here are the cases I want to try — anything to add or change?"

## 4. The paired subagent flow

For each test case, run two subagents on the same prompt:

**With-skill:**

```
Follow these instructions:

## Skill: <name>
<paste full SKILL.md content>

## Task
<the test prompt>

Solve the task using the skill's instructions.
Save outputs to: <workspace>/iter-N/eval-<name>/with_skill/outputs/
```

**Baseline (without-skill):**

```
Solve this task.

<the same test prompt>

Save outputs to: <workspace>/iter-N/eval-<name>/without_skill/outputs/
```

Run them concurrently. Do NOT tell either subagent it is being tested — that contaminates behavior. Use fresh context per run. Batch 2–3 cases at a time (4–6 concurrent subagents) to balance throughput against rate limits.

### On-disk layout (per iteration)

```
iter-N/
├── eval-<name>/
│   ├── eval_metadata.json      # id, name, prompt, expectations
│   ├── with_skill/
│   │   ├── outputs/            # the skill's produced files
│   │   └── timing.json         # tokens + duration
│   ├── without_skill/
│   │   ├── outputs/            # baseline's produced files
│   │   └── timing.json
│   └── grading.json            # produced by the grader
└── benchmark.json              # produced by aggregation
```

The `outputs/` subdirectory is the contract — every run directory must contain one, even if it holds a single file.

## 5. Grading and benchmark aggregation

**Persist outputs yourself** — subagents are unreliable about writing files.

Grade each case against its expectations. Per case:

```json
{
  "case_id": 0,
  "with_skill": {"pass": true, "notes": ""},
  "baseline": {"pass": false, "notes": "missed exclusion"},
  "delta": "with_skill_better"
}
```

Then aggregate into a benchmark table: pass-rate with skill vs baseline, per-case delta, and token cost. `scripts/aggregate_benchmark.py` turns per-case grading JSON into the benchmark table.

Targets: routing accuracy > 90%, output usability > 80%, redundant tokens < 20%.

## 6. Result analysis

Classify each case:

- **Non-discriminating** — passes both with and without the skill. Either the skill's job is easy, or it adds nothing; check Gate 1.
- **Flaky** — high variance across runs. Look for nondeterministic instructions or missing prerequisites.
- **Broken** — fails both. Fix the body, not the test.

## 7. Maintenance and iteration

- **Generalize from feedback.** When output is wrong, don't patch that one case — find the violated principle and encode the principle. Targeted fixes overfit.
- **Keep it lean.** If the model wasted tokens on busywork the skill encouraged, delete the offending guidance and observe.
- **Reframe, don't enforce.** If you're reaching for ALL-CAPS MUST/NEVER, the rule probably needs better reasoning, not louder volume.
- **Bundle repeated work.** If every test run reinvents the same helper script, move it to `scripts/` and point at it.
