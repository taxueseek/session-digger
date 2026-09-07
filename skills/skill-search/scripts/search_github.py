#!/usr/bin/env python3
"""Search the GitHub official API for skill repositories and first-party catalogs.

Two modes:
- default: `GET /search/repositories` with optional `topic:` filter, sorted by
  stars or updated. The GitHub API is the canonical-coverage source: it exposes
  repository-native metadata (stars, language, topics, pushed_at, license)
  that other catalogs approximate or omit.
- `--official-catalog OWNER/REPO`: list top-level directories of a first-party
  catalog repository (e.g. `anthropics/skills`). Each directory is one skill;
  this is the trust anchor, not a popularity measure.

Quota discipline: search API is 10 req/min anonymously; core API is 60 req/h.
Set `GITHUB_TOKEN` to raise limits and increase the search rate budget.
"""

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
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.github.com"
SEARCH_REPOS_URL = f"{API_BASE}/search/repositories"
CONTENTS_URL = f"{API_BASE}/repos/{{owner_repo}}/contents"
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
ANONYMOUS_SEARCH_LIMIT = 10  # per minute


def iso_timestamp(value: Any) -> str | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def normalize_repo_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    full_name = str(raw.get("full_name") or "")
    name = str(raw.get("name") or "").strip()
    license_info = raw.get("license")
    return {
        "source": "github",
        "name": name,
        "owner_repo": full_name,
        "description": raw.get("description"),
        "repo_stars": raw.get("stargazers_count"),
        "forks": raw.get("forks_count"),
        "language": raw.get("language"),
        "topics": raw.get("topics") or [],
        "pushed_at": iso_timestamp(raw.get("pushed_at")),
        "github_url": raw.get("html_url"),
        "default_branch": raw.get("default_branch"),
        "license_spdx": license_info.get("spdx_id") if isinstance(license_info, dict) else None,
        "source_key": f"{full_name.lower()}:",
        "family_key": f"{full_name.lower()}:{name.lower()}",
        "aliases": [],
    }


