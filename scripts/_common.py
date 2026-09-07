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
