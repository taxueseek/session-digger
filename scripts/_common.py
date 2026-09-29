"""Minimal shared utilities for session-digger."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path | str, data: Any, *, indent: bool = True) -> Path:
    p = Path(path)
    opts = {"ensure_ascii": False, "indent": 2} if indent else {"ensure_ascii": False}
    p.write_text(json.dumps(data, **opts) + "\n", encoding="utf-8")
    return p


def read_text(path: Path | str, *, errors: str = "strict") -> str:
    return Path(path).read_text(encoding="utf-8", errors=errors)


def json_out(data: Any, *, indent: bool = True) -> str:
    """Serialize to JSON string."""
    opts = {"ensure_ascii": False, "indent": 2} if indent else {"ensure_ascii": False}
    return json.dumps(data, **opts)


def ts19(ts: Any, default: str = "") -> str:
    """Timestamp → first 19 chars (``YYYY-MM-DDTHH:MM:SS``), tolerantly.

    The one shared implementation of "truncate a timestamp to seconds" for
    display and for ``strptime``. Before it lived here, the raw slice was
    re-derived at a dozen call sites and each rolled its own guard: a JSON
    null timestamp crashed the ``cmd_tools`` render, an integer epoch
    timestamp crashed the claude tool-extraction pass, and the sites that did
    guard only guarded None — a truthy non-string still fell through and
    died on the slice. Every timestamp truncation must route through here,
    so the next malformed shape is one fix instead of the thirteenth copy.
    """
    return ts[:19] if isinstance(ts, str) and ts else default
