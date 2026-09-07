#!/usr/bin/env python3
"""Search the ClawHub (OpenClaw ecosystem) registry without installing a skill.

Adds a fourth vertical catalog beside skills.sh, SkillsMP, and the GitHub API.
Metrics stay separate: downloads / installs / stars are telemetry signals, never
a combined quality score.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://clawhub.ai/api/search"
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_candidate(raw: dict[str, Any], query: str) -> dict[str, Any] | None:
    native = raw.get("native") if isinstance(raw.get("native"), dict) else {}
    skill = native.get("skill") if isinstance(native.get("skill"), dict) else {}
    stats = skill.get("stats") if isinstance(skill.get("stats"), dict) else {}
    install = raw.get("install") if isinstance(raw.get("install"), dict) else {}

    slug = str(raw.get("slug") or skill.get("slug") or "").strip()
    owner = str(
        raw.get("ownerHandle")
        or native.get("ownerHandle")
        or (install.get("reference") or "").split("/")[0]
        or ""
    ).strip()
    name = str(raw.get("displayName") or skill.get("displayName") or slug or "").strip()
    if not name and not slug:
        return None

    downloads = _as_int(raw.get("downloads") or stats.get("downloads"))
    installs = _as_int(stats.get("installs"))
    stars = _as_int(stats.get("stars"))

    ref = str(install.get("reference") or (f"{owner}/{slug}" if owner and slug else slug)).strip()
    canonical = str(raw.get("canonicalUrl") or "").strip()
    if canonical.startswith("/"):
        url = f"https://clawhub.ai{canonical}"
    elif canonical:
        url = canonical
    elif ref:
        url = f"https://clawhub.ai/{ref}"
    else:
        url = ""

    summary = str(skill.get("summary") or raw.get("summary") or "")[:320]
    family = f"clawhub:{(owner or 'unknown').lower()}:{(slug or name).lower()}"
    is_suspicious = bool(skill.get("isSuspicious"))

    return {
        "source": "clawhub",
        "query": query,
        "name": slug or name,
        "title": f"{owner}/{slug}" if owner and slug else name,
        "owner_handle": owner,
        "slug": slug,
        "snippet": summary,
        "description": summary,
        "url": url,
        "install_ref": ref,
        "family_key": family,
        "clawhub_downloads": downloads,
        "clawhub_installs": installs,
        "clawhub_stars": stars,
        "clawhub_score": raw.get("score"),
        "is_suspicious": is_suspicious,
        "skillish": True,
        "requires_source_review": True,
    }


def build_url(args: argparse.Namespace) -> str:
    params: dict[str, Any] = {"q": args.query, "limit": max(1, min(int(args.limit), 50))}
    return f"{API_URL}?{urlencode(params)}"


def retry_delay(attempt: int, backoff: float, jitter: float) -> float:
    return min(30.0, backoff * (2**attempt) + random.uniform(0.0, jitter))


def fetch_once(args: argparse.Namespace) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "skill-search/0.2.0"}
    request = Request(build_url(args), headers=headers)
    with urlopen(request, timeout=args.timeout) as response:
        body = response.read()
    return json.loads(body)


def fetch(args: argparse.Namespace) -> dict[str, Any]:
    query = (args.query or "").strip()
    if not query or "*" in query:
        raise RuntimeError("query must be non-empty and cannot contain wildcards")
    max_attempts = args.retries + 1
    payload: dict[str, Any] | None = None
    attempts = 0
    for attempt in range(max_attempts):
        attempts = attempt + 1
        try:
            payload = fetch_once(args)
            break
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = f"ClawHub HTTP {exc.code}: {body[:500]}"
            if exc.code not in RETRYABLE_HTTP_STATUS or attempts >= max_attempts:
                raise RuntimeError(message) from exc
        except (URLError, IncompleteRead, RemoteDisconnected, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            message = f"ClawHub request failed: {exc}"
            if attempts >= max_attempts:
                raise RuntimeError(message) from exc
        time.sleep(retry_delay(attempt, args.retry_backoff, args.retry_jitter))
    if payload is None:  # pragma: no cover - loop exits only through success or exception.
        raise RuntimeError("ClawHub request failed without a response")
    if not isinstance(payload, dict):
        raise RuntimeError(f"ClawHub returned an unsuccessful response: {payload}")

    raw_results = payload.get("results") or []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        candidate = normalize_candidate(item, query)
        if not candidate:
            continue
        if candidate.get("is_suspicious"):
            candidate["snippet"] = str(candidate.get("snippet") or "") + " [flagged suspicious]"
        key = str(candidate["family_key"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
        if len(candidates) >= args.limit:
            break

    return {
        "source": "clawhub",
        "query": query,
        "request": {"attempts": attempts, "max_attempts": max_attempts},
        "metric_semantics": {
            "clawhub_downloads": "ClawHub download telemetry; not correctness or skill quality",
            "clawhub_installs": "ClawHub install telemetry; adoption signal only",
            "clawhub_stars": "ClawHub stars; popularity signal, verify the source before adoption",
        },
        "raw_candidate_count": len(raw_results),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search and normalize ClawHub registry candidates.")
    parser.add_argument("query", help="Keyword search query; wildcard searches are unsupported.")
    parser.add_argument("--limit", type=int, default=15, choices=range(1, 51), metavar="1-50")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2, choices=range(0, 6), metavar="0-5")
    parser.add_argument("--retry-backoff", type=float, default=0.5, help="Initial retry delay in seconds.")
    parser.add_argument("--retry-jitter", type=float, default=0.2, help="Maximum random retry jitter in seconds.")
    parser.add_argument("--output", help="Optional JSON output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.retry_backoff < 0 or args.retry_jitter < 0:
        raise SystemExit("retry delay values must be non-negative")
    try:
        result = fetch(args)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2) from exc
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
