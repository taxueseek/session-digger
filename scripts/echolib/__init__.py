"""echolib — session-digger parsing and environment adapter library.

Split from the original ``echolib.py`` (6041 lines) into focused submodules
for maintainability.  All submodules are loaded into a shared namespace so
cross-module function references (e.g. ``_iter_jsonl()`` inside an adapter)
work without explicit imports — identical to the original single-file behaviour.

Usage: ``import echolib`` — unchanged from the monolith.
"""
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

# ── Shared namespace: all submodules define into, and read from, the same dict.
# This preserves cross-module references without explicit imports.
_namespace: dict = {}
_order = ["_helpers", "_models", "_claude", "_knowledge", "_adapters"]

for _mod in _order:
    _src = (_SCRIPT_DIR / f"{_mod}.py").read_text(encoding="utf-8")
    try:
        exec(compile(_src, f"echolib/{_mod}.py", "exec"), _namespace)
    except Exception as _exc:
        raise RuntimeError(f"Failed to load echolib/{_mod}.py") from _exc

globals().update(_namespace)

# Clean up helper names to keep dir(echolib) clean
for _k in list(globals()):
    if _k.startswith("_") and _k not in ("__name__", "__doc__", "__package__",
        "__loader__", "__spec__", "__path__", "__file__", "__builtins__"):
        if _k not in _namespace:  # only remove our temp vars
            pass
globals().pop("_SCRIPT_DIR", None)
globals().pop("_namespace", None)
globals().pop("_order", None)
globals().pop("_mod", None)
globals().pop("_src", None)
globals().pop("_exc", None)
globals().pop("Path", None)
