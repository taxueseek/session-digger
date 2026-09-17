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
    "kimix": {"name": "Kimix CLI", "root": "~/.kimix/sessions/", "format": "jsonl", "adapter": "kimix"},
    "codex": {"name": "Codex (OpenAI)", "root": "~/.codex/sessions/", "format": "jsonl", "adapter": "codex"},
    "cursor": {"name": "Cursor", "root": "~/.cursor/projects/", "format": "jsonl+sqlite", "adapter": "cursor"},
    "workbuddy": {"name": "WorkBuddy", "root": "~/.workbuddy/projects/", "format": "jsonl", "adapter": "workbuddy", "count_via_adapter": True},
    "trae_cn": {"name": "Trae CN (ByteDance)", "root": "~/.trae-cn/memory/projects/", "format": "jsonl-summary", "adapter": "trae_cn"},
    "zcode": {"name": "ZCode (Z-AI)", "root": "~/.zcode/cli/agents/", "format": "jsonl-trace", "adapter": "zcode"},
    # ZCode v2 主会话库：Claude-Code 同构 transcript（agent-config + acp-config）。
    # scan_depth：transcript 在 root 下第 5 层目录（agent-config/<kind>/<id>/projects/<slug>/），
    # 默认 max_depth=4 正好差一层 — 这是 scan 计数 n=0 而 adapter 能列出的根因。
    "zcode_v2": {"name": "ZCode v2 (主会话)", "root": "~/.zcode/v2/", "format": "jsonl-claude", "adapter": "zcode_v2", "scan_depth": 6},
    # DSH (DeepSeek)：zstd 压缩事件流，v2 store 优先于 v0。
    # count_via_adapter：.jsonl.zstd 不被通用 .jsonl 扫描识别，且同一 session 目录
    # 可能同时存在 v0/v2 两个 store（需去重），必须由适配器发现逻辑计数。
    "dsh": {"name": "DSH (DeepSeek)", "root": "~/.dsh/sessions/", "format": "jsonl-zstd", "adapter": "dsh", "count_via_adapter": True},
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
    # Kigi CLI — 独立 CLI，grok 同款会话布局（chat_history.jsonl 每会话）。
    # 不做专用适配器；adapter 默认 universal，由 SchemaProbe 自行发现与解析。
    "kigi": {"name": "Kigi CLI", "root": "~/.kigi/sessions/"},
}

# Environment / adapter ids that transcripts sometimes carry in a ``model``
# field (DSH writes ``model: "dsh"`` for sessions with no model record). Model
# reports must never read these as model names. Derived from the registry so a
# newly added environment is excluded automatically — this set drifted twice
# (dsh, kimix, zcode_v2) while it was maintained by hand.
#
# Only the *adapted* environments contribute: the KNOWN_UNADAPTED names include
# real model families ("gemini", "qwen") that must stay eligible as models.
ENVIRONMENT_MODEL_TOKENS = frozenset(ENV_REGISTRY) | frozenset(
    info.get("adapter", "universal") for info in ENV_REGISTRY.values()
)


def environment_model_tokens() -> frozenset:
    """Return :data:`ENVIRONMENT_MODEL_TOKENS` (canonical accessor)."""
    return ENVIRONMENT_MODEL_TOKENS


# Adapters whose transcript the indexer's single-pass reader can mirror: one
# event per JSONL record, Claude's type/isMeta/message.content[] shape, usage
# inline in the record.
#
# This must be an allow-list. The reader yields ZEROS for a layout it cannot
# parse instead of raising, so a missing rule empties a whole environment
# silently — a deny-list shipped without `dsh` and `universal` and reported 610
# messages as 0. It is also deliberately NOT derived from the `format` field:
# the registry labels claude, grok, kimi and kimix all as "jsonl" although their
# record shapes differ, so `format` is a display label, not a shape contract.
# The reader additionally fails closed on record shapes it does not recognise.
SINGLE_PASS_ADAPTERS = frozenset({"claude", "zcode_v2"})


def single_pass_adapters() -> frozenset:
    """Adapter names whose transcript layout the single-pass reader mirrors."""
    return SINGLE_PASS_ADAPTERS


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
            elif env_info.get("count_via_adapter") and is_registered:
                # 通用 .jsonl 扫描对该格式失效（压缩流/需去重等），由适配器
                # 自己的发现逻辑计数（保持 limit=0 全量、免解压的快路径）。
                try:
                    from echolib._adapters import ADAPTER_REGISTRY
                    entry = ADAPTER_REGISTRY.get(env_info.get("adapter", ""))
                    session_count = len(entry["list_sessions"](limit=0) or []) if entry else 0
                except Exception as exc:
                    _log.warning("adapter-count scan failed for %s: %s", env_id, exc)
            else:
                jsonl_files = _fast_find_jsonl(root, max_depth=env_info.get("scan_depth", 4))
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

    # as_completed() yields in completion order, so ``results`` used to be a
    # race: whichever directory happened to finish first led the list. The order
    # is part of this function's output (smoke-multi-env prints it, herdr-event
    # consumes it), so it has to be a function of the inputs, not of thread
    # scheduling. Applied after the unknown-dir sweep, which appends too.
    order = {eid: i for i, (eid, _info, _reg) in enumerate(env_tasks)}
    results.sort(key=lambda r: (order.get(r.get("env_id"), len(order)),
                                str(r.get("name") or "")))

    home = Path.home()
    known_dirs = set()
    # Register each environment's *top-level* home directory, not its root: a
    # registered root can be nested (``~/.newmax/conversations``), but the
    # unknown-dir sweep below only ever walks ``home`` one level deep, so it can
    # only recognise ``~/.newmax``. The previous form took
    # ``expanduser(root).split("/")[0]``, which for an absolute path is the empty
    # string — so no registered environment was ever marked known and every one
    # of them was reported a second time as "discovered": 72 result rows for
    # 53 environments, with dimcode/kimix/dsh/proma/... duplicated.
    for e in list(ENV_REGISTRY.values()) + list(KNOWN_UNADAPTED.values()):
        root = Path(os.path.expanduser(e["root"]))
        try:
            rel = root.resolve().relative_to(home.resolve())
            known_dirs.add(str(home / rel.parts[0]))
        except (ValueError, OSError, IndexError):
            known_dirs.add(str(root))
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

    # as_completed() yields in completion order, so ``results`` used to be a
    # race: whichever directory happened to finish first led the list. The order
    # is part of this function's output (smoke-multi-env prints it, herdr-event
    # consumes it), so it has to be a function of the inputs, not of thread
    # scheduling. Registry order first, then the unknown-dir discoveries.
    order = {eid: i for i, (eid, _info, _reg) in enumerate(env_tasks)}
    results.sort(key=lambda r: (order.get(r.get("env_id"), len(order)),
                                str(r.get("name") or "")))
    return results
