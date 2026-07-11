#!/usr/bin/env python3
"""Public-API gate for echolib._adapters.

Before committing the adapter-split: collects the set of every public symbol
exported by the package (via `from echolib._adapters import X` across the
repo), then — after the split — verifies re-export layer still exposes all
of them. Fails hard on any missing symbol.

Run:  python3 tests/_check_public_api.py
Used by: CI step + local commit gate.
"""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TESTS = Path(__file__).resolve().parent

# ── 1) Load the re-export entry point (echolib._adapters) ─────────────
_ADAPTERS = SCRIPTS / "echolib" / "_adapters.py"

# Ensure all sibling submodules resolve under the same `echolib` package.
PKG_INIT = SCRIPTS / "echolib" / "__init__.py"
pkg = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("echolib", str(PKG_INIT)))
sys.modules["echolib"] = pkg
importlib.util.spec_from_file_location("echolib", str(PKG_INIT)).loader.exec_module(pkg)

spec = importlib.util.spec_from_file_location("echolib._adapters", str(_ADAPTERS))
adapters = importlib.util.module_from_spec(spec)
sys.modules["echolib._adapters"] = adapters
spec.loader.exec_module(adapters)

# ── 2) Collect expected public names from actual usage ──────────────
expected: set[str] = set()
for family in (SCRIPTS, TESTS):
    for py in family.rglob("*.py"):
        if py.name == "_check_public_api.py":
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        #  from echolib._adapters import A, B, C
        import re
        for line in text.splitlines():
            m = re.search(r'from echolib\._adapters import (.+)', line)
            if not m:
                continue
            names_blob = m.group(1)
            # strip trailing comments
            names_blob = names_blob.split('#')[0]
            for n in names_blob.split(','):
                n = n.strip()
                if n and not n.startswith('(') and not n.endswith(')'):
                    n = n.split(' as ')[0].strip()
                    if n and n[0].isalpha() or n.startswith('_'):
                        expected.add(n)

# Also add canonical dispatcher names regardless of grep hit.
canonical = [
    "dispatch_resolve_agent",
    "dispatch_session_stats",
    "dispatch_extract_messages",
    "dispatch_extract_tools",
    "scan_all_environments_parallel",
    "register_adapter",
    "ADAPTER_REGISTRY",
]
expected.update(canonical)

# ── 3) Verify ────────────────────────────────────────────────────────
actual: set[str] = set(dir(adapters))
missing = sorted(expected - actual)

print(f"Expected public names: {len(expected)}")
print(f"Actual exports:        {len(actual)}")
if missing:
    print("MISSING (re-export failed):")
    for name in missing:
        print(f"  - {name}")
    raise SystemExit(1)
print("PASS: re-export layer exposes every public name.")