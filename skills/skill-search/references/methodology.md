# Methodology

Judgment rules behind the gates, tiers, and classifications used in `SKILL.md`. Read this when a gate or tier decision is non-obvious.

## Table of contents

1. Three Gates — why and when they veto
2. Complexity tiers and the 500-line signal
3. Freedom Level — matching specificity to fragility
4. Problem Classification — diagnosing a sick skill
5. Token-efficiency audit
6. Skill types and their required sections

--------

## 1. Three Gates — why and when they veto

Before any design work, all three must pass. If any is no, stop.

1. **Does the model already do this well enough?** If yes, the skill is overhead.
2. **Will the user run it 5+ times?** One-shot automations belong in a direct prompt or throwaway script.
3. **Is this genuinely outside the model's built-in capability?** Adding instructions for something the model already knows wastes context every trigger.

Rationale: these gates filter out the majority of "should I make a skill?" impulses. When the answer is yes-all-three, you have a real skill on your hands.

The same three questions apply in reverse when auditing an existing skill ("Drucker's three checks"):

| # | Question | If no |
|---|----------|-------|
| 1 | Does the result get worse without this skill? | delete, do not fix |
| 2 | Will the user run it 5+ times? | one-off, not worth optimizing |
| 3 | Can the model do it natively? | no skill needed |

## 2. Complexity tiers and the 500-line signal

| Tier | SKILL.md size | Directories | When |
|------|---------------|-------------|------|
| Simple | < 150 lines | none | pure instructions, no code |
| Medium | 100–300 lines | `scripts/` or `references/` | needs a script or deep reference |
| Complex | 200–650 lines | multiple subdirs | multiple workflows, cross-cutting |

**Start Simple, always.** It is far easier to add complexity when a real need appears than to remove it after the skill has accreted cruft. Most skills ship as Simple and stay there.

Tier tells you the structure, not the importance. A Simple skill that does one thing well is more valuable than a Complex skill that does ten things poorly.

When a Simple skill keeps growing because you keep adding "just one more case," that's a signal to either (a) split into a Medium with `references/`, or (b) question whether the new cases are real (Gate 1) or one-offs (Gate 2).

The 500-line SKILL.md limit is a hard signal: at 500 lines, progressive disclosure is mandatory, not optional. At 800 lines, the skill is a dumping ground and needs surgery.

## 3. Freedom Level — matching specificity to fragility

Match how prescriptive the skill is to how fragile the task is:

- **High freedom** (text instructions, many valid approaches): for creative or judgment-heavy tasks where multiple correct approaches exist. "Write a commit message that explains why the change was made" — don't script this.
- **Medium freedom** (parameterized scripts, preferred pattern): when a preferred pattern exists but parameters vary. "Run the deploy script with the env and service name."
- **Low freedom** (specific scripts, few parameters): for fragile operations where consistency is critical. PDF rotation, date formatting across timezones, anything where "the model's interpretation" introduces bugs.

The model explores a path: a narrow bridge over a cliff needs guardrails (low freedom); an open field allows many routes (high freedom).

Over-prescribing a high-freedom task makes the skill brittle and narrow. Under-prescribing a low-freedom task reintroduces the exact errors the skill was meant to prevent. When a skill is "sometimes wrong," the first diagnosis is whether freedom level matches fragility.

## 4. Problem Classification — diagnosing a sick skill

When a skill misbehaves, classify before treating. Mixed problems get functional fixes first.

| Class | Meaning | Signal | Diagnosis |
|-------|---------|--------|-----------|
| A — Functional | skill is broken | output is wrong or missing | quick scan of the body; check paths and logic |
| B — Efficiency | works but burns tokens | output is right but slow/expensive | quantitative token audit |
| C — Architecture | structural rot | hard to extend, changes ripple | deep diagnosis; often needs a split |
| D — Trigger | wrong trigger behavior | doesn't fire, or fires on wrong things | description calibration |

Priority within a class: **P0** blocking (skill is unusable) → **P1** noticeable (degrades quality) → **P2** nice-to-have (polish).

Always fix Class A before Class B — an efficient broken skill is still broken. Class D is the most common and the most self-inflicted: it almost always traces back to a description that recaps the workflow (the Description Trap, see `references/description-playbook.md`) or lacks trigger phrases.

For pure diagnosis without rewriting — "tell me what's wrong, don't fix it" — use **Audit Mode** (`references/audit-mode.md`). Route there rather than mixing diagnosis and rewriting in one pass.

## 5. Token-efficiency audit

For every skill improvement, quantify the token impact by categorizing each block of content:

- **Necessary** — required for the skill to function. Removing it breaks the skill.
- **Optional** — helpful in some cases but not every trigger. Candidate for `references/` (loaded on demand) instead of the body.
- **Redundant** — can be removed without affecting quality. The model already knows this; or it duplicates another section.

Target: redundant content < 20% of total token cost.

Audit method:

1. Walk the body section by section.
2. For each section: "if I delete this, does a test prompt's output get worse?" If no → redundant. If "only for case X" → optional. If "yes broadly" → necessary.
3. Move optional content to `references/` with a pointer from the body.
4. Delete redundant content outright.
5. Re-run the test prompts to confirm quality held.

## 6. Skill types and their required sections

Different skill types need different body structures. Pick the type before writing.

| Type | Focus | Must-have section | Example |
|------|-------|-------------------|---------|
| **Discipline** | rules the model must follow | rationalization table + Red Flags list | TDD, security review |
| **Technique** | how to do a specific thing | step-by-step + edge cases | PDF text extraction |
| **Pattern** | a mental model for decisions | when to apply + counter-examples | when to use subagents |
| **Reference** | API or doc lookup | searchable index + retrieval paths | BigQuery SQL patterns |

A discipline skill without a Red Flags list will be rationalized away. A technique skill without edge cases will fail on the second-most-common input. A reference skill without a searchable index wastes tokens loading the wrong section.

If a skill doesn't fit a type cleanly, it's probably two skills — split it.
