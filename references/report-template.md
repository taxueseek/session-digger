# Report Structure Reference

Use this as the shape for analysis output, not a fill-in-the-blanks form.
Adapt section depth to what's actually interesting in the data. Skip sections
that would be empty or trivial.

## Single-session report

```
## [session label / date / cwd]

**Format**: Claude Code JSONL | Grok | Kimi Code | Generic/unknown
**Turns**: N   **Duration**: ~Nm   **Tools called**: N total across N distinct tools
**Error rate**: N%   **Flags**: [list any flags from the stats engine]

### What happened
2-4 sentences: the task, the overall approach taken, outcome
(done / partially done / abandoned / blocked).

### Tool usage breakdown
Table or short list: tool name, call count, error count/rate. Only call out
tools with notable error rates or unusually high call counts — don't just
dump the raw JSON.

### Notable patterns
- Repeated failed approaches (same tool/error looping)
- Inefficient sequences (e.g., many small Edits where one larger edit would work)
- Good patterns worth repeating (efficient tool chaining, good error recovery)
- Anything flagged by the stats engine — explain WHY it matters, don't just
  restate the flag

### Reusable takeaways
Concrete, actionable: "when doing X, doing Y first avoided N failed attempts"
— not vague ("communication could be clearer").
```

## Batch / cross-session report

```
## Batch summary (N sessions)

### Aggregate stats
- Total tool calls, most-used tools across the batch
- Tools with highest error rates across the batch
- Longest / shortest sessions
- Flagged sessions count and breakdown

### Cross-session patterns
Things that show up repeatedly across multiple sessions — recurring failure
modes, recurring effective strategies. This is the most valuable section for
"what should change going forward."

### Trend direction (if longitudinal data available)
- Is the error rate improving, worsening, or stable?
- Are retry loops becoming more or less frequent?
- Which tools are regressing vs improving?

### Files/sessions needing attention
- Any sessions with unusually high error rates
- Any patterns that justify a skill/workflow change (see skill-gap-finder)
```

## Trend report

```
## Trend analysis (unit: week/month, lookback: N periods)

### Direction
Lead with the direction of change and whether it's meaningful:
"Error rate is improving (7.4% → 4.2% over 3 months)" or
"Error rate is stable at ~7% across 8 weeks — no meaningful trend."

### Per-period breakdown
Concise table: period, session count, tool calls, error rate, delta vs previous.

### Notable tool-level changes
- Tools with regressing error rates (getting worse)
- Tools with improving error rates (getting better)
- New tools appearing in recent periods

### By-project / by-tag comparison (if requested)
Which projects or tags have notably different stats from the overall average.
```

## Skill-gap report

```
## Skill improvement proposals (N patterns found across N sessions)

### Summary
- Sessions analyzed: N
- Patterns found: N (after min-occurrences filter)
- Skills scanned: N

### Proposals (sorted by evidence count)
For each proposal:
1. **Problem**: What's the recurring pain point?
2. **Evidence**: How many sessions? Which ones? (list session IDs/paths)
3. **Matched skill**: Which installed skill plausibly covers this?
   (match confidence: low/medium/high — treat as a pointer, not certainty)
4. **Suggested rule**: A concrete paragraph to add to the skill's SKILL.md
5. **Action**: Present to user for y/n/edit review — never auto-apply

### Note
Only patterns appearing in at least N separate sessions are reported.
One-off bad sessions are noise, not actionable signal.
```

## Tone/quality bar

- Don't pad with restated numbers already visible in a table.
- Prioritize the 2-3 most useful observations over an exhaustive list.
- If stats and the actual conversation content seem to disagree (e.g., high
  tool "error" rate but the task clearly succeeded — maybe errors were
  expected/handled), say so rather than reporting the raw number as if it's
  automatically bad.
- When data is thin, say that plainly. Don't paper over it with a
  confident-sounding narrative.
