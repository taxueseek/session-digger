"""
_adapter_discovery.py — Filesystem-based adapter plugin discovery.

Scans ``~/.config/session-digger/adapters/`` for external adapter
directories.  Each directory must contain an ``adapter.py`` that
exposes::

    def register(registry: dict) -> None:
        '''Register one or more adapters into *registry* (ADAPTER_REGISTRY).'''
        registry["my_env"] = {
            "name": "my_env",
            "display_name": "My Env",
            "list_sessions": my_list_sessions,
            "session_stats": my_session_stats,
            "extract_messages": my_extract_messages,
            "extract_tools": my_extract_tools,
            "session_path": my_session_path,
        }

The discovery dir can be overridden with the env var
``SESSION_DIGGER_ADAPTERS_DIR``.

Usage::

    from echolib._adapter_discovery import discover_plugins
    discovered = discover_plugins()
    for name, entry in discovered.items():
        ADAPTER_REGISTRY[name] = entry

This module has zero dependencies on other echolib submodules.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _discovery_dirs() -> list[Path]:
    """Return candidate directories to scan for adapter plugins.

    Priority: SESSION_DIGGER_ADAPTERS_DIR env → XDG config → default.
    """
    env = os.environ.get("SESSION_DIGGER_ADAPTERS_DIR")
    if env:
        return [Path(env).expanduser()]

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return [Path(xdg) / "session-digger" / "adapters"]

    return [Path.home() / ".config" / "session-digger" / "adapters"]


def discover_plugins() -> dict[str, dict]:
    """Scan the adapters directory and import any external adapter found.

    Returns a dict of ``{env_id: adapter_dict}`` suitable for merging into
    ``ADAPTER_REGISTRY``.  Silent on missing directories or import errors
    (logged to stderr for debugging).
    """
    discovered: dict[str, dict] = {}

    for base in _discovery_dirs():
        if not base.is_dir():
            continue

        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            adapter_file = entry / "adapter.py"
            if not adapter_file.is_file():
                continue

            env_id = entry.name
            spec = importlib.util.spec_from_file_location(
                f"echolib_external_{env_id}", str(adapter_file),
            )
            if spec is None or spec.loader is None:
                continue

            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
            except Exception as exc:
                print(
                    f"[session-digger] warning: failed to load "
                    f"external adapter '{env_id}': {exc}",
                    file=sys.stderr,
                )
                continue

            if not hasattr(mod, "register"):
                print(
                    f"[session-digger] warning: external adapter '{env_id}' "
                    f"has no register() function, skipping",
                    file=sys.stderr,
                )
                continue

            # Create a temporary registry for this adapter to fill
            temp_registry: dict[str, dict] = {}
            try:
                mod.register(temp_registry)
            except Exception as exc:
                print(
                    f"[session-digger] warning: external adapter '{env_id}' "
                    f"register() raised: {exc}",
                    file=sys.stderr,
                )
                continue

            for name, entry_dict in temp_registry.items():
                # Validate minimal required keys
                required = {"list_sessions", "session_stats",
                            "extract_messages", "extract_tools", "session_path"}
                actual = set((entry_dict or {}).keys()) & required
                missing = required - actual
                if missing:
                    print(
                        f"[session-digger] warning: external adapter "
                        f"'{name}' missing: {missing}, skipping",
                        file=sys.stderr,
                    )
                    continue
                entry_dict.setdefault("display_name", name)
                discovered[name] = entry_dict

    return discovered
