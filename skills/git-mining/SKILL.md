---
name: git-mining
description: This skill should be used when the user asks to "check git history", "trace code changes", "find when something was added", "correlate commits with sessions", "find hotspot files", or needs to mine git repository history for insights. Only applicable when the current project is a git repository. Use with `/trend` for commit frequency analysis over time.
version: 0.3.0
---

# Git Mining — Code History Integration

## Prerequisite Check

Before using git commands, verify the project is a git repo:
```bash
git rev-parse --git-dir 2>/dev/null && echo "IS_GIT_REPO" || echo "NOT_GIT_REPO"
```

If not a git repo, skip all git-related analysis and rely on JSONL data only.

## Scripts

### Cluster commits into work sessions
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/git-sessions.sh [repo-path] [--since "14 days ago"] [--gap 3600]
```

### Full structured context dump
```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/git-context.sh [repo-path] [--since "7 days ago"] [--limit 30]
```

## Key Git Commands for Agents

### Recent commits with file changes
```bash
git log --pretty=format:"%h|%ai|%s" --numstat --since="14 days ago" -30
```

### File history (survives renames)
```bash
git log --oneline --follow -- path/to/file.ts
```

### Symbol archaeology (when was X introduced?)
```bash
git log --oneline -S "FunctionName"        # exact string
git log --oneline -G "pattern.*regex"      # regex in diff
```

### Hotspot files (most frequently changed)
```bash
git log --name-only --pretty=format: --since="30 days ago" | grep -v '^$' | sort | uniq -c | sort -rn | head -15
```

### Commit message search
```bash
git log --oneline --grep="keyword" -i
```

### What changed in a file recently
```bash
git log -p --follow --since="7 days ago" -- path/to/file.ts
```

## Correlating Git Commits with Claude Sessions

Git commits made during Claude Code sessions cluster together in time (typically 60-440 seconds apart). Cross-session gaps are 1+ hours.

### Method 1: Timestamp matching
Compare `sessions-index.json` entries (`created`/`modified` fields) with git commit timestamps (`git log --pretty=format:"%at"`) to find which commits were made during which sessions.

### Method 2: Conventional commit patterns
Claude Code sessions typically produce commit sequences like:
- `feat:` → `fix:` → `fix:` → `docs:` → `release:` (complete cycle)
- `fix:` → `test:` (debugging session)
- `refactor:` → `style:` (cleanup session)

### Method 3: Branch correlation
The `gitBranch` field in session records and `sessions-index.json` entries tells you which branch each session was working on. Match with `git log --all --oneline <branch>`.

## What to Extract from Git History

| Question | Command |
|----------|---------|
| What changed recently? | `git log --oneline --since="7 days ago"` |
| What files change together? | `git log --name-only` then co-occurrence analysis |
| Who/what introduced a bug? | `git log -S "buggy_code"` + `git blame` |
| What's the architectural evolution? | `git log --diff-filter=A --name-status` (new files over time) |
| What keeps breaking? | `git log --grep="fix" --name-status` |
| What was the rationale? | `git log --pretty=format:"%s%n%b" -1 <hash>` (commit body) |

## Combining Git + JSONL

The most powerful analysis combines both sources:
1. Find the relevant git commits for a time period
2. Match their timestamps to Claude sessions
3. Read the session `.jsonl` to understand the full conversation context behind those commits
4. This gives you: **what was done** (git) + **why it was done** (conversation) + **what was tried and failed** (tool errors in JSONL)


## 下游协作

| 触发条件 | 推荐 |
|----------|------|
| 需要解析关联的会话记录 | `jsonl-core` |
| 挖出提交模式需要提炼经验 | `experience-synthesis` |

## DO NOT

- 从零创建新 skill → `skill-creator`
- 需要提炼经验教训 → `experience-synthesis`（git-mining 只做挖掘，不做分析）
- 需要解析 JSONL 会话数据 → `jsonl-core`
