"""Environment registry data + parallel discovery (leaf-ish module).

Role: **registry data** — ENV_REGISTRY / KNOWN_UNADAPTED and scan helpers.
Does not register adapter callables (that stays in _adapters hub).
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
from pathlib import Path

_log = logging.getLogger("echolib.registry")

# ── Adapted environments (have a full adapter registration) ──
ENV_REGISTRY = {
    "claude": {"name": "Claude Code", "root": "~/.claude/projects/", "format": "jsonl", "adapter": "claude"},
    "grok": {"name": "Grok Build", "root": "~/.grok/sessions/", "format": "jsonl", "adapter": "grok"},
    "kimi": {"name": "Kimi (standalone)", "root": "~/.kimi/sessions/", "format": "jsonl", "adapter": "kimi"},
    "kimi_code": {"name": "Kimi Code", "root": "~/.kimi-code/sessions/", "format": "jsonl", "adapter": "kimi_code"},
    "codex": {"name": "Codex (OpenAI)", "root": "~/.codex/sessions/", "format": "jsonl", "adapter": "codex"},
    "cursor": {"name": "Cursor", "root": "~/.cursor/projects/", "format": "jsonl+sqlite", "adapter": "cursor"},
    "workbuddy": {"name": "WorkBuddy", "root": "~/.workbuddy/projects/", "format": "jsonl", "adapter": "workbuddy"},
    "trae_cn": {"name": "Trae CN (ByteDance)", "root": "~/.trae-cn/memory/projects/", "format": "jsonl-summary", "adapter": "trae_cn"},
    "zcode": {"name": "ZCode (Z-AI)", "root": "~/.zcode/cli/agents/", "format": "jsonl-trace", "adapter": "zcode"},
    "dim": {"name": "DIM (Memory)", "root": "~/.dim/memory/", "format": "jsonl-summary", "adapter": "dim"},
    "dimcode": {"name": "DimCode (SQLite)", "root": "~/.dimcode/v2/dimcode.sqlite", "format": "sqlite", "adapter": "dimcode"},
    "reasonix": {"name": "Reasonix", "root": "~/.reasonix/sessions/", "format": "jsonl", "adapter": "reasonix"},
}

# Light discovery only — universal SchemaProbe handles parse when opened.
KNOWN_UNADAPTED = {
    "mimo": {"name": "MiMo", "root": "~/.mimo/"},
    "qwen": {"name": "Qwen Code", "root": "~/.qwen/projects/"},
    "qoder": {"name": "Qoder", "root": "~/.qoder/cache/projects/"},
    "qoder-cn": {"name": "Qoder CN", "root": "~/.qoder-cn/"},
    "openclaw-autoclaw": {"name": "OpenClaw AutoClaw", "root": "~/.openclaw-autoclaw/agents/"},
    "gstack": {"name": "GStack", "root": "~/.gstack/"},
    "codebuddy": {"name": "CodeBuddy", "root": "~/.codebuddy/"},
    "commandcode": {"name": "CommandCode", "root": "~/.commandcode/"},
    "cc-switch": {"name": "CC-Switch", "root": "~/.cc-switch/"},
    "newmax": {"name": "NewMax", "root": "~/.newmax/conversations/"},
    "proma": {"name": "Proma", "root": "~/.proma/agent-sessions/"},
    "iflow": {"name": "iFlow", "root": "~/.iflow/projects/"},
    "deepcode": {"name": "DeepCode", "root": "~/.deepcode/projects/"},
    "gemini": {"name": "Gemini", "root": "~/.gemini/"},
}


def scan_all_environments_parallel():
    """Parallel scan of all known and unknown environments.

    Each result includes ``tier`` (adapter depth) for honest capability display.
    """
    # Lazy imports keep this module free of adapter business logic.
    from echolib._claude_index import _fast_find_jsonl
    from echolib._policy import adapter_tier

    results = []

    def _scan_env(env_id, env_info, is_registered=True):
        root = Path(os.path.expanduser(env_info["root"]))
        exists = root.exists()
        session_count = 0
        if exists:
            if root.is_file():
                session_count = 1
            else:
                jsonl_files = _fast_find_jsonl(root)
                session_count = len(jsonl_files)
        adapter = env_info.get("adapter", "universal")
        return {
            "name": env_info["name"],
            "env_id": env_id,
            "path": str(root),
            "exists": exists,
            "session_count": session_count,
            "format": env_info.get("format", "unknown"),
            "adapter": adapter,
            "status": "adapted" if is_registered else ("unadapted" if exists else "missing"),
            "tier": adapter_tier(adapter if is_registered else "universal"),
        }

    env_tasks = []
    for env_id, env_info in ENV_REGISTRY.items():
        env_tasks.append((env_id, env_info, True))
    for env_id, env_info in KNOWN_UNADAPTED.items():
        env_tasks.append((env_id, env_info, False))

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_scan_env, eid, info, reg): eid
            for eid, info, reg in env_tasks
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                _log.warning("adapter thread failed: %s", exc, exc_info=True)

    home = Path.home()
    known_dirs = set()
    for e in list(ENV_REGISTRY.values()) + list(KNOWN_UNADAPTED.values()):
        known_dirs.add(os.path.expanduser(e["root"]).split("/")[0])
    known_dirs.update(
        str(home / d)
        for d in (
            ".claude", ".zcode", ".agents", ".config", ".cache", ".npm", ".cargo",
            ".ssh", ".local", ".cursor", ".codex", ".grok",
        )
    )

    dotdirs = []
    try:
        for dotdir in home.iterdir():
            if not dotdir.is_dir() or not dotdir.name.startswith("."):
                continue
            if str(dotdir) in known_dirs:
                continue
            dotdirs.append(dotdir)
    except OSError:
        pass

    def _scan_unknown_dir(dotdir):
        try:
            jsonl_files = _fast_find_jsonl(dotdir)
            if jsonl_files:
                return {
                    "name": dotdir.name,
                    "env_id": dotdir.name.lstrip("."),
                    "path": str(dotdir),
                    "exists": True,
                    "session_count": len(jsonl_files),
                    "format": "unknown",
                    "adapter": "universal",
                    "status": "discovered",
                    "tier": adapter_tier("universal"),
                }
        except OSError:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_scan_unknown_dir, d) for d in dotdirs]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as exc:
                _log.warning("adapter thread failed: %s", exc, exc_info=True)

    return results