def normalize_catalog_item(owner_repo: str, item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("type") != "dir":
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    return {
        "source": "github-official",
        "name": name,
        "owner_repo": owner_repo,
        "description": None,
        "repo_stars": None,
        "forks": None,
        "language": None,
        "topics": [],
        "pushed_at": None,
        "github_url": item.get("html_url") or f"https://github.com/{owner_repo}/tree/main/{name}",
        "default_branch": None,
        "license_spdx": None,
        "source_key": f"{owner_repo.lower()}:{name.lower()}",
        "family_key": f"{owner_repo.lower()}:{name.lower()}",
        "aliases": [],
    }


def retry_delay(attempt: int, backoff: float, jitter: float) -> float:
    """Return capped exponential delay for a zero-based retry attempt."""
    return min(30.0, backoff * (2**attempt) + random.uniform(0.0, jitter))


def headers() -> dict[str, str]:
    result = {"Accept": "application/vnd.github+json", "User-Agent": "skill-search/0.1.0", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


def build_search_url(query: str, topic: str | None, sort: str, limit: int, page: int) -> str:
    q = query.strip()
    if topic:
        q = f"{q} topic:{topic}" if q else f"topic:{topic}"
    params = {"q": q, "per_page": limit, "page": page}
    if sort:
        params["sort"] = sort
        params["order"] = "desc"
    return f"{SEARCH_REPOS_URL}?{urlencode(params)}"


def fetch_repos(query: str, args: argparse.Namespace) -> dict[str, Any]:
    """Search repositories once with retries; returns a normalized result envelope."""
    url = build_search_url(query, args.topic, args.sort, args.limit, args.page)
    max_attempts = args.retries + 1
    payload: dict[str, Any] | None = None
    rate: dict[str, str | None] = {}
    attempts = 0
    for attempt in range(max_attempts):
        attempts = attempt + 1
        try:
            request = Request(url, headers=headers())
            with urlopen(request, timeout=args.timeout) as response:
                body = response.read()
                rate = {
                    "limit": response.headers.get("X-RateLimit-Limit"),
                    "remaining": response.headers.get("X-RateLimit-Remaining"),
                    "reset": response.headers.get("X-RateLimit-Reset"),
                }
            payload = json.loads(body)
            break
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = f"GitHub HTTP {exc.code}: {body[:500]}"
            if exc.code == 403 and "rate limit" in body.lower():
                raise RuntimeError(f"{message} (anonymous search quota is about {ANONYMOUS_SEARCH_LIMIT}/min; set GITHUB_TOKEN)") from exc
            if exc.code not in RETRYABLE_HTTP_STATUS or attempts >= max_attempts:
                raise RuntimeError(message) from exc
        except (URLError, IncompleteRead, RemoteDisconnected, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            message = f"GitHub request failed: {exc}"
            if attempts >= max_attempts:
                raise RuntimeError(message) from exc
        time.sleep(retry_delay(attempt, args.retry_backoff, args.retry_jitter))
    if payload is None:  # pragma: no cover - loop exits only through success or exception.
        raise RuntimeError("GitHub request failed without a response")
    raw_items = payload.get("items", [])
    candidates = [normalize_repo_candidate(item) for item in raw_items if isinstance(item, dict)]
    return {
        "source": "github",
        "mode": "repositories",
        "query": query,
        "topic": args.topic,
        "request": {"attempts": attempts, "max_attempts": max_attempts},
        "metric_semantics": {
            "repo_stars": "GitHub repository stars; popularity signal, not skill quality or user rating",
            "pushed_at": "repository maintenance recency signal; verify source SKILL.md before adoption",
            "license_spdx": "repository license; not a permission grant for its content",
        },
        "pagination": {
            "total_count": payload.get("total_count"),
            "incomplete_results": payload.get("incomplete_results"),
        },
        "rate_limit": rate,
        "raw_candidate_count": len(candidates),
        "deduplicated_candidate_count": len(candidates),
        "candidates": candidates,
    }


def fetch_official_catalog(catalog_ref: str, args: argparse.Namespace) -> dict[str, Any]:
    """List top-level skill directories of a first-party catalog repository.

    `catalog_ref` may be `OWNER/REPO` or `OWNER/REPO/sub/path` (e.g.
    `anthropics/skills/skills`), so a catalog that nests skills below the
    repository root can still be enumerated.
    """
    parts = catalog_ref.strip("/").split("/")
    if len(parts) < 2:
        raise RuntimeError(f"invalid official catalog reference: {catalog_ref!r} (expected OWNER/REPO[/path])")
    owner_repo = "/".join(parts[:2])
    subpath = "/".join(parts[2:])
    url = CONTENTS_URL.format(owner_repo=owner_repo)
    if subpath:
        url = f"{url}/{subpath}"
    max_attempts = args.retries + 1
    attempts = 0
    for attempt in range(max_attempts):
        attempts = attempt + 1
        try:
            request = Request(url, headers=headers())
            with urlopen(request, timeout=args.timeout) as response:
                body = response.read()
                rate = {
                    "limit": response.headers.get("X-RateLimit-Limit"),
                    "remaining": response.headers.get("X-RateLimit-Remaining"),
                    "reset": response.headers.get("X-RateLimit-Reset"),
                }
            payload = json.loads(body)
            break
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = f"GitHub HTTP {exc.code}: {body[:500]}"
            if exc.code not in RETRYABLE_HTTP_STATUS or attempts >= max_attempts:
                raise RuntimeError(message) from exc
        except (URLError, IncompleteRead, RemoteDisconnected, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            message = f"GitHub request failed: {exc}"
            if attempts >= max_attempts:
                raise RuntimeError(message) from exc
        time.sleep(retry_delay(attempt, args.retry_backoff, args.retry_jitter))
    if not isinstance(payload, list):
        raise RuntimeError(f"GitHub contents response was not a list: {payload!r}")
    candidates = []
    for item in payload:
        candidate = normalize_catalog_item(owner_repo, item)
        if candidate is not None:
            candidates.append(candidate)
    return {
        "source": "github-official",
        "mode": "official-catalog",
        "query": catalog_ref,
        "topic": None,
        "request": {"attempts": attempts, "max_attempts": max_attempts},
        "metric_semantics": {
            "catalog_repo": "first-party curated catalog; presence means curated/published, not popularity",
        },
        "pagination": {"total_count": len(candidates), "incomplete_results": False},
        "rate_limit": rate,
        "raw_candidate_count": len(candidates),
        "deduplicated_candidate_count": len(candidates),
        "candidates": candidates,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search GitHub official API for skill repositories and catalogs.")
    parser.add_argument("query", nargs="?", help="Repository search query; optional when --official-catalog is given.")
    parser.add_argument("--topic", help="Restrict results to a topic (e.g. claude-skills, agent-skills).")
    parser.add_argument("--sort", choices=("stars", "updated"), default="stars")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=10, choices=range(1, 101), metavar="1-100")
    parser.add_argument("--official-catalog", metavar="OWNER/REPO[/PATH]", help="List top-level skill directories of a first-party catalog repo instead of searching; PATH is optional (e.g. anthropics/skills/skills).")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2, choices=range(0, 6), metavar="0-5")
    parser.add_argument("--retry-backoff", type=float, default=0.5, help="Initial retry delay in seconds.")
    parser.add_argument("--retry-jitter", type=float, default=0.2, help="Maximum random retry jitter in seconds.")
    parser.add_argument("--output", help="Optional JSON output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.official_catalog:
        if "/" not in args.official_catalog:
            raise SystemExit("--official-catalog must be OWNER/REPO")
    elif not (args.query or "").strip():
        raise SystemExit("query must be non-empty unless --official-catalog is given")
    if args.retry_backoff < 0 or args.retry_jitter < 0:
        raise SystemExit("retry delay values must be non-negative")
    try:
        if args.official_catalog:
            result = fetch_official_catalog(args.official_catalog, args)
        else:
            result = fetch_repos(args.query, args)
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
