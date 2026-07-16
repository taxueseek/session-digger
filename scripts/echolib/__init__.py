"""echolib — session-digger parsing and environment adapter library.

All public names re-exported for backward compat with ``import echolib``.
Submodules are loaded in strict dependency order (no circular imports).
"""
# ── Submodule imports in dependency order ──
# _helpers provides shared constants used by every other module
from echolib._helpers import (
    CLAUDE_DIR, NOISE_TYPES, KNOWN_TYPES, _NOISE_STRINGS,
    GROK_DIR, GROK_SEARCH_DB, KIMI_DIR, KIMI_CODE_DIR, CODEX_DIR, CURSOR_DIR,
    WORKBUDDY_DIR, TRAE_DIR, ZCODE_DIR, DIM_DIR,
    DIMCODE_DB_PATH, REASONIX_DIR,
    CODEX_ROLLOUT_RE, _codex_home, _codex_homes,
    _iter_jsonl, _strip_system_reminder, _extract_content_text, _match_call_results,
    normalize_session_path, session_in_cwd,
)

from echolib._contracts import (  # noqa: F811  — re-export type definitions
    SessionStats, ToolInfo, MessageInfo, ParsedSession,
    TrendPeriod, TrendResult, TrendDirection,
    ScanResult, AdapterEntry,
    GapReport, GapPattern, GapProposal,
)

from echolib._models import (
    Memory, MemoryStats, Record, SessionMeta, StaleScore,
    all_memory_dirs, estimate_tokens, iter_memories, memory_stats,
    parse_frontmatter, staleness_score,
    _HALF_LIVES,
)

from echolib._claude import (
    cli_error, detect_schema, extract_files_changed,
    extract_messages, extract_tools, find_subagent_files,
    iter_records, parse_int_or_die,
    session_stats,
    _normalize_timestamp, _reverse_find, _tool_key,
)
# Sub-helpers re-exported from _claude_index.py (project + index discovery layer).
from echolib._claude_index import (
    all_project_dirs, broad_list_claude_sessions, build_fallback_index,
    detect_agent_type, find_project_dir,
    list_sessions, load_index, resolve_project_root,
    _encode_project_path, _fast_find_jsonl, _sanitize_tsv, _scan_project_dir,
)
# Pydantic / model helper also reachable under its old import path.
from echolib._models import normalize_model_name as _normalize_model_name

from echolib._knowledge import (
    build_summary_index, extract_knowledge, has_fresh_summary,
    load_analysis_result, save_analysis_result, save_summary_index,
    _APPROVAL_PATTERNS, _CORRECTION_PATTERNS, _IMPERATIVE_PATTERNS,
    _TIER_ONCE_TTL, _TIER_PERIODIC_TTL, _URL_PATTERN, _VALUE_PATTERNS,
    _summary_path_for,
)

from echolib._adapters import (
    ADAPTER_REGISTRY, ENV_REGISTRY, KNOWN_UNADAPTED,
    register_adapter,  # function definition
    # Adapter functions — every environment's 5-method interface
    codex_extract_messages, codex_extract_tools, codex_list_sessions,
    codex_list_sessions_fallback, codex_session_path, codex_session_stats_dedicated,
    cursor_extract_messages, cursor_extract_tools, cursor_list_sessions,
    cursor_session_path, cursor_session_stats,
    cross_tool_list_sessions, cross_tool_session_stats,
    dim_extract_messages, dim_extract_tools, dim_list_sessions,
    dim_session_path, dim_session_stats,
    dimcode_extract_messages, dimcode_extract_tools, dimcode_list_sessions,
    dimcode_session_path, dimcode_session_stats,
    dispatch_extract_messages, dispatch_extract_tools, dispatch_resolve_agent,
    dispatch_session_stats,
    grok_aggregate_model_usage, grok_extract_tools, grok_family_usage_report,
    grok_list_sessions, grok_list_subagents, grok_session_path,
    kimi_code_extract_messages, kimi_code_extract_tools, kimi_code_list_sessions,
    kimi_code_session_path, kimi_code_session_stats,
    kimi_extract_messages, kimi_extract_tools, kimi_list_sessions,
    kimi_session_path, kimi_session_stats,
    reasonix_extract_messages, reasonix_extract_tools, reasonix_list_sessions,
    reasonix_session_path, reasonix_session_stats,
    scan_all_environments_parallel,
    trae_extract_messages, trae_extract_tools, trae_list_sessions,
    trae_session_path, trae_session_stats,
    universal_extract_messages, universal_extract_tools, universal_list_sessions,
    universal_session_path, universal_session_stats,
    workbuddy_extract_messages, workbuddy_extract_tools, workbuddy_list_sessions,
    workbuddy_session_path, workbuddy_session_stats,
    zcode_db_extract_messages, zcode_db_extract_tools, zcode_db_list_sessions,
    zcode_db_session_stats, zcode_extract_messages, zcode_extract_tools,
    zcode_list_sessions, zcode_session_path, zcode_session_stats,
    # Private API hooks (still re-exported for tests / advanced introspection.
    # Only intra-package helpers that must be importable at the top level
    # are listed here; pure implementation helpers live in their own submodules.)
    _SCHEMA_PROBE_CACHE,
    _detect_format_from_content,
    _empty_stats,
    _probe_schema, _schema_get_model, _schema_get_text,
    _schema_get_timestamp, _schema_is_assistant, _schema_is_role,
    _schema_is_tool_call, _schema_is_user,
)

# ── register_adapter() calls execute at module import time ──
# The 12 register_adapter("env", ...) calls are at the bottom of
# _adapters.py and execute when that module is imported above.

# ── Discover external adapter plugins ──
# Scans ~/.config/session-digger/adapters/*/adapter.py
# 注意：必须透过「模块对象」读 ADAPTER_REGISTRY（不要用 from import 的快照），
# 因为 register_adapter() 在执行期是 mutate 模块级 dict —— 快照会漏掉已注册的 claude/grok/…。
from echolib._adapter_discovery import discover_plugins as _discover_plugins
import echolib._adapters as _adapters_mod
for _env_id, _adapter in _discover_plugins().items():
    if _env_id not in _adapters_mod.ADAPTER_REGISTRY:
        _adapters_mod.ADAPTER_REGISTRY[_env_id] = _adapter

# Clean up helper names from module namespace
for _name in list(locals()):
    if _name.startswith("_disc") or _name.startswith("_env_") or _name.startswith("_adapter") or _name == "_name":
        locals().pop(_name, None)
