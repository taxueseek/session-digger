---
description: Git log, blame, and diff commands used for correlating commits with session history
---

# Git Commands Reference for Echo Sleuth

## Log Format Placeholders

| Placeholder | Description |
|-------------|-------------|
| `%H` / `%h` | Full / short commit hash |
| `%an` / `%ae` | Author name / email |
| `%ai` | Author date (ISO 8601) |
| `%at` | Author date (Unix timestamp) — best for arithmetic |
| `%s` | Subject line |
| `%b` | Body |
| `%D` | Ref names (branches, tags) |

## Structured Output for Parsing

### Pipe-delimited (best for awk)
```bash
git log --pretty=format:'%h|%at|%ai|%s' -20
```

### With numstat (lines added/removed per file)
```bash
git log --pretty=format:"COMMIT %h %ai %s" --numstat -10
```
Output: `<added>\t<deleted>\t<filename>` after each commit header.

### With name-status (operation type per file)
```bash
git log --name-status -10
```
Operation codes: `A`=Added, `M`=Modified, `D`=Deleted, `R`=Renamed

## Filtering

```bash
# By time
git log --since="7 days ago" --until="3 days ago"

# By file type changed
git log --diff-filter=A   # only commits that Added files
git log --diff-filter=D   # only commits that Deleted files
git log --diff-filter=M   # only commits that Modified files

# By message
git log --grep="pattern" -i    # case-insensitive message search

# By code change (pickaxe)
git log -S "exact string"      # string was added or removed
git log -G "regex.*pattern"    # regex match on diff lines

# By file
git log --follow -- path/to/file    # follow renames
```

## Blame (Line-Level Attribution)

```bash
# Standard
git blame path/to/file.ts

# Machine-readable
git blame --porcelain path/to/file.ts

# For a specific line range
git blame -L 10,20 path/to/file.ts
```

## Activity Summaries

```bash
# Commits per day
git log --pretty=format:"%ai" --since="30 days ago" | cut -c1-10 | sort | uniq -c

# Commits per author
git shortlog -sn --since="30 days ago"

# Diff stats for a range
git diff --stat HEAD~10..HEAD
```
