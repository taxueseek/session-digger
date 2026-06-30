#!/usr/bin/env bash
# git-context.sh — Structured git context dump for Claude to reason about
# Usage: git-context.sh [repo-path] [--since "7 days ago"] [--limit 30]

set -euo pipefail

# Parse repo path: only consume first arg if it's not a flag
REPO="."
if [[ $# -gt 0 && "${1}" != --* ]]; then
  REPO="$1"
  shift
fi

SINCE="14 days ago"
LIMIT=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --limit must be a number" >&2
  exit 1
fi

if ! command -v git &>/dev/null; then
  echo "ERROR: git is not installed or not in PATH" >&2
  exit 1
fi

if ! git -C "$REPO" rev-parse --git-dir &>/dev/null; then
  echo "ERROR: $REPO is not a git repository" >&2
  exit 1
fi

echo "# Git Context Report"
echo "# Repo: $(git -C "$REPO" remote get-url origin 2>/dev/null || basename "$(cd "$REPO" && pwd)")"
echo "# Branch: $(git -C "$REPO" branch --show-current 2>/dev/null || echo 'detached')"
echo "# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

echo "## Commit Activity"
git -C "$REPO" log --pretty=format:"%ai" --since="$SINCE" 2>/dev/null | \
  cut -c1-10 | sort | uniq -c | sort -k2 || true
echo ""

echo ""
echo "## Hotspot Files (most changed)"
git -C "$REPO" log --name-only --pretty=format: --since="$SINCE" 2>/dev/null | \
  grep -v '^$' | sort | uniq -c | sort -rn | head -15 || true
echo ""

echo ""
echo "## Recent Commits"
git -C "$REPO" log \
  --pretty=format:"---
hash: %h
date: %ai
subject: %s
body: %b" \
  --numstat \
  --since="$SINCE" \
  -n "$LIMIT" 2>/dev/null || true
