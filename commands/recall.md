---
name: recall
description: |
  Search past conversations for a topic, decision, or mistake.
  Triggers: "recall", "remember when", "search past conversations", "上次讨论过什么", "之前做过什么", "回忆", "之前怎么做的".
  Pass --lite for raw, no-synthesis output (works on standard tier; cheapest API path).
argument-hint: <search-topic> [--scope current|all] [--limit N] [--lite]
---

Search past conversation sessions for information about: $ARGUMENTS

Supports Claude Code, Grok Build, and Kimi Code sessions. Use --agent to control scope.

If no search topic is provided, show the 10 most recent sessions for the current project as a summary list.

## Mode selection

Inspect $ARGUMENTS for `--lite`:

- **If `--lite` is present** → run lite mode below. Do not launch the recall agent. Do not synthesize.
- **Otherwise** → run full mode (the agent path).

### Lite mode (`--lite`)

The user has explicitly opted out of model synthesis. Goals: minimum tokens, raw evidence, no extra reasoning.

1. Strip `--lite` from $ARGUMENTS. Pass the remaining arguments to the shell script.
2. Run the script via the Bash tool. Use the most distinctive keyword from the user's query as the positional argument; pass `--scope` and `--limit` through if present.

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/recall-lite.sh <keyword> [--scope ...] [--limit ...]
```

3. Return the script's stdout to the user verbatim, wrapped in a single sentence at the top: "Lite mode — raw matches, no synthesis." Do not summarize, rank, interpret, or add commentary. The user is asking for the raw dump on purpose.

If the user's query is a question (e.g. "how did I import vitepress books"), pick the most distinctive content word as the keyword (e.g. `vitepress`). State your keyword choice in one line so the user can re-run with a different word if they want.

### Full mode (default)

Launch the `recall` agent via the Task tool with the following context:

- **Search topic**: $ARGUMENTS
- **Current project**: the current working directory
- **Default scope**: "current" project (use "all" if `--scope all` is specified)
- **Default limit**: 10 sessions
- **Agent**: auto-detected from session directory structure. Override with `--agent claude|grok|kimi_code|cross`

The recall agent will:
1. Search session indices (including fallback index for unindexed projects)
2. Deep-dive into the most relevant sessions
3. Determine focus from the query (session search, decision archaeology, or mistake hunting)
4. Present findings with dates, context, and excerpts

If the query mentions decisions, rationale, "why did we", or alternatives — focus on decision archaeology.
If the query mentions errors, mistakes, failures, "what went wrong" — focus on mistake hunting.
Otherwise, perform a general session search and summarize findings.

## When to suggest lite mode

If the user reports an API/billing error from a previous `/recall` invocation (e.g. "Extra usage is required for 1M context", "rate limit", "model unavailable"), tell them they can re-run with `--lite` to skip synthesis and get raw matches at minimum cost. Also point them to `${CLAUDE_PLUGIN_ROOT}/scripts/recall-lite.sh`, which they can run directly from a shell with zero API calls at all.
