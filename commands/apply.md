---
name: apply
description: |
  Review candidate rules from /analyze and persist them to CLAUDE.md or memory.
  Triggers: "apply rules", "implement suggestions", "write rules to CLAUDE.md", "应用规则", "采纳建议".
argument-hint: [<session-id>|--all] [--target CLAUDE.md|memory|docs]
allowed-tools: Bash, Read, Edit, AskUserQuestion
---

Review candidate rules produced by `/analyze` and persist approved ones.

Arguments: $ARGUMENTS

**Script path discovery:**
```bash
SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
```

## Step 1: Generate candidate rules

Run `/analyze` first (or re-run it now) to produce the candidate rule set:

```bash
bash $SD_ROOT/scripts/analyze-session.sh <session.jsonl> --format json
```

If the user ran `/analyze` earlier in this conversation, reuse those results instead of re-running.

Extract the `suggestions` array from the JSON output. Each suggestion has: `rule`, `evidence`, `target`, `category`.

## Step 2: Interactive review

For each candidate rule, present it to the user with its evidence and ask for a decision:

```
---
Rule (1/N): [retry] When Bash fails 3 times in a row, switch approach.
  Evidence: Retried 4x — npm install && npm run build

[y] approve  [n] skip  [e] edit  [a] approve all  [q] quit
> 
```

Use the AskUserQuestion tool for each rule. Options:
- **approve** — Add to target file
- **skip** — Discard this rule
- **edit** — Show the rule text, ask for revised text, then approve
- **approve all** — Approve this and all remaining rules without further prompting
- **quit** — Stop reviewing

## Step 3: Write approved rules

Determined by `$ARGUMENTS`:
- `--target CLAUDE.md` (default) — Append to the project's `CLAUDE.md`
- `--target memory` — Create a new `.md` file in the project's `memory/` directory with frontmatter
- `--target docs` — Write to `docs/lessons-learned.md`

### CLAUDE.md format (section-based append)

Check if `CLAUDE.md` exists. If not, create it with a heading.

Append under a `## Session Learned Rules` section (create the heading if missing):

```markdown
## Session Learned Rules

<!-- session-digger:apply $(date +%Y-%m-%d) -->

- When Bash fails 3 times in a row, switch approach. Do not keep retrying the same command.
- [category] rule text...
```

### Memory file format

Create `memory/lessons-YYYYMMDD.md` with frontmatter:

```markdown
---
name: "Session lessons (YYYY-MM-DD)"
description: "Auto-extracted rules from session analysis: <session-id>"
type: feedback
---

- When Bash fails 3 times in a row, switch approach.
- [correction] rule text...
```

Then ensure `MEMORY.md` includes the new file (append `![[lessons-YYYYMMDD.md]]` if not present).

## Step 4: Report

Summarize actions taken:
- N rules approved and written
- N rules skipped
- N rules edited
- Target file path

Do **not** show the full file content — just confirm the write succeeded and give the path.
