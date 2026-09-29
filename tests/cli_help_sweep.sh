#!/usr/bin/env bash
# cli_help_sweep.sh — run `--help` against every CLI entry in scripts/ and
# print one line per entry: "OK <name>" or "FAIL <name> <reason>".
#
# Companion to tests/test_cli_entries.py, which runs this script and asserts
# the output. The sweep lives in bash because it is exactly what a shell
# user's first keystroke does, and because it keeps dynamic execution out of
# the Python gate.
#
# Excluded (not CLI entries — say why if you add more):
#   _common.py             stdlib-only helper, imported never executed
#   _sdroot.sh             sourced library defining SDROOT resolution
#   _check_dimcode_split.py one-off dev diagnostic
#   retrieval_utils.py     library: dedupe/collapse helpers, no __main__
#   topic_classify.py      library: shared classification logic for topic-scan.sh, no __main__
#   test_* / *_test.py     test helpers, not user entries

set -uo pipefail
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)"

fail=0
for f in "$SCRIPTS_DIR"/*.py "$SCRIPTS_DIR"/*.sh; do
  name="$(basename "$f")"
  case "$name" in
    _common.py|_sdroot.sh|_check_dimcode_split.py) continue ;;
    retrieval_utils.py|topic_classify.py) continue ;;
    test_*|*_test.py) continue ;;
  esac
  case "$name" in
    *.sh) out="$(bash "$f" --help 2>&1)" ;;
    *)    out="$(python3 "$f" --help 2>&1)" ;;
  esac
  rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "FAIL $name --help exited $rc"
    fail=1
  elif [[ -z "$out" ]]; then
    echo "FAIL $name --help printed nothing"
    fail=1
  elif printf '%s' "$out" | grep -q "Traceback"; then
    echo "FAIL $name --help crashed with a traceback"
    fail=1
  else
    echo "OK $name"
  fi
done

if [[ $fail -ne 0 ]]; then
  exit 1
fi
echo "ALL-OK"
