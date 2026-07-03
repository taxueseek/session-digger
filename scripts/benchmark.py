#!/usr/bin/env python3
"""
benchmark.py — Performance benchmarks for session-digger conversation data processing.

Measures:
1. File discovery speed across environments
2. JSON parsing throughput
3. Session stats computation
4. Cross-environment scanning
5. Memory usage
"""

import json
import time
import tracemalloc
from pathlib import Path
import sys
import os

# Add scripts directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))


def benchmark_file_discovery():
    """Benchmark 1: File discovery speed across environments."""
    print("\n" + "=" * 60)
    print("Benchmark 1: File Discovery Speed")
    print("=" * 60)
    
    envs = [
        Path.home() / ".codex/sessions",
        Path.home() / ".workbuddy/projects",
        Path.home() / ".trae-cn/memory/projects",
        Path.home() / ".claude/projects",
        Path.home() / ".grok/sessions",
        Path.home() / ".kimi-code/sessions",
    ]
    
    start = time.time()
    count = 0
    total_size = 0
    env_counts = {}
    
    for env_dir in envs:
        env_name = env_dir.parent.name if env_dir.exists() else "unknown"
        if env_dir.exists():
            env_count = 0
            for f in env_dir.rglob("*.jsonl"):
                count += 1
                env_count += 1
                total_size += f.stat().st_size
            env_counts[env_name] = env_count
            print(f"  {env_name:15s}: {env_count:5d} files")
    
    elapsed = time.time() - start
    print(f"\n{'Total':15s}: {count:5d} files, {total_size/1e6:.1f} MB")
    print(f"Time: {elapsed:.3f}s ({count/elapsed:.0f} files/sec)")
    
    return count, total_size, elapsed


def benchmark_json_parsing():
    """Benchmark 2: JSON parsing speed on largest file."""
    print("\n" + "=" * 60)
    print("Benchmark 2: JSON Parsing Speed")
    print("=" * 60)
    
    # Find largest file across all environments
    envs = [
        Path.home() / ".codex/sessions",
        Path.home() / ".workbuddy/projects",
        Path.home() / ".trae-cn/memory/projects",
        Path.home() / ".claude/projects",
        Path.home() / ".grok/sessions",
        Path.home() / ".kimi-code/sessions",
    ]
    
    largest = None
    largest_size = 0
    
    for env_dir in envs:
        if env_dir.exists():
            for f in env_dir.rglob("*.jsonl"):
                size = f.stat().st_size
                if size > largest_size:
                    largest_size = size
                    largest = f
    
    if not largest:
        print("No JSONL files found!")
        return 0, 0, 0
    
    print(f"Largest file: {largest.name} ({largest_size/1e6:.1f} MB)")
    print(f"  Path: {largest}")
    
    start = time.time()
    lines = 0
    errors = 0
    
    with open(largest) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if line:
                try:
                    json.loads(line)
                    lines += 1
                except json.JSONDecodeError as e:
                    errors += 1
                    if errors <= 3:
                        print(f"  JSON error at line {line_num}: {str(e)[:100]}")
    
    elapsed = time.time() - start
    
    if elapsed > 0:
        print(f"\nParsed: {lines} records ({errors} errors)")
        print(f"Time: {elapsed:.3f}s")
        print(f"Throughput: {lines/elapsed:.0f} records/sec, {largest_size/elapsed/1e6:.1f} MB/sec")
    else:
        print("No records parsed (file may be empty)")
    
    return lines, largest_size, elapsed


def benchmark_session_stats():
    """Benchmark 3: Full session stats computation."""
    print("\n" + "=" * 60)
    print("Benchmark 3: Session Stats Computation")
    print("=" * 60)
    
    # Import from echolib (env-adapters merged into echolib)
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    
    try:
        import echolib
        codex_session_stats = echolib.ADAPTER_REGISTRY["codex"]["session_stats"]
    except Exception as e:
        print(f"ERROR: Could not import codex adapter: {e}")
        return 0, 0
    
    codex_dir = Path.home() / ".codex/sessions"
    sessions = list(codex_dir.rglob("rollout-*.jsonl"))
    
    if not sessions:
        print("No Codex rollout sessions found!")
        return 0, 0
    
    print(f"Processing {len(sessions)} Codex sessions...")
    
    start = time.time()
    success_count = 0
    error_count = 0
    for s in sessions:
        try:
            codex_session_stats(str(s))
            success_count += 1
        except Exception as e:
            error_count += 1
            if error_count <= 3:
                print(f"  Error processing {s.name}: {e}")
    elapsed = time.time() - start
    
    if elapsed > 0:
        print(f"Processed: {success_count} sessions ({error_count} errors)")
        print(f"Time: {elapsed:.3f}s ({success_count/elapsed:.1f} sessions/sec)")
    else:
        print("No sessions processed")
    
    return success_count, elapsed


def benchmark_universal_scan():
    """Benchmark 4: Cross-environment full scan."""
    print("\n" + "=" * 60)
    print("Benchmark 4: Universal Cross-Environment Scan")
    print("=" * 60)
    
    envs = [
        Path.home() / ".codex/sessions",
        Path.home() / ".workbuddy/projects",
        Path.home() / ".trae-cn/memory/projects",
        Path.home() / ".claude/projects",
        Path.home() / ".grok/sessions",
        Path.home() / ".kimi-code/sessions",
    ]
    
    start = time.time()
    jsonl_count = 0
    total_size = 0
    env_stats = {}
    
    for env_dir in envs:
        env_name = env_dir.parent.name
        if env_dir.exists():
            files = list(env_dir.rglob("*.jsonl"))
            env_size = sum(f.stat().st_size for f in files)
            jsonl_count += len(files)
            total_size += env_size
            env_stats[env_name] = {'count': len(files), 'size': env_size}
            print(f"  {env_name:15s}: {len(files):5d} files, {env_size/1e6:.1f} MB")
    
    elapsed = time.time() - start
    
    print(f"\n{'Total':15s}: {jsonl_count:5d} files, {total_size/1e6:.1f} MB")
    print(f"Time: {elapsed:.3f}s ({jsonl_count/elapsed:.0f} files/sec)")
    
    return jsonl_count, total_size, elapsed


