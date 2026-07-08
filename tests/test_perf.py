#!/usr/bin/env python3
"""Performance benchmark for session-digger after refactoring."""
import sys
import time
import glob
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
import echolib

# Collect 20 session files from different environments
files = []
for pattern in [
    str(Path.home() / ".grok" / "sessions" / "*" / "*" / "chat_history.jsonl"),
    str(Path.home() / ".zcode" / "cli" / "agents" / "sess_*" / "agent_*" / "transcript.jsonl"),
    str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl"),
]:
    found = glob.glob(pattern, recursive=True)
    files.extend(found[:8])
files = files[:20]
print(f"Testing {len(files)} session files...")

# Test 1: dispatch_session_stats
t0 = time.time()
total_stats = 0
for f in files:
    stats = echolib.dispatch_session_stats(f)
    total_stats += stats.get("user_messages", 0) + stats.get("assistant_messages", 0)
elapsed = time.time() - t0
print(f"dispatch_session_stats: {elapsed:.3f}s ({len(files)} files, {total_stats} messages)")

# Test 2: dispatch_extract_messages
t0 = time.time()
total_msgs = 0
for f in files:
    msgs = list(echolib.dispatch_extract_messages(f, limit=5))
    total_msgs += len(msgs)
elapsed = time.time() - t0
print(f"dispatch_extract_messages: {elapsed:.3f}s ({total_msgs} messages extracted)")

# Test 3: dispatch_extract_tools
t0 = time.time()
total_tools = 0
for f in files:
    tools = list(echolib.dispatch_extract_tools(f, limit=5))
    total_tools += len(tools)
elapsed = time.time() - t0
print(f"dispatch_extract_tools: {elapsed:.3f}s ({total_tools} tools extracted)")

# Test 4: scan_all_environments_parallel
t0 = time.time()
results = echolib.scan_all_environments_parallel()
elapsed = time.time() - t0
print(f"scan_all_environments_parallel: {elapsed:.3f}s ({len(results)} envs)")

# Test 5: _iter_jsonl vs raw open (micro-benchmark)
import json
test_file = files[0] if files else None
if test_file and Path(test_file).exists():
    # Raw open
    t0 = time.time()
    for _ in range(50):
        count = 0
        try:
            with open(test_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                        count += 1
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            pass
    raw_elapsed = time.time() - t0

    # _iter_jsonl
    t0 = time.time()
    for _ in range(50):
        count = sum(1 for _ in echolib._iter_jsonl(test_file))
    iter_elapsed = time.time() - t0

    print(f"_iter_jsonl micro-bench (50x): raw={raw_elapsed:.3f}s, helper={iter_elapsed:.3f}s, overhead={((iter_elapsed/raw_elapsed-1)*100):.1f}%")

print(f"\necholib.py line count: {sum(1 for _ in open(PLUGIN_ROOT / 'scripts' / 'echolib.py'))}")
