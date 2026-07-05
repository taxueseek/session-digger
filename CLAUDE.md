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
- `/trend` — Longitudinal trend analysis (week/month-over-month, by-theme, regressions)
- `/optimize` — Cross-session skill gap analysis → SKILL.md improvement proposals

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
- `index-builder.py` — Build/update SQLite FTS5 index. v1.1: now stores rich stats (tool_usage_json, tool_errors_json, flags_json, duration_seconds, project_name, tags, outcome) for trend/skill-gap analysis. Subcommands: `build` (incremental), `search` (FTS query), `detail` (session metadata), `stats` (index overview).
- `dialog-adapter.py` — Import external conversation formats (WeChat export, generic JSON/CSV, plaintext transcripts) into session-digger's JSONL schema. Auto-detects format.
- `topic-segmenter.py` — Topic boundary detection via time-gap + content-similarity heuristics. Outputs labeled segments with keywords.

**Trend & skill-gap analysis (v0.8):**
- `trend-engine.py` — Longitudinal analysis over the SQLite index. Three modes: `period-over-period` (week/month comparison with deltas), `by-theme` (aggregate by project/tag/agent), `regressions` (detect tools getting worse). Reads ONLY the index, never raw transcripts.
- `skill-gap-finder.py` — Mine the index for recurring pain points (tool errors, retry loops, long conversations, project outliers) and draft reviewable SKILL.md proposals. Matches patterns to installed skills via keyword overlap. Never auto-edits skill files.
- `format-detector.py` — Signature-matching format detection for unknown agent transcripts. Scores known formats (Claude Code, Grok, Kimi Code, Cline, Aider, generic markdown) and surfaces unknown formats with sample keys for extension.

**Per-command scripts:**
- `analyze-session.sh` — Pattern analysis: retry loops, errors, user corrections. Invoked by `/analyze`.
- `apply-rules.sh` — Format, review, and persist candidate rules. Invoked by `/apply`.
- `post-compaction-hook.sh` — Claude Code PreToolUse hook: detect compaction and remind about context recovery tools.

**Group chat profiles (v0.7.1):**
- `chat-profiles.py` — Incremental participant profile extraction for group chats.
  Uses append-only merge rules inspired by baoyu-wechat-summary: quotes/events
  append unbounded, tags/interests merge with frequency sorting, speaking style
  only refines at 100-message thresholds. Outputs one JSON per participant.

**Replaced by `sd-recall.py` subcommands:**
- The old shell scripts (`list-sessions.sh`, `extract-messages.sh`, `extract-tools.sh`, `session-stats.sh`, `parse-jsonl.sh`, etc.) have been removed in v0.6.0.
- Their functionality lives in `sd-recall.py` subcommands:
  `sessions`, `search`, `stats`, `session-stats`, `messages`, `tools`, `files`, `schema`.
- `save-summary.sh` and `extract-knowledge.sh` have also been replaced:
  `sd-recall.py save-summary` and `sd-recall.py extract-knowledge`.
- `recall-lite.sh` — End-to-end no-API recall. Invoked by `/recall --lite` and runnable directly from a shell.

## Architecture (four-layer model)

Inspired by agent-transcript-analyzer. Each layer has a distinct cost and trust level:

```
Layer 0: PARSE     echolib.py           Raw transcript → stats (exact, ground truth)
Layer 1: INDEX     index-builder.py     Stats → SQLite cache (rebuildable, fast to query)
Layer 2: TREND     trend-engine.py      Index → aggregation (pure arithmetic, re-runnable)
Layer 3: DECISION  skill-gap-finder.py  Patterns → proposals (judgment call, human-approved)
```

Never collapse layers: Layer 0 is exact, Layer 1 is a cheap cache, Layer 2 is
pure aggregation, Layer 3 is the only layer that makes judgment calls.

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

## Incremental fingerprint

`_file_fingerprint()` uses mtime + size + head-hash (4KB MD5) for change detection.
This catches content changes that preserve mtime (git checkout, file copy with
preserved timestamp), which the old mtime-only check missed.

## Privacy markers

`dialog-adapter.py` stamps imported sessions with a `privacy` field:
- `"plaintext_chat"` for WeChat/transcript imports (plaintext conversation data)
- `"imported"` for generic JSON/CSV

Downstream analysis should respect this flag and avoid echoing raw content
back to users or LLMs without explicit consent. This mirrors wechat-local-vault's
`contains_plaintext_wechat_data: true` marker pattern.

## References

- `references/report-template.md` — Structure for single-session, batch, trend, and skill-gap reports.
- `references/format-signatures.md` — Agent format signature catalogue + checklist for adding new ones.

## Prerequisites

- Python 3.6+ (stdlib only, no pip packages)
- bash
- git (optional, needed only for git-mining features and git-based agents)
- curl (optional, needed only for URL validation in /audit --deep)
