#!/usr/bin/env python3
"""Tests for the ClawHub catalog engine."""

from __future__ import annotations

import io
import importlib.util
import json
import unittest
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("search_clawhub", ROOT / "scripts" / "search_clawhub.py")
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("unable to load search_clawhub.py")
SEARCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEARCH)


class FakeResponse:
    def __init__(self, body: bytes | Exception) -> None:
        self.body = body

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
        "query": "pdf",
        "limit": 10,
        "timeout": 5.0,
        "retries": 1,
        "retry_backoff": 0.0,
        "retry_jitter": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def sample_raw(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "slug": "pdf-tools",
        "displayName": "PDF Tools",
        "ownerHandle": "acme",
        "downloads": 3000,
        "score": 0.9,
        "native": {
            "ownerHandle": "acme",
            "skill": {
                "slug": "pdf-tools",
                "displayName": "PDF Tools",
                "summary": "Rotate, merge, and fill PDF forms.",
                "stats": {"downloads": 3000, "installs": 900, "stars": 42},
                "isSuspicious": False,
            },
        },
        "install": {"reference": "acme/pdf-tools"},
        "canonicalUrl": "/skills/acme/pdf-tools",
    }
    item.update(overrides)
    return item


class ClawHubSearchTest(unittest.TestCase):
    def test_normalize_candidate_fields_and_family_key(self) -> None:
        candidate = SEARCH.normalize_candidate(sample_raw(), "pdf")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["source"], "clawhub")
        self.assertEqual(candidate["family_key"], "clawhub:acme:pdf-tools")
        self.assertEqual(candidate["clawhub_downloads"], 3000)
        self.assertEqual(candidate["clawhub_installs"], 900)
        self.assertEqual(candidate["clawhub_stars"], 42)
        self.assertEqual(candidate["url"], "https://clawhub.ai/skills/acme/pdf-tools")
        self.assertEqual(candidate["install_ref"], "acme/pdf-tools")
        self.assertTrue(candidate["requires_source_review"])

    def test_normalize_rejects_item_without_name_or_slug(self) -> None:
        candidate = SEARCH.normalize_candidate({"displayName": ""}, "pdf")
        self.assertIsNone(candidate)

    def test_suspicious_item_is_flagged(self) -> None:
        candidate = SEARCH.normalize_candidate(
            sample_raw(native={"ownerHandle": "acme", "skill": {"slug": "pdf-tools", "stats": {}, "isSuspicious": True}}),
            "pdf",
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertTrue(candidate["is_suspicious"])

    def test_fetch_returns_candidates_and_metric_semantics(self) -> None:
        payload = json.dumps({"results": [sample_raw()]}).encode()
        with patch.object(SEARCH, "urlopen", return_value=FakeResponse(payload)):
            result = SEARCH.fetch(fetch_args())
        self.assertEqual(result["source"], "clawhub")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["raw_candidate_count"], 1)
        self.assertIn("clawhub_downloads", result["metric_semantics"])

    def test_fetch_deduplicates_by_family_key(self) -> None:
        payload = json.dumps({"results": [sample_raw(), sample_raw()]}).encode()
        with patch.object(SEARCH, "urlopen", return_value=FakeResponse(payload)):
            result = SEARCH.fetch(fetch_args())
        self.assertEqual(result["candidate_count"], 1)

    def test_incomplete_read_is_retried(self) -> None:
        payload = json.dumps({"results": []}).encode()
        responses = [FakeResponse(IncompleteRead(b"partial", 10)), FakeResponse(payload)]
        with patch.object(SEARCH, "urlopen", side_effect=responses) as mocked, patch.object(SEARCH.time, "sleep"):
            result = SEARCH.fetch(fetch_args())
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(result["candidate_count"], 0)

    def test_non_retryable_http_error_stops_immediately(self) -> None:
        error = HTTPError("https://clawhub.ai", 400, "bad request", {}, io.BytesIO(b"invalid query"))
        mocked = MagicMock(side_effect=error)
        with patch.object(SEARCH, "urlopen", mocked), patch.object(SEARCH.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                SEARCH.fetch(fetch_args())
        self.assertEqual(mocked.call_count, 1)

    def test_wildcard_query_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "wildcard"):
            SEARCH.fetch(fetch_args(query="*"))

    def test_empty_result_is_not_an_error(self) -> None:
        payload = json.dumps({"results": []}).encode()
        with patch.object(SEARCH, "urlopen", return_value=FakeResponse(payload)):
            result = SEARCH.fetch(fetch_args())
        self.assertEqual(result["candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
