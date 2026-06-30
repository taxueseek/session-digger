#!/usr/bin/env bash
# memory-dashboard.sh — Memory overview and heuristic audit output
# Usage: memory-dashboard.sh [--project NAME]
#
# Without --project: scans all projects.
# With --project: filters to matching project.
# Output: formatted text summary.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PROJECT_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_FILTER="$2"; shift 2 ;;
    *) shift ;;
  esac
done

ES_PROJECT="$PROJECT_FILTER" ES_SCRIPT_DIR="$SCRIPT_DIR" \
python3 << 'PYEOF'
import os, sys, time
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

project_filter = os.environ.get("ES_PROJECT", "") or None

dirs = echolib.all_memory_dirs()
if not dirs:
    print("No projects with memories found.")
    sys.exit(0)

# Filter if requested
if project_filter:
    dirs = [(name, path) for name, path in dirs if project_filter in name]
    if not dirs:
        print("No matching projects found for: %s" % project_filter)
        sys.exit(0)

total_files = 0
total_tokens = 0
alerts = []
project_stats = []

# Compute all_project_dirs count once (fixes double-call)
all_proj_count = len(list(echolib.all_project_dirs()))

for proj_name, mem_dir in sorted(dirs):
    stats = echolib.memory_stats(mem_dir)
    total_files += stats.file_count
    total_tokens += stats.estimated_tokens
    project_stats.append((proj_name, stats))

    for m in echolib.iter_memories(mem_dir):
        ss = echolib.staleness_score(m)
        if ss.score > 50:
            age_days = int((time.time() - m.mtime) / 86400)
            alerts.append((ss.score, os.path.basename(m.path), proj_name,
                           "%dd old, type=%s" % (age_days, m.type), ss.action))

print("Memory Dashboard")
print("=" * 60)
print("Projects with memories:  %d / %d" % (len(dirs), all_proj_count))
print("Total memory files:      %d" % total_files)
print("Estimated token load:    ~%d tokens/conversation" % total_tokens)
print()

if alerts:
    alerts.sort(key=lambda x: -x[0])
    print("Staleness Alerts (%d)" % len(alerts))
    print("-" * 60)
    for score, fname, proj, reason, action in alerts:
        print("  [%3d] %s/%s — %s → %s" % (score, proj, fname, reason, action))
    print()

# Top token consumers
project_stats.sort(key=lambda x: -x[1].estimated_tokens)
print("Top Token Consumers")
print("-" * 60)
for proj_name, stats in project_stats[:10]:
    print("  %-40s %d files, ~%d tokens" % (proj_name, stats.file_count, stats.estimated_tokens))
PYEOF
