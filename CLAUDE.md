# Session Digger — Plugin Development Guide

## Purpose

Session Digger mines Claude Code session JSONL files for insights. It helps users recall past work, trace decisions, learn from mistakes, and understand file histories by analyzing `~/.claude/projects/` conversation data.

## Architecture

```
commands/  → User-facing slash commands (entry points)
agents/    → Task-specific agents dispatched by commands via the Task tool
skills/    → Reusable knowledge (parsing rules, git patterns, synthesis taxonomy)
scripts/   → Python/bash tools that do the actual JSONL parsing and extraction
```

**Flow:** Commands dispatch to agents. Agents use skills for domain knowledge and call scripts (via Bash tool) for data extraction. Scripts are thin bash wrappers around `scripts/echolib.py`.

### Commands
- `/recall` — Search and analyze past sessions
- `/recap` — Summarize recent sessions
- `/timeline` — Chronological project history (sessions + git)
- `/lessons` — Extract lessons learned
- `/dashboard` — Global memory overview and staleness alerts
- `/audit` — Memory staleness audit (heuristic or deep)
- `/extract` — Extract knowledge from conversation sessions
- `/prune` — Interactive memory cleanup
- `/save-summary` — Save analysis result as cached summary for future recall

### Agents
- `recall` — Unified search: session finding, decision archaeology, mistake hunting
- `file-historian` — Trace a file's history across sessions and git
- `analyze` — Deep analysis of specific sessions
- `schema-scout` — Detect JSONL schema changes
- `memory-auditor` — Deep content-aware memory verification

### Skills
- `jsonl-core` — Canonical JSONL parsing infrastructure and record type reference
- `git-mining` — Git log/blame/diff patterns for correlating commits with sessions
- `experience-synthesis` — Taxonomy for categorizing insights (decisions, mistakes, patterns)
- `memory-management` — Memory format, staleness scoring, and routing knowledge

### Scripts
All in `scripts/`, require only Python 3.6+ (stdlib only) and bash. Git scripts additionally require git.
- `echolib.py` — Core Python parsing module (no pip dependencies)
- `list-sessions.sh` — Index-based session listing with grep/limit
- `session-stats.sh` — Single-pass session statistics
- `extract-messages.sh` — Human-readable message extraction
- `extract-tools.sh` — Tool call extraction with error filtering
- `extract-files-changed.sh` — Files edited in a session
- `parse-jsonl.sh` — Low-level JSONL parser with schema detection
- `build-index.sh` — Build fallback index for projects without sessions-index.json
- `git-context.sh` / `git-sessions.sh` — Git history helpers
- `memory-dashboard.sh` — Memory overview and heuristic audit output
- `extract-knowledge.sh` — Two-pass knowledge extraction from sessions
- `recall-lite.sh` — End-to-end no-API recall: list-sessions + extract-messages + extract-tools, dumped raw. Wraps the others. Invoked by `/recall --lite` and runnable directly from a shell.
- `save-summary.sh` — Save analysis result as `.summary.jsonl` for cache-first recall. Invoked by `/save-summary`.
- `summary-index.sh` — Archive index management for cross-environment summary scanning.
- `benchmark.py` — Performance benchmarking for echolib parsing functions. Run with `python3 scripts/benchmark.py` to measure session-stats throughput.

## Key Conventions

- **Index first**: Always query `list-sessions.sh` before opening raw `.jsonl` files.
- **Script-based parsing**: Use the provided scripts instead of ad-hoc grep/jq pipelines. `echolib.py` handles schema variations and noise filtering.
- **Grep tool is not bash**: In agent/skill docs, `Grep pattern=...` calls refer to the Claude Code Grep tool, not the bash `grep` command.
- **`${CLAUDE_PLUGIN_ROOT}`**: Resolves to this plugin's root directory at runtime. When undefined (global skill installation, not plugin mode), use the `SD_ROOT` preamble pattern: `SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"; [[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"`.
- **Cache side effect**: `build-index.sh` / `build_fallback_index()` writes `.session-digger-index.json` inside `~/.claude/projects/<dir>/`. This is excluded from the plugin repo via `.gitignore`.
- **Lite mode**: Slash commands cost a model turn by definition — that's the contract. When users hit billing/tier errors or want raw evidence, route them to lite mode: either `/recall --lite` (one cheap turn, raw script output, no synthesis) or `scripts/recall-lite.sh` from a shell (zero API calls). Do not pretend a slash command can be made API-free.
- **Empty TSV fields**: When parsing tab-separated rows from `list-sessions.sh` in bash, do not use `IFS=$'\t' read` directly — bash collapses consecutive tabs because tab is whitespace IFS, which corrupts rows where SUMMARY (or any other field) is empty. Translate tabs to a non-whitespace delimiter first (`tr '\t' $'\x1f'`, then `IFS=$'\x1f' read`). See `recall-lite.sh` for the pattern.

## Prerequisites

- Python 3.6+ (stdlib only, no pip packages)
- bash
- git (optional, needed only for git-mining features and git-based agents)
- curl (optional, needed only for URL validation in /audit --deep)
