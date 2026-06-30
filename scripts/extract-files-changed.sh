#!/usr/bin/env bash
# extract-files-changed.sh — List all files edited during a session
# Usage: extract-files-changed.sh <file.jsonl> [--with-versions]
#
# Output format:
#   FILE_PATH  [VERSION_COUNT]
#
# Uses reverse-read on large files to find the last snapshot efficiently.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

FILE="${1:?Usage: extract-files-changed.sh <file.jsonl> [--with-versions]}"
WITH_VERSIONS=0
[[ "${2:-}" == "--with-versions" ]] && WITH_VERSIONS=1

ES_FILE="$FILE" ES_VERSIONS="$WITH_VERSIONS" ES_SCRIPT_DIR="$SCRIPT_DIR" \
python3 << 'PYEOF'
import os, sys
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

file_path = os.environ["ES_FILE"]
with_ver = os.environ.get("ES_VERSIONS", "0") == "1"

files = echolib.extract_files_changed(file_path, with_versions=with_ver)
if not files:
    print("(no files changed in this session)", file=sys.stderr)
    sys.exit(0)

for entry in files:
    print("\t".join(str(x) for x in entry))
PYEOF
