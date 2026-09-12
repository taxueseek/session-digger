#!/usr/bin/env python3
"""Runtime contract for wechat-digger.

Single source of truth for the interpreter requirement, declared once and
enforced at the CLI boundary. Before this module existed the requirement was
implicit and self-contradictory: ``extract_keys.py`` advertised "Python 3.9+"
while ``analyze.py`` called ``int.bit_count()`` (3.10+ only), so the same
codebase passed 108/108 tests on Homebrew 3.14 and failed 11 on the macOS
system 3.9 interpreter. The failure looked like a code defect but was really
an undeclared runtime contract.

Two design decisions worth recording:

1. **Minimum is 3.8, not 3.10.** The only 3.10-only API in the tree
   (``int.bit_count``) now has a portable fallback in ``analyze._popcount``,
   so lowering the floor is cheaper than breaking every documented
   ``python3 ...`` invocation on a stock macOS system interpreter.
2. **The check is advisory by default.** ``require_python`` raises only for
   interpreters that genuinely cannot run the code. A warning for
   "newer than tested" versions keeps a future 3.15 from being rejected
   outright while still surfacing that it is untested.
"""

from __future__ import annotations

import sys

# Lowest interpreter this codebase is verified to run on. See module
# docstring for why this is 3.8 rather than 3.10.
MIN_PYTHON: tuple[int, int] = (3, 8)

# Highest interpreter the test suite has actually been executed against.
# Newer interpreters are allowed but reported as untested.
TESTED_THROUGH: tuple[int, int] = (3, 14)

# The interpreter pin used for the project's own virtualenv. Recorded here so
# ``doctor`` can compare it against whatever interpreter the user invoked.
RECOMMENDED_PYTHON: str = "3.12"


def _fmt(version: tuple[int, int]) -> str:
    return ".".join(str(part) for part in version)


def check_python(version_info: tuple[int, ...] | None = None) -> dict:
    """Return a structured verdict about the running interpreter.

    Never raises. Callers decide how to react, so ``doctor`` can report the
    same facts it uses to gate execution.
    """
    info = version_info if version_info is not None else sys.version_info
    current = (info[0], info[1])

    supported = current >= MIN_PYTHON
    tested = current <= TESTED_THROUGH

    if not supported:
        status = "unsupported"
        detail = (
            f"Python {_fmt(MIN_PYTHON)}+ is required; "
            f"running {_fmt(current)}"
        )
    elif not tested:
        status = "untested"
        detail = (
            f"Python {_fmt(current)} is newer than the tested ceiling "
            f"({_fmt(TESTED_THROUGH)}); proceeding, but this combination "
            "has not been validated"
        )
    else:
        status = "ok"
        detail = f"Python {_fmt(current)} is supported and tested"

    return {
        "status": status,
        "supported": supported,
        "tested": tested,
        "current": _fmt(current),
        "minimum": _fmt(MIN_PYTHON),
        "tested_through": _fmt(TESTED_THROUGH),
        "recommended": RECOMMENDED_PYTHON,
        "detail": detail,
    }


def require_python(version_info: tuple[int, ...] | None = None) -> None:
    """Abort with an actionable message when the interpreter is too old.

    Untested-but-supported versions pass silently here (``doctor`` reports
    them); only genuinely unsupported interpreters stop execution.
    """
    verdict = check_python(version_info)
    if verdict["supported"]:
        return
    raise SystemExit(
        "wechat-digger requires Python "
        f"{verdict['minimum']}+ but is running on {verdict['current']}.\n"
        f"Install a newer interpreter (recommended {verdict['recommended']}) "
        "and re-run, or invoke the project virtualenv directly:\n"
        "  .venv/bin/python scripts/wd.py <command>"
    )


if __name__ == "__main__":
    import json

    print(json.dumps(check_python(), ensure_ascii=False, indent=2))
