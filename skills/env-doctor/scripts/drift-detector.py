#!/usr/bin/env python3
"""
drift-detector.py — 检测 skill 跨目录安装漂移

为什么原生做不到：Skill 漂移是跨目录概念，
单环境不感知其他目录的同一 skill 版本是否不同。

用法:
    python3 drift-detector.py              # 全量对比
    python3 drift-detector.py --json       # JSON 输出
    python3 drift-detector.py --threshold 5 # 大小差异阈值(百分比)

输出: JSON 或 human-readable
退出码: 0=无漂移, 1=有漂移
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

HOME = Path.home()


def find_skill_dirs() -> dict[str, Path]:
    """找到所有可能的 skill 安装根目录"""
    candidates = [
        ("claude", HOME / ".claude/skills"),
        ("agents", HOME / ".agents/skills"),
        ("codex", HOME / ".codex/skills"),
        ("grok", HOME / ".grok/skills"),
        ("kimi", HOME / ".kimi-code/skills"),
    ]
    result = {}
    for name, path in candidates:
        if path.exists() and path.is_dir():
            result[name] = path
    return result


def get_installed_skills(root: Path) -> dict[str, Path]:
    """返回 {skill_name: SKILL.md 路径}"""
    skills = {}
    for item in sorted(root.iterdir()):
        if item.is_dir():
            skill_md = item / "SKILL.md"
            if skill_md.exists():
                skills[item.name] = skill_md
    return skills


def get_file_hash(path: Path) -> str:
    """快速 hash (size + mtime + head 4KB md5)"""
    import hashlib
    stat = path.stat()
    h = hashlib.md5()
    h.update(f"{stat.st_size}:{stat.st_mtime}".encode())
    try:
        with open(path, "rb") as f:
            h.update(f.read(4096))
    except Exception:
        pass
    return h.hexdigest()[:12]


def detect_git_root(path: Path) -> Optional[str]:
    """检测某个路径是否在 git 仓库内"""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Detect skill installation drift across directories")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--threshold", type=int, default=5,
                        help="Size difference threshold in percent (default: 5)")
    args = parser.parse_args()

    roots = find_skill_dirs()
    if len(roots) < 2:
        result = {"status": "skip", "reason": "少于 2 个 skill 根目录，无法对比"}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("只有 1 个 skill 安装目录，漂移检测无意义")
        sys.exit(0)

    # 收集所有环境的 skills
    all_skills: dict[str, dict[str, Path]] = {}  # skill_name -> {env: path}
    for env_name, root in roots.items():
        for skill_name, skill_md in get_installed_skills(root).items():
            all_skills.setdefault(skill_name, {})[env_name] = skill_md

    # 分析漂移
    drifts = []
    consistent = []

    for skill_name, locations in sorted(all_skills.items()):
        if len(locations) < 2:
            continue

        # 比较 hash
        hashes = {}
        sizes = {}
        for env, path in locations.items():
            hashes[env] = get_file_hash(path)
            sizes[env] = path.stat().st_size

        unique_hashes = set(hashes.values())
        if len(unique_hashes) > 1:
            # 有漂移
            size_vals = list(sizes.values())
            max_diff_pct = max(abs(a - b) / max(b, 1) * 100 for a in size_vals for b in size_vals)
            if max_diff_pct >= args.threshold:
                drifts.append({
                    "skill": skill_name,
                    "locations": {env: str(path) for env, path in locations.items()},
                    "sizes": sizes,
                    "hashes": hashes,
                    "max_diff_pct": round(max_diff_pct, 1),
                })
            else:
                consistent.append(skill_name)
        else:
            consistent.append(skill_name)

    # 汇总
    total_shared = len(drifts) + len(consistent)

    report = {
        "status": "drift" if drifts else "clean",
        "environments_checked": list(roots.keys()),
        "total_skills_per_env": {env: len(get_installed_skills(root)) for env, root in roots.items()},
        "shared_skills": total_shared,
        "drift_count": len(drifts),
        "consistent_count": len(consistent),
        "drifts": drifts,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"=== Drift Detector ===")
        print(f"环境: {', '.join(roots.keys())}")
        print(f"各环境 skill 数: {report['total_skills_per_env']}")
        print(f"多环境共享: {total_shared}")
        print()
        if drifts:
            print(f"! DRIFTED ({len(drifts)}):")
            for d in drifts:
                sizes_str = ", ".join(f"{env}:{size}B" for env, size in d["sizes"].items())
                print(f"  {d['skill']}: {sizes_str} (max diff {d['max_diff_pct']}%)")
        else:
            print("OK no drift")

        if consistent:
            print(f"\nOK consistent ({len(consistent)}): {', '.join(consistent[:10])}{'...' if len(consistent) > 10 else ''}")

    sys.exit(1 if drifts else 0)


if __name__ == "__main__":
    main()
