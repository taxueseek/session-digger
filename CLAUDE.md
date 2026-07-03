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
- `/recall` — Search and analyze past sessions (auto-uses FTS index after `/index`)
- `/recap` — Summarize recent sessions
- `/timeline` — Chronological project history (sessions + git)
- `/lessons` — Extract lessons learned
- `/dashboard` — Global memory overview and staleness alerts
- `/audit` — Memory staleness audit (heuristic or deep)
- `/extract` — Extract knowledge from conversation sessions
- `/prune` — Interactive memory cleanup
- `/analyze` — Analyze sessions for patterns (retry loops, errors, corrections)
- `/apply` — Interactive rule approval and persistence (y/n/e/a/q review)
- `/topics` — Topic segmentation and boundary detection
- `/index` — Build/update SQLite FTS search index
- `/import` — Import external conversations (WeChat, JSON, CSV, transcript)
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

**Core library:**
- `echolib.py` — Core Python parsing module (no pip dependencies)

**Unified engine (v0.7):**
- `sd-recall.py` — Unified single-process recall engine. Replaces bash pipeline (list-sessions + extract-messages + extract-tools). Uses SQLite FTS index when available, falls back to file scan. Supports `search`, `sessions`, `stats` subcommands.
- `index-builder.py` — Build/update SQLite FTS5 index. Subcommands: `build` (incremental), `search` (FTS query), `detail` (session metadata), `stats` (index overview). Stores in `~/.claude/.session-digger/index.db`.
- `dialog-adapter.py` — Import external conversation formats (WeChat export, generic JSON/CSV, plaintext transcripts) into session-digger's JSONL schema. Auto-detects format.
- `topic-segmenter.py` — Topic boundary detection via time-gap + content-similarity heuristics. Outputs labeled segments with keywords.

**Per-command scripts:**
- `analyze-session.sh` — Pattern analysis: retry loops, errors, user corrections. Invoked by `/analyze`.
- `apply-rules.sh` — Format, review, and persist candidate rules. Invoked by `/apply`.
- `post-compaction-hook.sh` — Claude Code PreToolUse hook: detect compaction and remind about context recovery tools.

**Replaced by `sd-recall.py` subcommands:**
- The old shell scripts (`list-sessions.sh`, `extract-messages.sh`, `extract-tools.sh`, `session-stats.sh`, `parse-jsonl.sh`, etc.) have been removed in v0.6.0.
- Their functionality lives in `sd-recall.py` subcommands:
  `sessions`, `search`, `stats`, `session-stats`, `messages`, `tools`, `files`, `schema`.
- `save-summary.sh` and `extract-knowledge.sh` have also been replaced:
  `sd-recall.py save-summary` and `sd-recall.py extract-knowledge`.
- `recall-lite.sh` — End-to-end no-API recall. Invoked by `/recall --lite` and runnable directly from a shell.

## Key Conventions

- **Index first**: Run `/index` once per environment, then use `sd-recall.py search` which auto-uses FTS.
- **`sd-recall.py` as primary engine**: New commands should use `sd-recall.py search` / `sessions` / `stats` instead of the bash pipeline. Faster (single process), same output.
- **Script-based parsing**: Use the provided scripts instead of ad-hoc grep/jq pipelines. `echolib.py` handles schema variations and noise filtering.
- **Grep tool is not bash**: In agent/skill docs, `Grep pattern=...` calls refer to the Claude Code Grep tool, not the bash `grep` command.
- **`${CLAUDE_PLUGIN_ROOT}`**: Resolves to this plugin's root directory at runtime. When undefined (global skill installation, not plugin mode), use the `SD_ROOT` preamble pattern: `SD_ROOT="${CLAUDE_PLUGIN_ROOT:-}"; [[ -z "$SD_ROOT" ]] && SD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"`.
- **Cache side effect**: `build-index.sh` / `build_fallback_index()` writes `.session-digger-index.json` inside `~/.claude/projects/<dir>/`. This is excluded from the plugin repo via `.gitignore`.
- **SQLite index**: `~/.claude/.session-digger/index.db` — auto-maintained by `index-builder.py`. Mtime-based incremental updates: only changed files are re-parsed.
- **`--decisions` mode**: When user asks "why did we..." or "decisions about X", add `--decisions` to recall. Returns 60-80% fewer tokens by only surfacing decision-point messages.
- **Lite mode**: Slash commands cost a model turn by definition — that's the contract. When users hit billing/tier errors or want raw evidence, route them to lite mode: either `/recall --lite` (one cheap turn, raw script output, no synthesis) or `scripts/recall-lite.sh` from a shell (zero API calls). Do not pretend a slash command can be made API-free.
- **Empty TSV fields**: When parsing tab-separated rows from `list-sessions.sh` in bash, do not use `IFS=$'\t' read` directly — bash collapses consecutive tabs because tab is whitespace IFS, which corrupts rows where SUMMARY (or any other field) is empty. Translate tabs to a non-whitespace delimiter first (`tr '\t' $'\x1f'`, then `IFS=$'\x1f' read`). See `recall-lite.sh` for the pattern.

## Speed Tier (v0.7)

```
1. /index (first time, ~5-30s one-time)
   └─> sd-recall search → <50ms (FTS index hit)
       └─> decisions mode → 60-80% token reduction

2. /topics → topic-segmenter.py (single pass, O(n) per session)

3. /import → dialog-adapter.py writes JSONL to _imported/
   └─> /index picks it up automatically
```

## Prerequisites

- Python 3.6+ (stdlib only, no pip packages)
- bash
- git (optional, needed only for git-mining features and git-based agents)
- curl (optional, needed only for URL validation in /audit --deep)
