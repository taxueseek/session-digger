#!/usr/bin/env python3
"""
wechat-digger doctor / health check.

Checks path resolution, source readiness, index integrity, privacy hardcodes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from paths import (
    acquire_dir,
    acquire_tool,
    default_data_root,
    default_index_path,
    digger_python,
    digger_root,
    private_vault_dir,
    vault_cli_path,
)
from runtime_contract import check_python
from source_registry import detect_summary

try:
    from acquire_bridge import inventory as acquire_inventory
except Exception:  # pragma: no cover
    acquire_inventory = None


HARDCODE_PATTERNS = [
    re.compile(r"/Users/[A-Za-z0-9._-]+/"),
]


def check_hardcodes(root: Path) -> list[dict]:
    issues = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in {".py", ".md", ".json"}:
            continue
        # skip vendored acquire stack + fixtures/cache
        if "fixtures" in p.parts or "__pycache__" in p.parts:
            continue
        if "acquire" in p.parts and p.name != "README.md":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in HARDCODE_PATTERNS:
            if pat.search(text):
                if "禁止硬编码" in text or "Users/<" in text or "`/Users/" in text:
                    continue
                if "你的电脑用户名" in text or "<user>" in text:
                    continue
                issues.append({"file": str(p.relative_to(root)), "pattern": pat.pattern})
                break
    return issues


def doctor(allow_fixture: bool = True) -> dict:
    root = digger_root()
    src = detect_summary(allow_fixture=allow_fixture)
    index_path = default_index_path()
    index_stats = None
    if index_path.exists():
        try:
            from index_builder import connect, stats

            index_stats = stats(connect(index_path))
            # 全史索引健康度：full_history 标记 + 最近一次兜底异常（持久化在 meta，跨进程可见）
            con = connect(index_path)
            try:
                for k, out_key in (("full_history", "full_history"), ("fullhist_last_error", "fullhist_last_error")):
                    row = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
                    index_stats[out_key] = row[0] if row else None
            finally:
                con.close()
        except Exception as e:
            index_stats = {"error": str(e)}

    hardcodes = check_hardcodes(root)
    acq = acquire_dir()
    vcli = vault_cli_path()
    bundled = bool(acq and acq.name == "acquire")
    inv = acquire_inventory() if acquire_inventory else {}

    # decrypt/keys 在公开包按契约不携带（SYNC 铁律）——文件存在才参与判定：
    # 内部栈全要求，公开包自动豁免，doctor 的 ok 语义两边一致（缺失如实标注）
    _acq_dir = acquire_dir()
    tools_ok = all(
        acquire_tool(n) is not None
        for n in ("vault_cli", "decrypt", "keys")
        if n == "vault_cli" or (_acq_dir / f"{n}.py").is_file()
    )

    py = digger_python()
    deps = {"python": py, "Crypto": False, "zstandard": False}
    try:
        import subprocess as _sp

        # Probe each dependency with the import the code actually performs.
        # The previous single probe was ``from Crypto.Cipher import AES``, which
        # the abandoned pycrypto 2.6.1 satisfies — so ``doctor`` reported
        # acquire_deps OK on an interpreter where every image/decrypt command
        # then died on ``Crypto.Util.Padding`` (pycryptodome-only). A check that
        # passes on a package the code cannot use is worse than no check.
        probes = {
            "Crypto": "from Crypto.Cipher import AES; from Crypto.Util.Padding import unpad",
            "zstandard": "import zstandard",
        }
        failures = []
        for name, code in probes.items():
            r = _sp.run([py, "-c", code], capture_output=True, text=True, timeout=15)
            deps[name] = r.returncode == 0
            if r.returncode != 0:
                failures.append((r.stderr or r.stdout or "").strip())
        if failures:
            deps["detail"] = " | ".join(failures)[:200]
            if any("Util.Padding" in f or "No module named 'Crypto.Util'" in f for f in failures):
                deps["cause"] = "Crypto 是 pycrypto（缺 Util.Padding），需要 pycryptodome"
    except Exception as e:
        deps["detail"] = str(e)

    # Runtime contract: report the *running* interpreter against the declared
    # floor. Note this reflects the interpreter executing wd.py, which may
    # differ from `py` above (that one is the project venv used for the
    # bundled acquire stack).
    runtime = check_python()

    checks = [
        {"id": "skill_root", "ok": (root / "scripts" / "wd.py").is_file(), "detail": str(root)},
        {
            "id": "runtime_contract",
            "ok": runtime["supported"],
            "detail": runtime,
        },
        {
            "id": "acquire_bundled",
            "ok": bundled and tools_ok,
            "detail": {
                "dir": str(acq) if acq else None,
                "bundled": bundled,
                "vault_cli": str(vcli) if vcli else None,
                "tools_ready": tools_ok,
            },
        },
        {
            "id": "self_contained",
            "ok": bundled and tools_ok,
            "detail": "no external wechat-local-vault required when bundled",
        },
        {
            "id": "acquire_deps",
            "ok": bool(deps.get("Crypto") and deps.get("zstandard")),
            "detail": deps if deps.get("Crypto") else {
                **deps,
                "action": "bash scripts/setup_deps.sh  # creates .venv with pycryptodome+zstandard",
            },
        },
        {
            "id": "private_vault",
            "ok": True,
            "detail": {
                "dir": str(private_vault_dir()),
                "decrypted": (private_vault_dir() / "decrypted" / "current").exists(),
                "keys_config": inv.get("keys_config_exists"),
            },
        },
        {"id": "primary_source", "ok": src.get("primary") is not None, "detail": src.get("primary_detail")},
        {
            "id": "wx_engine",
            "ok": True,  # optional; never fails overall doctor
            "detail": src.get("wx") or {"ready": False},
        },
        {
            "id": "engines_by_op",
            "ok": True,
            "detail": src.get("engines_by_op") or {},
        },
        {"id": "data_root_writable", "ok": True, "detail": str(default_data_root())},
        {"id": "index", "ok": True, "detail": index_stats or f"not built yet: {index_path}"},
        {"id": "no_hardcoded_home", "ok": len(hardcodes) == 0, "detail": hardcodes or "clean"},
        {
            "id": "env_override",
            "ok": True,
            "detail": {
                "WECHAT_DIGGER_ROOT": os.environ.get("WECHAT_DIGGER_ROOT"),
                "WECHAT_ACQUIRE_DIR": os.environ.get("WECHAT_ACQUIRE_DIR"),
                "WECHAT_VAULT_ROOT": os.environ.get("WECHAT_VAULT_ROOT"),
                "WECHAT_DIGGER_DATA": os.environ.get("WECHAT_DIGGER_DATA"),
            },
        },
    ]
    try:
        default_data_root().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        for c in checks:
            if c["id"] == "data_root_writable":
                c["ok"] = False
                c["detail"] = str(e)

    failed = [c for c in checks if not c["ok"]]
    return {
        "ok": len(failed) == 0,
        "checks": checks,
        "sources": src,
        "acquire": inv,
        "failed": [c["id"] for c in failed],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    report = doctor()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("wechat-digger doctor")
        print(f"overall: {'OK' if report['ok'] else 'ISSUES'}")
        for c in report["checks"]:
            mark = "✓" if c["ok"] else "✗"
            print(f"  {mark} {c['id']}: {c['detail']}")
        print("sources primary:", report["sources"].get("primary"))
        print("available:", ", ".join(report["sources"].get("available") or []) or "(none)")
    sys.exit(0 if report["ok"] else 2)


if __name__ == "__main__":
    main()
