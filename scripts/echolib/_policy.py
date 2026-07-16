"""Token accounting policy + adapter capability tiers (leaf module).

Role: **policy** — single source of truth for:
  * how each environment interprets ``input_tokens`` vs cache legs
  * how deep an adapter is (T0–T4), for honest usage/report gating

Adapters may still pass ``input_includes_cache=`` explicitly; when omitted,
``attach_cache_hit_rates`` / ``finalize_session_stats`` resolve from this table.
"""
from __future__ import annotations

from typing import Literal, Optional, TypedDict

# ── Tiers (higher = deeper / more trustworthy for billing-style usage) ──
# T0 full token+cache+errors+model (+ optional family)
# T1 token+cache+errors+model
# T2 five-method contract; token weak/partial
# T3 contract present; data thin
# T4 discovery / SchemaProbe only
TIER_FULL = 0
TIER_TOKEN = 1
TIER_PARTIAL = 2
TIER_THIN = 3
TIER_PROBE = 4

# Minimum tier for main /usage and cache ranking tables (T0 and T1).
USAGE_MIN_TIER = TIER_TOKEN


class TokenPolicy(TypedDict):
    """Declared token accounting for one environment/provider."""
    input_includes_cache: Optional[bool]
    # True: cache_read is subset of input; False: additive non-cache input
    cache_is_subset: Optional[bool]
    source_type: Literal[
        "per_request", "cumulative_run", "cumulative_session", "none"
    ]
    has_token_usage: bool
    has_family_accounting: bool


# Keep keys aligned with ENV_REGISTRY + universal + common aliases.
PROVIDER_POLICY: dict[str, TokenPolicy] = {
    "claude": {
        "input_includes_cache": False,
        "cache_is_subset": False,
        "source_type": "per_request",
        "has_token_usage": True,
        "has_family_accounting": False,
    },
    "kimi_code": {
        "input_includes_cache": False,
        "cache_is_subset": False,
        "source_type": "per_request",
        "has_token_usage": True,
        "has_family_accounting": False,
    },
    "kimi": {
        "input_includes_cache": False,
        "cache_is_subset": False,
        "source_type": "per_request",
        "has_token_usage": True,
        "has_family_accounting": False,
    },
    "grok": {
        "input_includes_cache": True,
        "cache_is_subset": True,
        "source_type": "cumulative_run",
        "has_token_usage": True,
        "has_family_accounting": True,
    },
    "zcode": {
        "input_includes_cache": True,
        "cache_is_subset": True,
        "source_type": "per_request",
        "has_token_usage": True,
        "has_family_accounting": True,
    },
    "dimcode": {
        "input_includes_cache": True,
        "cache_is_subset": True,
        "source_type": "per_request",
        "has_token_usage": True,
        "has_family_accounting": False,
    },
    "codex": {
        "input_includes_cache": True,
        "cache_is_subset": True,
        "source_type": "cumulative_session",
        "has_token_usage": True,
        "has_family_accounting": False,
    },
    "workbuddy": {
        "input_includes_cache": True,
        "cache_is_subset": True,
        "source_type": "per_request",
        "has_token_usage": True,
        "has_family_accounting": False,
    },
    "cursor": {
        "input_includes_cache": None,
        "cache_is_subset": None,
        "source_type": "none",
        "has_token_usage": False,
        "has_family_accounting": False,
    },
    "trae_cn": {
        "input_includes_cache": None,
        "cache_is_subset": None,
        "source_type": "none",
        "has_token_usage": False,
        "has_family_accounting": False,
    },
    "dim": {
        "input_includes_cache": None,
        "cache_is_subset": None,
        "source_type": "none",
        "has_token_usage": False,
        "has_family_accounting": False,
    },
    "reasonix": {
        "input_includes_cache": None,
        "cache_is_subset": None,
        "source_type": "none",
        "has_token_usage": False,
        "has_family_accounting": False,
    },
    "universal": {
        "input_includes_cache": None,
        "cache_is_subset": None,
        "source_type": "none",
        "has_token_usage": False,
        "has_family_accounting": False,
    },
}

ADAPTER_TIER: dict[str, int] = {
    "claude": TIER_FULL,
    "grok": TIER_FULL,
    "kimi_code": TIER_TOKEN,
    "codex": TIER_TOKEN,
    "workbuddy": TIER_TOKEN,
    "zcode": TIER_TOKEN,
    "dimcode": TIER_TOKEN,  # has usage_run_stats when schema present
    "kimi": TIER_TOKEN,  # StatusUpdate token legs are real; not as deep as kimi_code wire
    "cursor": TIER_PARTIAL,
    "trae_cn": TIER_THIN,
    "dim": TIER_THIN,
    "reasonix": TIER_THIN,
    "universal": TIER_PROBE,
}

_DEFAULT_POLICY: TokenPolicy = {
    "input_includes_cache": None,
    "cache_is_subset": None,
    "source_type": "none",
    "has_token_usage": False,
    "has_family_accounting": False,
}


def get_token_policy(agent: str | None) -> TokenPolicy:
    """Return TokenPolicy for agent id; unknown agents → probe defaults."""
    if not agent:
        return dict(_DEFAULT_POLICY)  # type: ignore[return-value]
    key = str(agent).strip().lower()
    # display-name soft aliases
    aliases = {
        "claude code": "claude",
        "grok build": "grok",
        "kimi code": "kimi_code",
        "kimi (standalone)": "kimi",
        "codex (openai)": "codex",
        "trae cn": "trae_cn",
        "trae cn (bytedance)": "trae_cn",
        "zcode (z-ai)": "zcode",
        "dim (memory)": "dim",
        "dimcode (sqlite)": "dimcode",
    }
    key = aliases.get(key, key)
    pol = PROVIDER_POLICY.get(key)
    if pol is None:
        return dict(_DEFAULT_POLICY)  # type: ignore[return-value]
    return dict(pol)  # type: ignore[return-value]


def adapter_tier(agent: str | None) -> int:
    """Return capability tier (0–4). Unknown → T4 probe."""
    if not agent:
        return TIER_PROBE
    key = str(agent).strip().lower()
    return int(ADAPTER_TIER.get(key, TIER_PROBE))


def tier_supports(agent: str | None, capability: str) -> bool:
    """Whether agent tier is enough for a product capability.

    Capabilities:
      usage   — main token/cache tables (/usage)
      family  — main/subagent rollup APIs
      search  — list/search (all tiers with data on disk)
    """
    t = adapter_tier(agent)
    cap = (capability or "").strip().lower()
    if cap == "usage":
        return t <= USAGE_MIN_TIER and bool(get_token_policy(agent).get("has_token_usage"))
    if cap == "family":
        return bool(get_token_policy(agent).get("has_family_accounting"))
    if cap == "search":
        return True
    return False


def finalize_session_stats(stats: dict, agent: str) -> dict:
    """Stamp agent/tier and attach cache rates from PROVIDER_POLICY.

    Prefer this over ad-hoc ``attach_cache_hit_rates(..., input_includes_cache=)``
    so adapters cannot drift from the policy table.
    """
    if not isinstance(stats, dict):
        return stats
    stats["agent"] = agent
    stats["adapter_tier"] = adapter_tier(agent)
    pol = get_token_policy(agent)
    flag = pol.get("input_includes_cache")
    if flag is None or not pol.get("has_token_usage"):
        stats["input_includes_cache"] = flag
        stats["cache_hit_rate"] = None
        return stats
    # Local import avoids helpers↔policy cycle at module load.
    from echolib._helpers import attach_cache_hit_rates
    return attach_cache_hit_rates(stats, input_includes_cache=bool(flag))
