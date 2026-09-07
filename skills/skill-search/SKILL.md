---
name: skill-search
description: |
  Research, create, improve, migrate, evaluate, package, install-check, govern, and publish agent skills. Core strength is skill prior-art search across curated vertical sources (GitHub official API, skills.sh, SkillsMP, official catalogs, package registries) with intent-shaped queries, cross-source fusion, and source verification before adoption. Use for new or existing skills, prior-art synthesis, routing/trigger boundaries, trigger or output evals, Skill IR, release gates, README preparation, and create-and-publish workflows. Exclude one-off summaries, translations, ordinary docs, non-skill package publishing, and tasks that should not become a skill.
metadata:
  version: "0.1.0"
---

# Skill Search

Build reusable agent skill packages, not long prompts. Search first, then create.

## Router Rules

- Route by frontmatter `description` first.
- Once selected, this skill is the single authoring authority. Do not also invoke a generic `skill-creator` unless the user explicitly requests comparison or this skill is unavailable.
- Methodology core (gates, description playbook, eval design, iteration): **skill-foundry**. When a create/refactor task needs method-level reasoning, read skill-foundry's references; skill-search owns the full lifecycle (prior-art, publishing, ecosystem search) on top of that method.
- Built-in prior-art discovery belongs to this skill. Do not install, load, or delegate to a separate discovery skill.
- Built-in publishing belongs to this skill. Do not require or invoke a separate publisher skill after this package is selected.
- Keep the package root `SKILL.md` to routing and the minimal workflow. Put judgment in `references/`, deterministic behavior in `scripts/`, regression cases in `evals/`, and evidence in `reports/`.
- A package has one discoverable root `SKILL.md`; embedded examples and fixtures use `SKILL.example.md` or `SKILL.fixture.md`.
- Do not turn one-off summaries, translations, explanations, or brainstorming into skills.
- Match the user's action: create/refactor/package requests may edit; audit/evaluate/diagnose-only requests remain read-only; publish only when explicitly requested.
- Default to concise Chinese-first hyphenated names with no more than three preferred hyphen parts.
- Keep skill-search neutral: no owner branding, no hardcoded personal identity, no mandatory copyright line.

## Three Gates（前置决策门）

动手前，三问全过才继续设计；任一为否，直接回答用户、不创建包：

1. **模型已经能做好这件事吗？** 能 → skill 是多余开销。
2. **用户会用它 5 次以上吗？** 一次性自动化 → 直接 prompt 或一次性脚本。
3. **这件事真的超出模型内置能力吗？** 给模型已懂的事情加指令，每次触发都浪费 context。

三问反向用于审计已有 skill（德鲁克三查）：没它会变差吗？会用 5 次以上吗？模型自己能做吗？细节：references/non-skill-decision-tree.md、methodology.md。

## Search Core

