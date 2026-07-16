#!/usr/bin/env python3
"""
zcode-adapter.py — ZCode 会话数据适配器（echolib 包装层）。

所有查询逻辑由 echolib 的 zcode_db_* 函数实现。
本文件只提供 CLI 入口，不做数据查询。

用法:
  zcode-adapter.py list-sessions [--limit N] [--keyword K]
  zcode-adapter.py session-stats <session_id>
  zcode-adapter.py extract-tools <session_id> [--limit N]
  zcode-adapter.py extract-messages <session_id> [--role user|assistant|both] [--limit N]

v1.1 — 包装层，查询逻辑移至 echolib.py
"""

import argparse
import json
import sys
from pathlib import Path

# echolib lives in the same scripts/ directory
_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR))
import echolib


def main():
    parser = argparse.ArgumentParser(description="ZCode session data adapter (echolib wrapper)")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list-sessions")
    p_list.add_argument("--limit", type=int, default=200)
    p_list.add_argument("--keyword", default="")

    p_stats = sub.add_parser("session-stats")
    p_stats.add_argument("session_id")

    p_tools = sub.add_parser("extract-tools")
    p_tools.add_argument("session_id")
    p_tools.add_argument("--limit", type=int, default=30)

    p_msgs = sub.add_parser("extract-messages")
    p_msgs.add_argument("session_id")
    p_msgs.add_argument("--role", default="both", choices=["user", "assistant", "both"])
    p_msgs.add_argument("--limit", type=int, default=5)

    args = parser.parse_args()

    if args.command == "list-sessions":
        result = echolib.zcode_db_list_sessions(limit=args.limit, keyword=args.keyword)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "session-stats":
        result = echolib.zcode_db_session_stats(args.session_id)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "extract-tools":
        result = echolib.zcode_db_extract_tools(args.session_id, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "extract-messages":
        result = echolib.zcode_db_extract_messages(args.session_id, role=args.role, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
