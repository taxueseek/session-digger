# Description Playbook

The description field is the single highest-leverage piece of any skill. It is always in context, it is the only signal the model uses to decide whether to load the body, and a bad one makes a great skill invisible. Read this before writing or touching any description.

## Table of contents

1. The formula
2. Trigger phrases (5+)
3. The Description Trap (the #1 mistake)
4. Pushy but not too pushy
5. Exclusions
6. The discipline-skill Red Flags pattern

--------

## 1. The formula

```
[Action verb] + [value proposition]. Use when [trigger 1], [trigger 2], ...
or [symptom]. Covers [capability scope]. NOT for [exclusion].
```

- First sentence: what the skill does (capability), not how it does it.
- Then: the triggers — the user phrasings, file types, and contexts that should fire it.
- Then: scope (what it covers) so the model knows it's a fit.
- End with: what it does NOT do, to prevent false positives.

Good vs bad:

```
# BAD — vague, no triggers, no exclusions
description: Creates Word documents.

# GOOD — capability, triggers, scope, exclusion
description: >
  Create, read, and edit Word documents (.docx). Use when the user mentions
  "Word doc", ".docx", wants a report/memo/letter with tables of contents,
  page numbers, or letterheads, or needs find-and-replace, tracked changes,
  or image insertion in a document. NOT for PDFs, spreadsheets, or general coding.
```

## 2. Trigger phrases (5+)

The model under-triggers skills by default. Combat this by listing explicit phrases the user might actually type — including casual, imprecise, and typo-prone ones, not just technical terms.

Aim for 5+ trigger phrases mixing:

- **Precise**: ".docx", "tracked changes"
- **Fuzzy/casual**: "make me a Word doc", "fix the formatting"
- **Symptom**: "this document looks wrong", "the margins are off"

Bad trigger list (too few, too clinical): "create document", "edit document".
Good trigger list: "Word doc", ".docx", "report", "memo", "letterhead", "tracked changes", "fix the formatting", "page numbers", "table of contents".

## 3. The Description Trap (the #1 mistake)

**When the description summarizes the workflow, the model follows the description and skips the body.** The skill silently fails — it "triggers" but the body never gets read, so the actual instructions have zero effect.

```
# BAD — recaps the workflow
description: Use for TDD — write the test first, watch it fail, write the
minimal code to pass it, then refactor.

# GOOD — triggering conditions only
description: Use when implementing a feature or fixing a bug, before
writing any code.
```

Why this happens: the model reads the description, recognizes the workflow, and decides it already knows what to do. It then acts from the summary — which is necessarily lossy — instead of the carefully written body. The skill author is mystified why their detailed instructions have no effect.

**Rule: the description states WHEN, never HOW.** Put the how in the body. The description's job is to make the model open the body.

Self-check: read your description and ask "could a smart model execute the workflow from this alone?" If yes, you've trapped yourself — delete the how.

Run `scripts/lint_description.py` on a draft to flag Description Trap patterns.

## 4. Pushy but not too pushy

Models tend to under-trigger skills, so descriptions are written slightly "pushy" — explicitly listing scenarios, even ones that seem obvious. But over-pushy descriptions (listing every conceivable trigger) cause false positives and token bloat.

Calibration:

- **Too shy**: "Helps with documents." → never fires.
- **Right**: "Use when the user mentions .docx, Word, or wants a formatted report. Also fires on find-and-replace, tracked changes, or table-of-contents requests." → fires on real demand.
- **Too pushy**: "Use for anything text-related, writing, documents, files, or content." → fires on everything, including things it shouldn't.

The exclusion clause is what lets you be pushy safely: list generously, then carve out the false positives with "NOT for...".

## 5. Exclusions

End with explicit exclusions. These prevent two failure modes:

1. **False positives** — the skill fires on adjacent requests it can't handle.
2. **Routing collisions** — two skills both fire and the model picks arbitrarily.

Pattern: NOT for [adjacent thing A], [adjacent thing B], or [thing handled by skill Y].

When you have a skill family, use exclusions to route between members: each skill's description ends with NOT for clauses naming what the neighboring skill handles. The exclusions do the routing — no central orchestrator needed.

## 6. The discipline-skill Red Flags pattern

Discipline skills (TDD, security review, mandatory checklists) fail because the model rationalizes skipping them. A description alone won't fix this; the skill body needs a Red Flags list the model scans before acting. But the description sets up the discipline frame:

```
# Discipline description — sets the frame
description: >
  Enforce test-first development. Use BEFORE writing any implementation code,
  whenever the user asks to implement a feature, fix a bug, or add functionality.
  Fires on "let's build", "add a function", "implement", "fix this", even when
  the user says "just do it quickly" or "skip the tests". NOT for pure refactors
  with no behavior change, or docs/config-only changes.
```

Then the body carries the rationalization table and the Red Flags self-check (see `references/methodology.md`). The description's job is to make the discipline fire even when the model would rather skip it — note the explicit "even when the user says skip the tests" trigger.
