---
name: optimize
description: |
  Cross-session skill gap analysis. Mines the session index for recurring pain
  points (tool errors, retry loops, long conversations, project outliers) and
  drafts reviewable SKILL.md improvement proposals. Never auto-edits skill
  files — always produces a human-reviewable proposal with evidence.
  Triggers: "optimize skills", "skill gap", "什么该优化", "技能差距",
  "该不该改 skill", "重复出现的模式", "recurring patterns", "workflow issues".
argument-hint: [--min-occurrences N] [--skills-dir PATH] [--since DATE]
allowed-tools: Bash, Read, Task
---

Mine the session index for recurring pain points and draft skill improvement
proposals.

Arguments: $ARGUMENTS

**Script path discovery:**
```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
```

## Prerequisite

The index must be built first. If `~/.claude/.session-digger/index.db` doesn't
exist, run `/index` before `/optimize`.

## Execution

```bash
python3 $SD_ROOT/scripts/skill-gap-finder.py analyze \
  --min-occurrences ${MIN_OCCURRENCES:-3} \
  ${SKILLS_DIR:+--skills-dir "$SKILLS_DIR"} \
  ${SINCE:+--since "$SINCE"}
```

Parse $ARGUMENTS for:
- `--min-occurrences N` — minimum sessions a pattern must appear in (default: 3).
  Raise to 5+ for a stricter bar on a large index.
- `--skills-dir PATH` — directory to search for installed SKILL.md files.
  Defaults to `~/.claude/skills` and `~/.agents/skills`. Can be repeated.
- `--since DATE` — only consider sessions created on/after this ISO date.

## What it mines

1. **Recurring tool errors** — tools with >=25% error rate in at least N
   separate sessions (not just an aggregate rate dominated by one outlier).
2. **Recurring flags** — stats-engine flags (high error rate, long conversation,
   retry loop) that appear across multiple sessions with the same category.
3. **Project outliers** — projects whose average error rate is notably worse
   than the overall average (>=10pts higher AND >15% absolute).

## Output interpretation

Present proposals in this order:

1. **Summary** — sessions analyzed, patterns found, skills scanned
2. **Proposals** (sorted by evidence count, most recurring first):
   - **Problem** — what's the recurring pain point?
   - **Evidence** — how many sessions? Which ones? (session IDs + paths)
   - **Matched skill** — which installed skill plausibly covers this?
     (confidence: low/medium/high — treat as a pointer, not certainty)
   - **Suggested rule** — a concrete paragraph to add to a SKILL.md
   - **Action** — present to user for y/n/edit review

3. **Review each proposal** with the user:
   - Read the evidence sessions (at least spot-check 1-2) to confirm the root
     cause before recommending adoption
   - If the user agrees a proposal is worth adopting, use `str_replace` or the
     skill-creator workflow to edit the target SKILL.md
   - **Never apply a proposal automatically** — always get explicit user approval

## Tone

- If there's not enough data (fewer than min-occurrences sessions), say that
  plainly: "Only N sessions indexed — need at least M to distinguish a
  recurring pattern from noise. Keep using sessions and re-indexing."
- The `--min-occurrences` filter is the noise filter. A single bad session is
  noise; only recurring patterns justify a permanent rule change.
- Match confidence is a pointer to investigate, not a settled fact. Always
  sanity-check before treating a "high confidence" match as definitive.

## Architecture note

This is Layer 3 (DECISION) — the only layer that makes a judgment call ("this
is a real pattern worth acting on"). It always surfaces evidence + a
human-reviewable proposal rather than silently deciding for the user.

## Next step

- If user adopts a proposal → edit the target SKILL.md → suggest `/audit` to
  verify memory health
- If no patterns found → suggest running `/trend` to check if the situation is
  improving or worsening over time
- If too little data → suggest continued usage + periodic `/index` rebuilds
