#!/usr/bin/env bash
# Create skill-local .venv and install acquire deps (no system pip).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${WECHAT_DIGGER_VENV:-$ROOT/.venv}"
REQ="$ROOT/scripts/acquire/requirements.txt"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -U pip -q
"$VENV/bin/pip" install -r "$REQ" -q
echo "OK: $VENV/bin/python"
"$VENV/bin/python" -c "import zstandard; print('deps ok')"
"$VENV/bin/python" -c "from Crypto.Cipher import AES" 2>/dev/null && echo "pycryptodome ok (image layer enabled)" || echo "pycryptodome missing: image layer disabled (optional)"