def benchmark_memory_usage():
    """Benchmark 5: Memory usage during full scan."""
    print("\n" + "=" * 60)
    print("Benchmark 5: Memory Usage")
    print("=" * 60)
    
    tracemalloc.start()
    
    # Simulate a full scan operation
    envs = [
        Path.home() / ".codex/sessions",
        Path.home() / ".workbuddy/projects",
        Path.home() / ".trae-cn/memory/projects",
        Path.home() / ".claude/projects",
        Path.home() / ".grok/sessions",
        Path.home() / ".kimi-code/sessions",
    ]
    
    all_files = []
    for env_dir in envs:
        if env_dir.exists():
            all_files.extend(list(env_dir.rglob("*.jsonl")))
    
    # Parse all JSON files to measure peak memory
    parsed_count = 0
    for f in all_files:
        try:
            with open(f) as fh:
                for line in fh:
                    if line.strip():
                        json.loads(line)
                        parsed_count += 1
                        if parsed_count % 10000 == 0:
                            # Print memory at checkpoints
                            current, peak = tracemalloc.get_traced_memory()
                            print(f"  {parsed_count:6d} records: current={current/1e6:.1f}MB, peak={peak/1e6:.1f}MB")
        except Exception:
            continue
    
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    print(f"\nFinal: {parsed_count} records processed")
    print(f"Current memory: {current/1e6:.1f} MB")
    print(f"Peak memory: {peak/1e6:.1f} MB")
    print(f"Memory per record: {current/parsed_count/1024:.1f} KB" if parsed_count > 0 else "No records")
    
    return current, peak, parsed_count


def main():
    """Run all benchmarks."""
    print("=" * 60)
    print("ECHO-SLEUTH PYTHON PERFORMANCE BENCHMARKS")
    print("=" * 60)
    print(f"Python version: {sys.version}")
    print(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Track overall timing
    overall_start = time.time()
    
    # Run benchmarks
    file_count, total_size, discovery_time = benchmark_file_discovery()
    records, largest_size, parse_time = benchmark_json_parsing()
    session_count, stats_time = benchmark_session_stats()
    scan_count, scan_size, scan_time = benchmark_universal_scan()
    current_mem, peak_mem, processed = benchmark_memory_usage()
    
    # Summary
    overall_time = time.time() - overall_start
    
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"File discovery:      {file_count:6d} files, {total_size/1e6:6.1f} MB in {discovery_time:6.3f}s")
    print(f"JSON parsing:        {records:6d} records from largest file in {parse_time:6.3f}s")
    print(f"Session stats:       {session_count:6d} sessions in {stats_time:6.3f}s")
    print(f"Universal scan:      {scan_count:6d} files in {scan_time:6.3f}s")
    print(f"Peak memory:         {peak_mem/1e6:6.1f} MB")
    print(f"Total time:          {overall_time:6.3f}s")
    
    # Performance analysis
    print("\n" + "=" * 60)
    print("PERFORMANCE ANALYSIS")
    print("=" * 60)
    
    if parse_time > 0:
        print(f"JSON parsing throughput: {records/parse_time:.0f} records/sec")
        print(f"JSON parsing bandwidth: {largest_size/parse_time/1e6:.1f} MB/sec")
    
    if stats_time > 0:
        print(f"Session stats throughput: {session_count/stats_time:.1f} sessions/sec")
    
    if scan_time > 0:
        print(f"File discovery throughput: {file_count/scan_time:.0f} files/sec")
    
    # Theoretical Rust comparison
    print("\n" + "=" * 60)
    print("THEORETICAL RUST COMPARISON")
    print("=" * 60)
    print("Expected speedup factors based on benchmark data:")
    print("")
    print("1. JSON Parsing (simd-json vs json.loads):")
    if parse_time > 0:
        rust_json_speedup = 8  # simd-json typically 5-10x faster
        print(f"   Python: {records/parse_time:.0f} records/sec")
        print(f"   Rust (simd-json): ~{records/parse_time*rust_json_speedup:.0f} records/sec (estimated {rust_json_speedup}x)")
    print("")
    print("2. File Discovery (walkdir vs rglob):")
    if discovery_time > 0:
        rust_fs_speedup = 3  # walkdir with parallel iteration
        print(f"   Python: {file_count/discovery_time:.0f} files/sec")
        print(f"   Rust (walkdir): ~{file_count/discovery_time*rust_fs_speedup:.0f} files/sec (estimated {rust_fs_speedup}x)")
    print("")
    print("3. Memory Efficiency:")
    print(f"   Python peak: {peak_mem/1e6:.1f} MB")
    print(f"   Rust expected: ~{peak_mem/1e6/3:.1f} MB (estimated 3x less)")
    print("")
    print("4. Overall Speedup Estimate:")
    if overall_time > 0:
        overall_speedup = 5  # Conservative estimate
        print(f"   Python total: {overall_time:.2f}s")
        print(f"   Rust expected: ~{overall_time/overall_speedup:.2f}s (estimated {overall_speedup}x)")


if __name__ == "__main__":
    main()
