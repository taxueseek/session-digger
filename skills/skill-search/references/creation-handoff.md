# Creation Handoff

Use this structure after creating or materially redesigning a skill. Keep it concise enough to read in the final response, while preserving the fuller version in `reports/creation-handoff.md` for Production or higher.

## 0. Prior-art fields

When prior-art research ran, carry its decision into the handoff (also into any later audit handoff). The unified runner emits these fields directly with `--decide`:

```
python3 scripts/research_prior_art.py "<query>" --decide --summary
```

```
SEEK_ACTION: reuse|adapt|build
SEEK_TARGET: <path|name|url|->
SEEK_EVIDENCE: validated|hypothesis|missing_evidence|-
SEEK_CONFIDENCE: <low|medium|high>
SEEK_SUMMARY: <一句话 prior-art 结论>
```

- `reuse` → stop creating unless the user insists and states the delta.
- `adapt` → prefer editing the local target; catalog install/delete needs explicit user confirm.
- `build` → continue with the sections below.

Note: the script's evidence status is `hypothesis` when only catalog signals exist (local/digger channels not run, or no strong signal), `missing_evidence` when a requested channel actually failed, and `validated` only when a strong local reuse or covered-usage signal backs the decision.

## 1. Result

- skill name and version
- one-sentence job to be done
- local path and publication status

## 2. Reference skills studied

Name 2–4 genuinely relevant candidates. For each one include:

- skill and source link
- why it entered the shortlist
- dated adoption or trust signal, with metric semantics preserved
- concrete mechanism learned
- where that mechanism appears in the created skill

Do not list a candidate that was only seen in a search result but not inspected.

## 3. Absorbed and rejected

Summarize:

- `keep`: mechanism retained in principle
- `adapt`: mechanism changed for skill users, tools, language, or risk level
- `reject`: mechanism omitted and the concrete reason
- `invent`: original connection or capability created for this job

## 4. Advantages and highlights

Each advantage needs a label and evidence pointer:

| Label | Meaning | Allowed wording |
|---|---|---|
| `design advantage` | visible in the source/package and better aligned to the stated target job | “This design adds…” or “Compared with the inspected candidates, this package explicitly…” |
| `validated advantage` | supported by a named trigger, output, runtime, install, or human evaluation | “Passed 16/16 trigger cases…” |
| `hypothesis` | plausible but not yet proven | “Expected to help…, but provider-backed comparison is missing evidence.” |

Do not write “best,” “world-class,” “more accurate,” or “better than X” unless a fair comparison supports it. A checklist difference is not automatically an outcome advantage.

## 5. Verification and limits

Report:

- package validation
- trigger results
- output, runtime, or human evidence when available
- what remains `missing evidence`
- permissions or actions deliberately excluded

## Compact final-response template

```markdown
已创建：<skill> <version> — <one-line outcome>

参考学习
- <skill A>: 学习 <mechanism>; 落到 <artifact/section>。
- <skill B>: 学习 <mechanism>; 落到 <artifact/section>。

取舍与原创
- 保留：...
- 舍弃：...
- 原创：...

优势与证据
- [design advantage] ...
- [validated advantage] ...
- [hypothesis] ...（missing evidence）

验证：<results>
边界：<limitations>
```
