#!/usr/bin/env python3
"""
history-match.py — 从已知问题模式库匹配当前环境状态。

为什么需要脚本：需要读取 session-digger 的会话索引，
原生命令无法访问历史数据。

用法:
    python3 history-match.py                    # 输出全库（供模型参考）
    python3 history-match.py --env claude       # 只输出与某环境相关的
    python3 history-match.py --severity warning # 只输出某级别
    python3 history-match.py --json             # JSON 输出

退出码: 0
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
KNOWN_ISSUES_PATH = SCRIPT_DIR / "known-issues.json"

def main():
    parser = argparse.ArgumentParser(description="从已知问题模式库匹配当前环境状态")
    parser.add_argument("--env", help="只输出与某环境相关的模式")
    parser.add_argument("--severity", choices=["critical", "warning", "info"],
                        help="只输出某级别")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if not KNOWN_ISSUES_PATH.exists():
        print("known-issues.json not found", file=sys.stderr)
        sys.exit(1)

    with open(KNOWN_ISSUES_PATH) as f:
        data = json.load(f)

    patterns = data.get("issue_patterns", [])
    results = []

    for p in patterns:
        if args.severity and p["severity"] != args.severity:
            continue
        if args.env:
            # 匹配：pattern 的 id 或 description 包含环境名
            env_lower = args.env.lower()
            text = (p["id"] + p["title"] + p["description"]).lower()
            if env_lower not in text and "cross" not in text and "known" not in text:
                continue
        results.append(p)

    if args.json:
        print(json.dumps({
            "source": str(KNOWN_ISSUES_PATH),
            "total_in_library": len(patterns),
            "matched": len(results),
            "patterns": results
        }, ensure_ascii=False, indent=2))
    else:
        print(f"=== Known Issue Patterns (from {KNOWN_ISSUES_PATH.name}) ===")
        print(f"   Library: {len(patterns)} patterns, matched: {len(results)}")
        print()
        for p in results:
            sev_label = p["severity"].upper()
            print(f"  [{sev_label}] [{p['id']}] {p['title']}")
            print(f"     {p['description'][:120]}")
            if p.get("suggestion"):
                print(f"     → {p['suggestion']}")
            print()

if __name__ == "__main__":
    main()
