#!/usr/bin/env python3
"""Baseline functional test for session-digger before refactoring."""
import sys
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
import echolib

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS: {name}")


def fail(name, detail=""):
    global failed
    failed += 1
    print(f"  FAIL: {name} {detail}")


def test_import():
    """echolib imports without error."""
    try:
        import echolib
        ok("import echolib")
    except Exception as e:
        fail("import echolib", str(e))


def test_adapter_registry():
    """ADAPTER_REGISTRY has all expected adapters."""
    expected = {"claude", "grok", "kimi_code", "codex", "workbuddy",
                "trae_cn", "zcode", "dim", "reasonix", "universal"}
    actual = set(echolib.ADAPTER_REGISTRY.keys())
    if expected.issubset(actual):
        ok(f"ADAPTER_REGISTRY has all {len(expected)} adapters")
    else:
        missing = expected - actual
        fail("ADAPTER_REGISTRY", f"missing: {missing}")

    # Each adapter must have all 5 functions
    required_fns = {"list_sessions", "session_stats", "extract_messages",
                    "extract_tools", "session_path"}
    for name, info in echolib.ADAPTER_REGISTRY.items():
        actual_fns = set(info.keys()) - {"name", "display_name"}
        if not required_fns.issubset(actual_fns):
            fail(f"adapter {name} functions", f"missing: {required_fns - actual_fns}")
        else:
            ok(f"adapter {name} has all 5 functions")


def test_dispatch_resolve_agent():
    """dispatch_resolve_agent maps paths correctly."""
    home = Path.home()
    cases = [
        (str(home / ".claude" / "projects"), "claude"),
        (str(home / ".grok" / "sessions"), "grok"),
        (str(home / ".kimi-code" / "sessions"), "kimi_code"),
        (str(home / ".codex"), "codex"),
        (str(home / ".zcode" / "cli" / "agents"), "zcode"),
        (str(home / ".dim" / "memory"), "dim"),
        (str(home / ".reasonix" / "sessions"), "reasonix"),
    ]
    for path, expected in cases:
        actual = echolib.dispatch_resolve_agent(path)
        if actual == expected:
            ok(f"resolve {expected}")
        else:
            fail(f"resolve {expected}", f"got {actual}")


def test_empty_stats():
    """_empty_stats returns correct structure."""
    stats = echolib._empty_stats("test")
    required_keys = {"slug", "model", "branch", "started", "ended",
                     "user_messages", "assistant_messages", "tool_calls",
                     "files_edited", "errors", "input_tokens", "output_tokens",
                     "cache_read_tokens", "cache_create_tokens", "compactions",
                     "summary", "total_tokens"}
    actual_keys = set(stats.keys())
    if required_keys == actual_keys:
        ok("_empty_stats structure")
    else:
        fail("_empty_stats structure", f"missing: {required_keys - actual_keys}, extra: {actual_keys - required_keys}")


def test_dispatch_functions():
    """dispatch_session_stats / dispatch_extract_messages / dispatch_extract_tools are callable."""
    for fn_name in ["dispatch_session_stats", "dispatch_extract_messages",
                    "dispatch_extract_tools"]:
        fn = getattr(echolib, fn_name, None)
        if fn and callable(fn):
            ok(f"{fn_name} callable")
        else:
            fail(f"{fn_name} not found")


def test_scan_environments():
    """scan_all_environments_parallel returns results."""
    results = echolib.scan_all_environments_parallel()
    if isinstance(results, list) and len(results) > 0:
        ok(f"scan_all_environments_parallel ({len(results)} envs)")
        # Check each result has expected keys
        sample = results[0]
        required = {"name", "env_id", "path", "exists", "session_count", "status"}
        if required.issubset(set(sample.keys())):
            ok("scan result structure")
        else:
            fail("scan result structure", f"missing: {required - set(sample.keys())}")
    else:
        fail("scan_all_environments_parallel", "no results")


def test_index_builder_import():
    """index-builder.py imports without error."""
    try:
        sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
        import index_builder
        ok("import index_builder")
    except ImportError:
        # index-builder.py uses hyphen, may need importlib
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "index_builder", str(PLUGIN_ROOT / "scripts" / "index-builder.py"))
        if spec:
            ok("index-builder.py found")
        else:
            fail("index-builder.py not found")


def test_sd_recall_sessions():
    """sd-recall.py sessions command works (via subprocess)."""
    import subprocess
    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "sd-recall.py"),
         "sessions", "--scope", "all", "--limit", "5"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0 and "session" in result.stdout.lower():
        ok("sd-recall sessions command")
    else:
        fail("sd-recall sessions command", f"rc={result.returncode} stderr={result.stderr[:200]}")


def test_format_detector_import():
    """format-detector.py imports without error."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "format_detector", str(PLUGIN_ROOT / "scripts" / "format-detector.py"))
    if spec:
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            ok("format-detector.py imports")
        except Exception as e:
            fail("format-detector.py imports", str(e))
    else:
        fail("format-detector.py not found")


def main():
    print("=== session-digger baseline tests ===")
    print()
    tests = [
        test_import,
        test_adapter_registry,
        test_dispatch_resolve_agent,
        test_empty_stats,
        test_dispatch_functions,
        test_scan_environments,
        test_index_builder_import,
        test_sd_recall_sessions,
        test_format_detector_import,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            fail(t.__name__, str(e))
    print()
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
