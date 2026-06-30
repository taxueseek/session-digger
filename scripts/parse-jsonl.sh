#!/usr/bin/env bash
# parse-jsonl.sh — High-performance JSONL parser with pre-filtering and schema awareness
# Usage: parse-jsonl.sh <file.jsonl> [--types user,assistant] [--skip-noise] [--limit N]
#        [--fields type,timestamp,message] [--format lines|json|tsv] [--detect-schema]
#
# This is the canonical parser. All other extract-* scripts are convenience wrappers.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

FILE="${1:?Usage: parse-jsonl.sh <file.jsonl> [options]}"
shift

TYPES=""
SKIP_NOISE=0
LIMIT=0
FIELDS=""
FORMAT="lines"
DETECT_SCHEMA=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --types) TYPES="$2"; shift 2 ;;
    --skip-noise) SKIP_NOISE=1; shift ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --fields) FIELDS="$2"; shift 2 ;;
    --format) FORMAT="$2"; shift 2 ;;
    --detect-schema) DETECT_SCHEMA=1; shift ;;
    *) echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ "$LIMIT" != "0" ]] && ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --limit must be a number" >&2
  exit 1
fi

ES_FILE="$FILE" ES_TYPES="$TYPES" ES_SKIP_NOISE="$SKIP_NOISE" ES_LIMIT="$LIMIT" \
ES_FIELDS="$FIELDS" ES_FORMAT="$FORMAT" ES_DETECT_SCHEMA="$DETECT_SCHEMA" \
ES_SCRIPT_DIR="$SCRIPT_DIR" \
python3 << 'PYEOF'
import json, sys, os
sys.path.insert(0, os.environ["ES_SCRIPT_DIR"])
import echolib

file_path = os.environ['ES_FILE']
type_filter = set(os.environ.get('ES_TYPES', '').split(',')) - {''}
skip_noise = os.environ.get('ES_SKIP_NOISE', '0') == '1'
limit = int(os.environ.get('ES_LIMIT', '0'))
field_list = [f.strip() for f in os.environ.get('ES_FIELDS', '').split(',') if f.strip()]
fmt = os.environ.get('ES_FORMAT', 'lines')
detect_schema = os.environ.get('ES_DETECT_SCHEMA', '0') == '1'

if detect_schema:
    schema = echolib.detect_schema(file_path)
    print("file={}".format(schema["file"]))
    print("lines={}".format(schema["lines"]))
    print("bytes={}".format(schema["bytes"]))
    print("first_timestamp={}".format(schema["first_timestamp"]))
    print("last_timestamp={}".format(schema["last_timestamp"]))
    print("versions={}".format(",".join(schema["versions"])))
    print("models={}".format(",".join(schema["models"])))
    ut = schema["unknown_types"]
    print("unknown_types={}".format(",".join(ut) if ut else "none"))
    print("")
    print("record_types:")
    for rtype, info in schema["record_types"].items():
        marker = " [UNKNOWN]" if rtype in ut else ""
        print("  {}: {}{}".format(rtype, info["count"], marker))
        print("    fields: {}".format(", ".join(info["fields"])))
    sys.exit(0)

# Normal parsing mode
count = 0
if fmt == 'json':
    print('[')

for rec in echolib.iter_records(file_path, types=type_filter or None,
                                  skip_noise=skip_noise, limit=limit):
    d = rec.raw
    if field_list:
        d = {k: d[k] for k in field_list if k in d}

    if fmt == 'tsv':
        values = []
        for f_name in (field_list or sorted(d.keys())):
            v = d.get(f_name, '')
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            else:
                v = str(v)
            v = v.replace('\t', ' ').replace('\n', ' ')
            values.append(v)
        print('\t'.join(values))
    elif fmt == 'json':
        prefix = '  ' if count == 0 else ', '
        print(prefix + json.dumps(d, ensure_ascii=False))
    else:
        print(json.dumps(d, ensure_ascii=False))

    count += 1

if fmt == 'json':
    print(']')
PYEOF
