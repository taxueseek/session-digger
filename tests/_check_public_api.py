#!/usr/bin/env python3
"""Public-API gate for echolib._adapters.

Parses every ``from echolib._adapters import …`` across the repo with
``ast`` (never regex), then verifies the re-export layer still exposes
all of them.  Guards against:

* multi-line import statements (regex would miss them)
* digit-start identifiers (regex would IndexError)
* false-positive comments that look like imports

Run:  python3 tests/_check_public_api.py
Used by: CI step + local commit gate.
"""
import ast
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TESTS = Path(__file__).resolve().parent

# ── 1) Load the re-export entry point (echolib._adapters) ─────────────
PKG_INIT = SCRIPTS / "echolib" / "__init__.py"
pkg = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location("echolib", str(PKG_INIT)))
sys.modules["echolib"] = pkg
importlib.util.spec_from_file_location("echolib", str(PKG_INIT)).loader.exec_module(pkg)

_ADAPTERS = SCRIPTS / "echolib" / "_adapters.py"
spec = importlib.util.spec_from_file_location("echolib._adapters", str(_ADAPTERS))
adapters = importlib.util.module_from_spec(spec)
sys.modules["echolib._adapters"] = adapters
spec.loader.exec_module(adapters)

# ── 2) Collect expected public names via AST (never regex) ──────────
expected: set[str] = set()
for family in (SCRIPTS, TESTS):
    for py in family.rglob("*.py"):
        if py.name == "_check_public_api.py":
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "echolib._adapters":
                for alias in node.names:
                    # Use the *exported* name (alias.name), not the local
                    # binding (asname).  ``import X as Y`` still requires X
                    # on the adapters module.
                    name = alias.name
                    if name != "*":
                        expected.add(name)

# Canonical dispatcher names regardless of grep hit.
expected.update([
    "dispatch_resolve_agent",
    "dispatch_session_stats",
    "dispatch_extract_messages",
    "dispatch_extract_tools",
    "scan_all_environments_parallel",
    "register_adapter",
    "ADAPTER_REGISTRY",
])

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