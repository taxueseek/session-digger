"""echolib — session-digger parsing and environment adapter library.

All public names re-exported for backward compat with ``import echolib``.
Submodules are loaded in strict dependency order (no circular imports).
"""
# ── Submodule imports in dependency order ──
# _helpers provides shared constants used by every other module
from echolib._helpers import (
    CLAUDE_DIR, NOISE_TYPES, KNOWN_TYPES, _NOISE_STRINGS,
    GROK_DIR, KIMI_DIR, KIMI_CODE_DIR, CODEX_DIR,
    WORKBUDDY_DIR, TRAE_DIR, ZCODE_DIR, DIM_DIR,
    DIMCODE_DB_PATH, REASONIX_DIR,
    _iter_jsonl, _strip_system_reminder, _extract_content_text, _match_call_results,
)

from echolib._contracts import (  # noqa: F811  — re-export type definitions
    SessionStats, ToolInfo, MessageInfo, ParsedSession,
    TrendPeriod, ScanResult, AdapterEntry,
)

from echolib._models import (
    Memory, MemoryStats, Record, SessionMeta, StaleScore,
    all_memory_dirs, estimate_tokens, iter_memories, memory_stats,
    parse_frontmatter, staleness_score,
    _HALF_LIVES,
)

from echolib._claude import (
    all_project_dirs, broad_list_claude_sessions, build_fallback_index,
    cli_error, detect_agent_type, detect_schema, extract_files_changed,
    extract_messages, extract_tools, find_project_dir, find_subagent_files,
    iter_records, list_sessions, load_index, parse_int_or_die,
    resolve_project_root, session_stats,
    _encode_project_path, _fast_find_jsonl, _normalize_model_name,
    _normalize_timestamp, _reverse_find, _sanitize_tsv, _scan_project_dir, _tool_key,
)

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
    cross_tool_list_sessions, cross_tool_session_stats,
    dim_extract_messages, dim_extract_tools, dim_list_sessions,
    dim_session_path, dim_session_stats,
    dimcode_extract_messages, dimcode_extract_tools, dimcode_list_sessions,
    dimcode_session_path, dimcode_session_stats,
    dispatch_extract_messages, dispatch_extract_tools, dispatch_resolve_agent,
    dispatch_session_stats,
    grok_extract_tools, grok_list_sessions, grok_session_path,
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
    # Private API hooks
    _SCHEMA_PROBE_CACHE, _codex_quick_scan, _decode_grok_cwd,
    _detect_format_from_content, _dimcode_db_connect, _dimcode_normalize_session_id,
    _empty_stats, _encode_grok_cwd, _find_codex_rollout, _grok_extract_messages,
    _grok_join_content, _grok_resolve_path, _grok_session_stats,
    _kimi_code_resolve_path, _probe_schema, _schema_get_model, _schema_get_text,
    _schema_get_timestamp, _schema_is_assistant, _schema_is_role,
    _schema_is_tool_call, _schema_is_user, _trae_extract_intents,
    _universal_quick_scan, _workbuddy_quick_scan, _zcode_db_connect,
    _zcode_db_fmt_timestamp, _zcode_db_parse_message_data,
)

# ── register_adapter() calls execute at module import time ──
# The 12 register_adapter("env", ...) calls are at the bottom of
# _adapters.py and execute when that module is imported above.

# ── Discover external adapter plugins ──
# Scans ~/.config/session-digger/adapters/*/adapter.py
from echolib._adapter_discovery import discover_plugins as _discover_plugins
for _env_id, _adapter in _discover_plugins().items():
    from echolib._adapters import ADAPTER_REGISTRY as _reg
    if _env_id not in _reg:
        _reg[_env_id] = _adapter

# Clean up helper names from module namespace
for _name in list(locals()):
    if _name.startswith("_disc") or _name.startswith("_env_") or _name.startswith("_adapter") or _name == "_name":
        locals().pop(_name, None)
