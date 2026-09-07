#!/usr/bin/env python3
"""Search SkillsMP without installing another agent skill."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import UTC, datetime
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


API_URL = "https://skillsmp.com/api/v1/skills/search"
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def github_parts(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    repo = "/".join(parts[:2]).lower() if len(parts) >= 2 else parsed.path.strip("/").lower()
    source_path = "/".join(parts[4:]) if len(parts) >= 5 and parts[2] in {"tree", "blob"} else "/".join(parts[2:])
    return repo, source_path


def iso_timestamp(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def normalize_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    github_url = str(raw.get("githubUrl", "")).strip()
    name = str(raw.get("name", "")).strip()
    repo, source_path = github_parts(github_url)
    return {
        "source": "skillsmp",
        "name": name,
        "author": raw.get("author"),
        "description": raw.get("description"),
        "content_language": raw.get("contentLanguage"),
        "github_url": github_url,
        "skillsmp_url": raw.get("skillUrl"),
        "repo_stars": raw.get("stars"),
        "catalog_updated_at": iso_timestamp(raw.get("updatedAt")),
        "source_key": f"{repo}:{source_path.lower()}",
        "family_key": f"{repo}:{name.lower()}",
        "aliases": [],
    }


def preference(candidate: dict[str, Any], language: str | None) -> tuple[int, int]:
    candidate_language = str(candidate.get("content_language") or "")
    if language:
        language_rank = 0 if candidate_language == language else 2
    else:
        language_rank = {"en": 0, "mul": 1, "und": 2}.get(candidate_language, 3)
    path_length = len(str(candidate.get("source_key", "")))
    return language_rank, path_length


def deduplicate(candidates: list[dict[str, Any]], language: str | None = None) -> list[dict[str, Any]]:
    families: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = str(candidate["family_key"])
        current = families.get(key)
        if current is None:
            families[key] = candidate
            continue
        if preference(candidate, language) < preference(current, language):
            candidate["aliases"] = [
                *current.get("aliases", []),
                {"language": current.get("content_language"), "github_url": current.get("github_url")},
            ]
            families[key] = candidate
        else:
            current.setdefault("aliases", []).append(
                {"language": candidate.get("content_language"), "github_url": candidate.get("github_url")}
            )
    return list(families.values())


def build_url(args: argparse.Namespace) -> str:
    params: dict[str, Any] = {
        "q": args.query,
        "page": args.page,
        "limit": args.limit,
        "sortBy": args.sort,
    }
    for key in ("category", "occupation", "language"):
        value = getattr(args, key)
        if value:
            params[key] = value
    return f"{API_URL}?{urlencode(params)}"


def retry_delay(attempt: int, backoff: float, jitter: float) -> float:
    """Return capped exponential delay for a zero-based retry attempt."""
    return min(30.0, backoff * (2**attempt) + random.uniform(0.0, jitter))


def fetch_once(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, str | None]]:
    headers = {"Accept": "application/json", "User-Agent": "skill-search/0.1.0"}
    api_key = os.environ.get("SKILLSMP_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(build_url(args), headers=headers)
    with urlopen(request, timeout=args.timeout) as response:
        body = response.read()
        rate = {
            "daily_limit": response.headers.get("X-RateLimit-Daily-Limit"),
            "daily_remaining": response.headers.get("X-RateLimit-Daily-Remaining"),
            "minute_limit": response.headers.get("X-RateLimit-Minute-Limit"),
            "minute_remaining": response.headers.get("X-RateLimit-Minute-Remaining"),
        }
    payload = json.loads(body)
    return payload, rate


def fetch(args: argparse.Namespace) -> dict[str, Any]:
    max_attempts = args.retries + 1
    payload: dict[str, Any] | None = None
    rate: dict[str, str | None] = {}
    attempts = 0
    for attempt in range(max_attempts):
        attempts = attempt + 1
        try:
            payload, rate = fetch_once(args)
            break
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = f"SkillsMP HTTP {exc.code}: {body[:500]}"
            if exc.code not in RETRYABLE_HTTP_STATUS or attempts >= max_attempts:
                raise RuntimeError(message) from exc
        except (URLError, IncompleteRead, RemoteDisconnected, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            message = f"SkillsMP request failed: {exc}"
            if attempts >= max_attempts:
                raise RuntimeError(message) from exc
        time.sleep(retry_delay(attempt, args.retry_backoff, args.retry_jitter))
    if payload is None:  # pragma: no cover - loop exits only through success or exception.
        raise RuntimeError("SkillsMP request failed without a response")
    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError(f"SkillsMP returned an unsuccessful response: {payload}")
    raw_skills = payload.get("data", {}).get("skills", [])
    candidates = [normalize_candidate(item) for item in raw_skills if isinstance(item, dict)]
    return {
        "source": "skillsmp",
        "query": args.query,
        "request": {
            "attempts": attempts,
            "max_attempts": max_attempts,
        },
        "metric_semantics": {
            "repo_stars": "GitHub repository stars; not installs, ratings, or skill-specific quality",
            "catalog_updated_at": "SkillsMP indexed update time; verify the repository for current source",
        },
        "pagination": payload.get("data", {}).get("pagination", {}),
        "rate_limit": rate,
        "raw_candidate_count": len(candidates),
        "deduplicated_candidate_count": len(deduplicate(candidates, args.language)),
        "candidates": deduplicate(candidates, args.language),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search and normalize SkillsMP catalog candidates.")
    parser.add_argument("query", help="Keyword search query; wildcard searches are unsupported.")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    parser.add_argument("--sort", choices=("stars", "recent"), default="stars")
    parser.add_argument("--category")
    parser.add_argument("--occupation")
    parser.add_argument("--language", help="ISO language code, mul, or und.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2, choices=range(0, 6), metavar="0-5")
    parser.add_argument("--retry-backoff", type=float, default=0.5, help="Initial retry delay in seconds.")
    parser.add_argument("--retry-jitter", type=float, default=0.2, help="Maximum random retry jitter in seconds.")
    parser.add_argument("--output", help="Optional JSON output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.query.strip() or "*" in args.query:
        raise SystemExit("query must be non-empty and cannot contain wildcards")
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
