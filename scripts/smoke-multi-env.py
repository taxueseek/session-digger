#!/usr/bin/env python3
"""Multi-environment smoke for session-digger (stdlib only).

Prints adapted / unadapted / discovered status with adapter tier.
Does not fail the process when an environment is missing on disk.
Exit 1 only on import/hard errors.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import echolib  # noqa: E402


def main():
    print("=== session-digger multi-env smoke ===")
    print(f"adapters registered: {len(echolib.ADAPTER_REGISTRY)}")
    print(f"ENV_REGISTRY: {len(echolib.ENV_REGISTRY)}")
    print(f"KNOWN_UNADAPTED: {len(echolib.KNOWN_UNADAPTED)}")

    results = echolib.scan_all_environments_parallel()
    adapted = [r for r in results if r.get("status") == "adapted"]
    unadapted = [r for r in results if r.get("status") == "unadapted"]
    discovered = [r for r in results if r.get("status") == "discovered"]
    missing = [r for r in results if r.get("status") == "missing"]

    print(
        f"scan: adapted={len(adapted)} unadapted={len(unadapted)} "
        f"discovered={len(discovered)} missing={len(missing)}"
    )
    print("--- adapted (exists) ---")
    for r in sorted(adapted, key=lambda x: x.get("env_id", "")):
        if not r.get("exists"):
            continue
        usage = "usageOK" if echolib.tier_supports(r.get("adapter"), "usage") else "usageNO"
        print(
            f"  T{r.get('tier', '?')} {r.get('env_id'):12s} "
            f"n={r.get('session_count', 0):5} {usage}  {r.get('path')}"
        )

    print("--- unadapted present on disk ---")
    for r in sorted(unadapted, key=lambda x: x.get("env_id", "")):
        if r.get("exists"):
            print(f"  T{r.get('tier', '?')} {r.get('env_id'):12s} n={r.get('session_count', 0)}")

    if discovered:
        print("--- discovered ---")
        for r in discovered[:15]:
            print(f"  {r.get('env_id')} n={r.get('session_count')} @ {r.get('path')}")

    # Per-adapter list + one stats sample when sessions exist
    print("--- registry sample (limit 2 sessions / agent) ---")
    for name, entry in sorted(echolib.ADAPTER_REGISTRY.items()):
        if name == "universal":
            continue
        try:
            sessions = entry["list_sessions"](limit=2) or []
        except Exception as exc:
            print(f"  ! {name}: list_sessions error: {exc}")
            continue
        print(f"  {name}: {len(sessions)} listed (cap 2) tier=T{echolib.adapter_tier(name)}")
        for s in sessions[:1]:
            # SessionMeta uses full_path; dict adapters historically used path.
            if isinstance(s, dict):
                path = s.get("full_path") or s.get("path") or s.get("session_id") or s.get("id")
            else:
                path = (
                    getattr(s, "full_path", None)
                    or getattr(s, "path", None)
                    or getattr(s, "session_id", None)
                )
            if not path:
                print("      (no path/session_id on list entry)")
                continue
            try:
                # URI schemes (dimcode://) go through adapter session_stats directly
                if str(path).startswith("dimcode:") or str(path).startswith("dimcode://"):
                    stats = entry["session_stats"](path)
                    routed = name
                else:
                    routed = echolib.dispatch_resolve_agent(path)
                    stats = echolib.dispatch_session_stats(path)
                um = stats.get("user_messages") if isinstance(stats, dict) else None
                tok = stats.get("total_tokens") if isinstance(stats, dict) else None
                print(
                    f"      route={routed} user_msgs={um} total_tokens={tok} "
                    f"cache_hit={(stats.get('cache_hit_rate') if isinstance(stats, dict) else None)}"
                )
                if routed == "universal" and name != "universal":
                    print(f"      WARN: expected {name}, got universal")
            except Exception as exc:
                print(f"      stats error: {exc}")

    # Family aggregates when available
    print("--- family usage (if data) ---")
    for fn_name in ("grok_aggregate_model_usage", "zcode_aggregate_model_usage"):
        fn = getattr(echolib, fn_name, None)
        if not fn:
            continue
        try:
            # limit kwargs differ; try common signatures
            try:
                data = fn(limit=3)
            except TypeError:
                data = fn()
            n = len(data) if isinstance(data, dict) else "?"
            print(f"  {fn_name}: models={n}")
        except Exception as exc:
            print(f"  {fn_name}: skip ({exc})")

    print("=== smoke done (exit 0; missing envs are not failures) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
