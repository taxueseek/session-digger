#!/usr/bin/env bash
# build-index.sh — Build fallback index for projects without sessions-index.json
# Usage: build-index.sh [project-path|"all"]
#
# Creates .session-digger-index.json cache files for fast repeat access.
# This is called automatically by list-sessions.sh, but can be run manually
# to pre-warm the cache for all projects.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

SCOPE="${1:-all}"

ES_SCOPE="$SCOPE" ES_SCRIPT_DIR="$SCRIPT_DIR" \
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

scope = os.environ.get("ES_SCOPE", "all")
total = 0
indexed = 0

if scope == "all":
    for project_dir in echolib.all_project_dirs():
        index_path = project_dir / "sessions-index.json"
        if not index_path.exists():
            jsonl_files = list(project_dir.glob("*.jsonl"))
            if jsonl_files:
                total += 1
                entries = echolib.build_fallback_index(project_dir)
                if entries:
                    indexed += 1
                    print("Indexed {}: {} sessions".format(project_dir.name, len(entries)))
    print("\nDone: indexed {} of {} unindexed projects".format(indexed, total))
else:
    proj_dir = echolib.find_project_dir(scope)
    if not proj_dir:
        print("ERROR: No Claude session directory found for " + scope, file=sys.stderr)
        sys.exit(1)
    index_path = proj_dir / "sessions-index.json"
    if index_path.exists():
        print("Already indexed: {} (sessions-index.json exists)".format(proj_dir.name))
        sys.exit(0)
    entries = echolib.build_fallback_index(proj_dir)
    print("Indexed {}: {} sessions".format(proj_dir.name, len(entries)))
PYEOF
