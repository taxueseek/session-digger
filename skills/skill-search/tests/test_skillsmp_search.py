#!/usr/bin/env python3
"""Tests for SkillsMP normalization and family deduplication."""

from __future__ import annotations

import importlib.util
import io
import json
import unittest
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("search_skillsmp", ROOT / "scripts" / "search_skillsmp.py")
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("unable to load search_skillsmp.py")
SEARCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEARCH)


class FakeResponse:
    def __init__(self, body: bytes | Exception, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def fetch_args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "query": "seo",
        "page": 1,
        "limit": 5,
        "sort": "stars",
        "category": None,
        "occupation": None,
        "language": None,
        "timeout": 1.0,
        "retries": 2,
        "retry_backoff": 0.0,
        "retry_jitter": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SkillsMPSearchTest(unittest.TestCase):
    def test_metric_and_source_fields_are_explicit(self) -> None:
        candidate = SEARCH.normalize_candidate(
            {
                "name": "seo",
                "author": "example",
                "contentLanguage": "en",
                "githubUrl": "https://github.com/example/repo/tree/main/skills/seo",
                "skillUrl": "https://skillsmp.com/example/seo",
                "stars": 123,
                "updatedAt": 1_700_000_000,
            }
        )
        self.assertEqual(candidate["repo_stars"], 123)
        self.assertEqual(candidate["family_key"], "example/repo:seo")
        self.assertNotIn("rating", candidate)

    def test_translated_family_is_collapsed_with_alias(self) -> None:
        raw = [
            SEARCH.normalize_candidate(
                {
                    "name": "seo",
                    "contentLanguage": "zh",
                    "githubUrl": "https://github.com/example/repo/tree/main/docs/zh-CN/skills/seo",
                }
            ),
            SEARCH.normalize_candidate(
                {
                    "name": "seo",
                    "contentLanguage": "en",
                    "githubUrl": "https://github.com/example/repo/tree/main/skills/seo",
                }
            ),
        ]
        result = SEARCH.deduplicate(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content_language"], "en")
        self.assertEqual(result[0]["aliases"][0]["language"], "zh")

    def test_incomplete_read_is_retried(self) -> None:
        payload = json.dumps({"success": True, "data": {"skills": [], "pagination": {}}}).encode()
        responses = [
            FakeResponse(IncompleteRead(b"partial", 10)),
            FakeResponse(payload, {"X-RateLimit-Daily-Remaining": "49"}),
        ]
        with patch.object(SEARCH, "urlopen", side_effect=responses) as mocked, patch.object(SEARCH.time, "sleep"):
            result = SEARCH.fetch(fetch_args())
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(result["request"]["attempts"], 2)
        self.assertEqual(result["rate_limit"]["daily_remaining"], "49")

    def test_non_retryable_http_error_stops_immediately(self) -> None:
        error = HTTPError("https://skillsmp.com", 400, "bad request", {}, io.BytesIO(b"invalid query"))
        mocked = MagicMock(side_effect=error)
        with patch.object(SEARCH, "urlopen", mocked), patch.object(SEARCH.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                SEARCH.fetch(fetch_args())
        self.assertEqual(mocked.call_count, 1)


if __name__ == "__main__":
    unittest.main()