The search core is the differentiator of this skill. Before any new skill or substantial redesign, run prior-art research (see [Built-In Prior-Art Discovery](#built-in-prior-art-discovery)). It draws from curated vertical sources — not a general web engine — so results are skill-native, verifiable, and mergeable by canonical repository.

Source roles:

| Source | Best use | Metric meaning | Important limitation |
|---|---|---|---|
| GitHub official API | canonical coverage, stars/updated sort, topic filters | repository-native metadata | repository popularity is not skill quality |
| skills.sh | popularity anchor and install discovery | installs are ecosystem adoption telemetry | installs are not satisfaction or correctness |
| SkillsMP | broad coverage, multilingual and occupation discovery | `stars` are repository stars | independent index, duplicates/localizations, approximate totals |
| ClawHub | OpenClaw ecosystem coverage | downloads/installs/stars are telemetry | narrower ecosystem; suspicious entries flagged |
| Official catalogs / registries | first-party trust anchor (e.g. anthropics/skills, npm/pypi skill packages) | curated or published status | narrow scope by design |
| Local inventory / local-seek | installed-skill reuse and content signals | presence and content hits only | local suggestion, not external evidence |
| Session-digger usage | historical opportunity/gap signals | counts only, raw sessions never included | signal, not proof; needs manual review |

## Modes

- `Scaffold`: exploratory or personal; minimum useful files.
- `Production`: team reuse; README, interface, trigger eval, output contract, and install evidence.
- `Library`: shared infrastructure; Production plus Skill IR, portability, trust, and review cadence.
- `Governed`: public or high-trust; Library plus permission, rollback, secret, release, and claim gates.

Choose proportionally with references/operating-modes.md, gate-selection.md, and qa-ladder.md.

审计请求（体检 / token 太大 / 太慢 / 架构 / 重构 skill）路由到 **Audit Mode**：先诊断后修复，只修诊断支持的改动。入口：[Audit Mode](references/audit-mode.md)，深度材料在 `references/audit/`。审计是同一 lifecycle 的阶段，不维护第二套方法树。

## Built-In Prior-Art Discovery

Before a new skill or substantial redesign:

1. Derive 2–4 intent-shaped queries covering outcome, domain action, quality mechanism, and an adjacent synonym.
2. Prefer the unified runner; add `--decide` to emit the reuse/adapt/build/invent decision, evidence status, and handoff `SEEK_*` fields:

```bash
python3 scripts/research_prior_art.py "<query 1>" "<query 2>" --strict --decide --summary --output reports/prior-art-candidates.json
```

Its underlying catalog calls (for debugging a single channel):

```bash
npx --yes skills find "<query>"; python3 scripts/search_skillsmp.py "<query>" --limit 20 --sort stars
python3 scripts/search_github.py "<query>" --topic claude-skills --sort stars; python3 scripts/search_clawhub.py "<query>" --limit 15
```

3. Add local and historical signals when the environment provides them:

```bash
python3 scripts/research_prior_art.py "<query>" --local --digger --decide --summary
```

`--local` scans installed SKILL.md (local_meta) and skill content (local_body);
`--digger` reads session-digger usage signals. Both read-only, raw sessions
never included. Without them the decision stays `hypothesis`; a failed channel
is `missing_evidence`; only strong local/usage signals reach `validated`.
4. Keep metrics separate: skills.sh installs measure adoption; GitHub/SkillsMP stars belong to the source repository; ClawHub downloads measure its ecosystem; neither is a user rating or quality score.
5. Deduplicate by canonical GitHub repository and skill path. Collapse translations, mirrors, and obvious forks without adding metrics together.
6. Shortlist genuinely relevant popularity, trust, and complementary anchors. Inspect source `SKILL.md`, maintenance, license, permissions, security signals, and available rating evidence; never execute untrusted candidate code just to study it.
7. Synthesize `keep / adapt / reject / invent`. Map each adopted mechanism to the new package instead of collaging prose.
8. Record dated sources, metrics, dedup, rejections, and missing evidence in `reports/prior-art-research.md` (Production+).

If a catalog fails, continue with the other sources, record `missing evidence`, and lower the claim. Full method: [Prior-Art Research](references/prior-art-research.md).

## Generalization Gate

Before promoting one failure into a core rule:

1. restate it as a domain-neutral behavior
2. classify it as core mechanism, optional adapter, or eval-only fixture
3. promote only safety/factual/permission invariants or behavior repeated across unrelated domains
4. keep one-off details in fixtures or specialist references
5. rerun the original and unrelated boundary cases

Prefer intent fidelity, source fidelity, and decision rules over an expanding topic encyclopedia.

## Skill OS

1. `Intent`: recurring job, users, inputs, output, exclusions, standards, references.
2. `Skill IR`: platform-neutral meaning and evidence boundary.
3. `Package`: lean root instructions, interface, README, and earned resources.
4. `Eval`: trigger boundaries first; output/runtime/human eval when risk justifies it.
5. `Review`: package, context, trust, install, README, and public claims.
6. `Operate`: explicit feedback, failures, drift, and next-iteration proposals without raw private content.

## Compact Workflow

1. Decide whether the request deserves a reusable skill; otherwise answer directly and create no package.
2. Capture job, finished output, target users, inputs, exclusions, permissions, standards, existing assets, platforms, and publication intent.
3. Pass prior-art discovery or record why it is not applicable or missing evidence.
4. Pass the generalization gate for sample-driven core changes.
5. Choose the lightest valid mode.
6. Write the `description` early; run `evals/trigger_cases.json` before expanding structure.
7. Create only earned resources. Never create ceremonial directories or duplicate README/SKILL prose.
8. Export `reports/skill-ir.json` for Production+, public, or cross-platform packages.
9. Add output evals when correctness, safety, persuasion, or repeatability cannot be shown by trigger tests alone.
10. Keep mutations within the requested action boundary and preserve rollback for risky changes.
11. Validate package, unit tests, trigger behavior, context budget, secret/trust boundaries, and evidence claims.
12. Produce the creation handoff and clearly label missing evidence.
13. Publish only when requested — see [Self-Contained Publishing](references/publishing.md); never push directly to the default branch.

Core commands:

```bash
python3 scripts/validate_skill.py .
python3 scripts/export_skill_ir.py . --output reports/skill-ir.json
python3 scripts/trigger_eval.py . --cases evals/trigger_cases.json --output reports/trigger-eval.json
python3 scripts/release_check.py . --phase local --run-tests
python3 scripts/publish_skill.py /path/to/skill --dry-run
```

## Gate Ladder

- `Scaffold`: valid frontmatter, useful README hook, natural triggers, explicit exclusions.
- `Production`: Scaffold plus interface, trigger eval, output contract, troubleshooting, root isolation, and install verification.
- `Library`: Production plus Skill IR, portability, trust, review cadence, and evidence artifacts.
- `Governed`: Library plus permission/rollback boundary, secret scan, output or integrity-preserving human evidence, and public-claim guard.

Unavailable telemetry, provider runs, approval, install proof, or human review must remain `missing evidence`; planned work is not proof. See [Review And Release Gates](references/review-release-gates.md) and [Resource Boundary Spec](references/resource-boundaries.md).

## Output Contract

For package-producing requests, provide only what the selected mode earns:

1. working skill directory and trigger-aware root `SKILL.md`
2. aligned `agents/interface.yaml`
3. human-facing README for shared/public skills
4. trigger cases and generated trigger report for Production+
5. Skill IR, prior-art report, and creation handoff for Production+
6. optional references, scripts, output evals, reports, and manifest when they improve judgment, repeatability, or evidence
7. publish artifacts only when publishing was requested

The final creation handoff must name the **reference skills studied**, give **candidate-specific lessons**, explain deliberate rejections and original contributions, and label each highlight as **design advantage**, **validated advantage**, or **hypothesis**. Never claim global superiority without a fair comparison. Use [Creation Handoff](references/creation-handoff.md).

## Publish Flow

1. Treat README as a product page: value, install, natural examples, prerequisites, outputs, configuration, risks, and troubleshooting.
2. Audit without mutation when useful: `python3 scripts/publish_skill.py /path/to/skill --dry-run`.
3. Only after an explicit publish request, run `python3 scripts/publish_skill.py /path/to/skill`.
4. The bundled publisher prepares ISC LICENSE, README and profile assets; resolves skill/repository identity; blocks secrets and reused release versions; creates or reuses a GitHub repository; and publishes only through a feature branch and PR.
5. Merge is blocked by conflicts, failed/pending checks or requested changes. Successful publication creates `vX.Y.Z`, verifies `npx skills add --list`, performs an isolated install, and runs the published release gate.
6. Do not report publication complete until the remote default version, GitHub Release, discovery and clean installation are verified.

Detailed CLI and safety decisions: [Self-Contained Skill Publishing](references/publishing.md). README method: [GitHub README Playbook](references/github-readme-playbook.md). Operation method: [SkillOps Loop](references/skillops-loop.md).

## Defaults

- Prefer practical, concise, publishable Chinese output.
- Keep one creator authority and one root skill entrypoint.
- Preserve platform-neutral source plus minimal adapters.
- Public claims must match trigger, output, runtime, install, or human evidence actually present.
- Adopt proven patterns semantically instead of mirroring them wholesale; keep dated evidence in internal reports.

## Reference Map

- Decide: references/non-skill-decision-tree.md, methodology.md, operating-modes.md, gate-selection.md, qa-ladder.md
- Design: references/skill-engineering-method.md, skill-archetypes.md, intent-dialogue.md, description-playbook.md
- Prior Art: references/prior-art-research.md
- Evidence: references/eval-playbook.md, output-eval-method.md, skill-ir-method.md, governance.md, creation-handoff.md
- Audit: references/audit-mode.md, references/audit/ (diagnostic-framework, engineering-patterns, design-patterns, anti-patterns, quality-checklist, skill-patterns)
- Release: references/publishing.md, review-release-gates.md, github-readme-playbook.md, skillops-loop.md, resource-boundaries.md
