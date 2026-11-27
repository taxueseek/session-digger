# Git Mining Reference (Code History Correlation)

> 原 `skills/git-mining` 合并至此参考文档，由 `/timeline` 与 `file-historian` 按需加载。

## Prerequisite Check

```bash
git rev-parse --git-dir 2>/dev/null && echo "IS_GIT_REPO" || echo "NOT_GIT_REPO"
```

## Bundled Scripts

```bash
SD_ROOT="${SESSION_DIGGER_ROOT:-$HOME/.agents/skills/session-digger}"
bash "$SD_ROOT/scripts/git-sessions.sh" [repo-path] [--since "14 days ago"] [--gap 3600]
bash "$SD_ROOT/scripts/git-context.sh" [repo-path] [--since "7 days ago"] [--limit 30]
```

## Key Git Commands

```bash
git log --pretty=format:"%h|%ai|%s" --numstat --since="14 days ago" -30
git log --oneline --follow -- path/to/file.ts
git log --oneline -S "FunctionName"
git log --name-only --pretty=format: --since="30 days ago" | grep -v '^$' | sort | uniq -c | sort -rn | head -15
```

## Correlating Git Commits with Agent Sessions

1. **Timestamp matching**: compare session `created`/`modified` (from `index.db` or `sd-recall.py sessions`) with `git log --pretty=format:"%at|%h|%s"`.
2. **Branch correlation**: match `sessions.branch` (`gitBranch`) with `git log --all --oneline <branch>`.
3. **Commit + transcript**: inspect git commits for *what shipped*, then query `sd-recall.py search` / `messages` for *why it was chosen* and *what failed*.
