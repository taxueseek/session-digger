#!/usr/bin/env python3
"""Tests for dual-catalog prior-art orchestration."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("research_prior_art", ROOT / "scripts" / "research_prior_art.py")
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("unable to load research_prior_art.py")
RESEARCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESEARCH)


def args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "queries": ["seo audit"],
        "timeout": 1.0,
        "skip_skills_sh": False,
        "skip_skillsmp": False,
        "skip_github": True,
        "skip_clawhub": True,
        "official_catalog": None,
        "strict": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ResearchPriorArtTest(unittest.TestCase):
    def test_skills_sh_parser_handles_ansi_and_compact_installs(self) -> None:
        output = "\x1b[32mowner/repo@seo-audit\x1b[0m \x1b[36m177.3K installs\x1b[0m"
        result = RESEARCH.parse_skills_sh_output(output, "seo audit")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["skills_sh_installs"], 177300)
        self.assertEqual(result[0]["family_key"], "owner/repo:seo-audit")

    def test_cross_catalog_family_keeps_metrics_separate(self) -> None:
        merged = RESEARCH.merge_candidates(
            [
                {
                    "source": "skills.sh",
                    "query": "seo",
                    "family_key": "owner/repo:seo",
                    "skills_sh_installs": 100,
                },
                {
                    "source": "skillsmp",
                    "query": "seo audit",
                    "family_key": "owner/repo:seo",
                    "repo_stars": 900,
                },
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["skills_sh"]["skills_sh_installs"], 100)
        self.assertEqual(merged[0]["skillsmp"]["repo_stars"], 900)
        self.assertNotIn("score", merged[0])

    def test_one_catalog_failure_degrades_with_missing_evidence(self) -> None:
        def skills_sh(_query: str, _timeout: float) -> list[dict[str, object]]:
            return [{"source": "skills.sh", "query": "seo", "family_key": "owner/repo:seo", "skills_sh_installs": 10}]

        def skillsmp(_query: str, _args: object) -> list[dict[str, object]]:
            raise RuntimeError("rate limited")

        result = RESEARCH.research(args(), skills_sh, skillsmp)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["complete"])
        self.assertTrue(result["missing_evidence"])

    def test_strict_mode_fails_on_partial_catalog_evidence(self) -> None:
        def skills_sh(_query: str, _timeout: float) -> list[dict[str, object]]:
            return [{"source": "skills.sh", "query": "seo", "family_key": "owner/repo:seo", "skills_sh_installs": 10}]

        def skillsmp(_query: str, _args: object) -> list[dict[str, object]]:
            raise RuntimeError("rate limited")

        result = RESEARCH.research(args(strict=True), skills_sh, skillsmp)
        self.assertFalse(result["ok"])

    def test_summary_omits_candidate_payload(self) -> None:
        result = {
            "ok": True,
            "complete": True,
            "researched_at": "2026-08-03",
            "queries": ["seo"],
            "official_catalog_run": {"catalog": None, "status": "not_run"},
            "candidate_family_count": 1,
            "query_runs": [],
            "missing_evidence": [],
            "candidates": [{"large": "payload"}],
        }
        summary = RESEARCH.summary_view(result)
        self.assertNotIn("candidates", summary)
        self.assertEqual(summary["candidate_family_count"], 1)

    def test_github_channel_merges_with_skillsmp_by_family_key(self) -> None:
        def skills_sh(_query: str, _timeout: float) -> list[dict[str, object]]:
            return []

        def skillsmp(_query: str, _args: object) -> list[dict[str, object]]:
            return [{"source": "skillsmp", "query": "seo", "family_key": "owner/repo:seo", "repo_stars": 900}]

        def github(_query: str, _args: object) -> list[dict[str, object]]:
            return [
                {"source": "github", "query": "seo", "family_key": "owner/repo:seo", "repo_stars": 1200, "pushed_at": "2026-08-01T00:00:00+00:00"}
            ]

        result = RESEARCH.research(args(skip_skillsmp=False, skip_github=False), skills_sh, skillsmp, github)
        self.assertEqual(result["candidate_family_count"], 1)
        family = result["candidates"][0]
        self.assertEqual(family["skillsmp"]["repo_stars"], 900)
        self.assertEqual(family["github"]["repo_stars"], 1200)
        self.assertEqual(sorted(family["catalogs"]), ["github", "skillsmp"])

    def test_github_runner_injects_github_topic_and_sort(self) -> None:
        import json as jsonlib
        import subprocess
        from unittest import mock

        captured: dict[str, object] = {}

        def fake_fetch(query: str, request_args: object) -> dict[str, object]:
            captured["query"] = query
            captured["topic"] = request_args.topic
            captured["sort"] = request_args.sort
            captured["limit"] = request_args.limit
            return {"candidates": []}

        with mock.patch.object(RESEARCH.GITHUB, "fetch_repos", side_effect=fake_fetch):
            RESEARCH.run_github(
                "seo",
                SimpleNamespace(github_topic="claude-skills", github_sort="updated", github_limit=5, timeout=1.0, retries=0, retry_backoff=0.0, retry_jitter=0.0),
            )
        self.assertEqual(captured["query"], "seo")
        self.assertEqual(captured["topic"], "claude-skills")
        self.assertEqual(captured["sort"], "updated")
        self.assertEqual(captured["limit"], 5)

    def test_official_catalog_runner_lists_skill_dirs(self) -> None:
        from unittest import mock

        def fake_catalog(owner_repo: str, request_args: object) -> dict[str, object]:
            return {
                "candidates": [
                    {"source": "github-official", "name": "document-skills", "owner_repo": owner_repo, "family_key": f"{owner_repo}:document-skills"}
                ]
            }

        with mock.patch.object(RESEARCH.GITHUB, "fetch_official_catalog", side_effect=fake_catalog):
            result = RESEARCH.research(
                args(official_catalog="anthropics/skills"),
                lambda _q, _t: [],
                lambda _q, _a: [],
                lambda _q, _a: [],
            )
        self.assertEqual(result["official_catalog_run"]["status"], "ok")
        self.assertEqual(result["official_catalog_run"]["catalog"], "anthropics/skills")
        self.assertEqual(result["candidate_family_count"], 1)

    def test_clawhub_channel_merges_by_family_key(self) -> None:
        def skills_sh(_query: str, _timeout: float) -> list[dict[str, object]]:
            return []

        def skillsmp(_query: str, _args: object) -> list[dict[str, object]]:
            return []

        def clawhub(_query: str, _args: object) -> list[dict[str, object]]:
            return [
                {"source": "clawhub", "query": "seo", "family_key": "clawhub:acme:seo-tool", "clawhub_downloads": 5000, "clawhub_stars": 80},
                {"source": "clawhub", "query": "seo", "family_key": "clawhub:acme:seo-tool", "clawhub_downloads": 6000},
            ]

        result = RESEARCH.research(args(skip_github=True, skip_clawhub=False), skills_sh, skillsmp, clawhub_runner=clawhub)
        self.assertEqual(result["candidate_family_count"], 1)
        family = result["candidates"][0]
        self.assertEqual(family["clawhub"]["clawhub_downloads"], 6000)
        self.assertEqual(sorted(family["catalogs"]), ["clawhub"])

    def test_clawhub_failure_records_missing_evidence(self) -> None:
        def skills_sh(_query: str, _timeout: float) -> list[dict[str, object]]:
            return []

        def skillsmp(_query: str, _args: object) -> list[dict[str, object]]:
            return []

        def clawhub(_query: str, _args: object) -> list[dict[str, object]]:
            raise RuntimeError("clawhub down")

        result = RESEARCH.research(args(skip_github=True, skip_clawhub=False), skills_sh, skillsmp, clawhub_runner=clawhub)
        self.assertFalse(result["complete"])
        self.assertTrue(any("clawhub" in item for item in result["missing_evidence"]))

    def test_local_and_usage_channels_run_only_when_requested(self) -> None:
        def skills_sh(_query: str, _timeout: float) -> list[dict[str, object]]:
            return []

        def skillsmp(_query: str, _args: object) -> list[dict[str, object]]:
            return []

        # Without --local/--digger the blocks stay None (no local I/O, no subprocess).
        result = RESEARCH.research(args(skip_github=True, skip_clawhub=True), skills_sh, skillsmp)
        self.assertIsNone(result["local"])
        self.assertIsNone(result["usage"])

    def test_clawhub_runner_passes_limit_and_timeout(self) -> None:
        from unittest import mock

        captured: dict[str, object] = {}

        def fake_fetch(request_args: object) -> dict[str, object]:
            captured["limit"] = request_args.limit
            captured["timeout"] = request_args.timeout
            captured["query"] = request_args.query
            return {"candidates": []}

        with mock.patch.object(RESEARCH.CLAWHUB, "fetch", side_effect=fake_fetch):
            RESEARCH.run_clawhub(
                "seo",
                SimpleNamespace(clawhub_limit=7, timeout=3.0, retries=0, retry_backoff=0.0, retry_jitter=0.0),
            )
        self.assertEqual(captured["query"], "seo")
        self.assertEqual(captured["limit"], 7)
        self.assertEqual(captured["timeout"], 3.0)


class DecisionStageTest(unittest.TestCase):
    def _remote(self, status: str = "ok", hits: list[dict[str, object]] | None = None) -> dict[str, object]:
        return {"status": status, "missing": [], "hits": hits or []}

    def _empty_local(self) -> dict[str, object]:
        return {"status": "not_run", "hits": []}

    def test_flatten_candidates_keeps_metrics_separate(self) -> None:
        families = [
            {
                "family_key": "acme/repo:seo",
                "catalogs": ["skills.sh", "skillsmp"],
                "skills_sh": {
                    "source": "skills.sh",
                    "owner_repo": "acme/repo",
                    "skill_name": "seo",
                    "skills_sh_installs": 1000,
                    "skills_sh_installs_display": "1K",
                    "skills_sh_url": "https://skills.sh/acme/repo/seo",
                },
                "skillsmp": {"source": "skillsmp", "repo_stars": 900, "name": "seo"},
            }
        ]
        flat = RESEARCH.flatten_candidates(families)
        self.assertEqual(len(flat), 1)
        record = flat[0]
        self.assertEqual(record["skills_sh_installs"], 1000)
        self.assertEqual(record["repo_stars"], 900)
        self.assertEqual(record["catalogs"], ["skills.sh", "skillsmp"])
        self.assertIsNone(record["score"], "metrics stay separate; no unified score is invented")

    def test_strong_local_exact_match_reuses_with_validated_evidence(self) -> None:
        local_inv = {
            "status": "ok",
            "hits": [{"name": "seo-audit", "path": "/skills/seo-audit", "score": 1.0, "exact_name": True}],
        }
        decision, synthesis, evidence = RESEARCH.decide(
            "seo audit",
            None,
            local_inv,
            self._empty_local(),
            self._remote(),
            {"status": "not_run", "opportunity_hits": [], "gap_hits": []},
        )
        self.assertEqual(decision["action"], "reuse")
        self.assertEqual(decision["confidence"], "high")
        self.assertEqual(evidence["status"], "validated")
        self.assertEqual(synthesis[0]["verdict"], "keep")
        handoff = RESEARCH.build_handoff(decision, evidence, synthesis)
        self.assertEqual(handoff["SEEK_ACTION"], "reuse")
        self.assertEqual(handoff["SEEK_EVIDENCE"], "validated")

    def test_medium_local_adapts_instead_of_creating(self) -> None:
        local_inv = {
            "status": "ok",
            "hits": [{"name": "seo-scan", "path": "/skills/seo-scan", "score": 0.55, "exact_name": False}],
        }
        decision, _synthesis, _evidence = RESEARCH.decide(
            "seo audit",
            None,
            local_inv,
            self._empty_local(),
            self._remote(),
            {"status": "not_run", "opportunity_hits": [], "gap_hits": []},
        )
        self.assertEqual(decision["action"], "adapt")
        self.assertEqual(decision["target"], "/skills/seo-scan")

    def test_strong_remote_adapts_with_user_confirm_flag(self) -> None:
        remote = self._remote(
            hits=[
                {
                    "title": "acme/repo:seo",
                    "url": "https://skills.sh/acme/repo/seo",
                    "github_url": "https://github.com/acme/repo",
                    "catalogs": ["skills.sh"],
                    "skills_sh_installs": 10000,
                    "skills_sh_installs_display": "10K",
                    "repo_stars": 800,
                    "clawhub_downloads": None,
                    "score": None,
                    "skillish": True,
                }
            ]
        )
        decision, synthesis, evidence = RESEARCH.decide(
            "seo audit",
            None,
            self._empty_local(),
            self._empty_local(),
            remote,
            {"status": "not_run", "opportunity_hits": [], "gap_hits": []},
        )
        self.assertEqual(decision["action"], "adapt")
        self.assertEqual(decision["flags"], ["install_requires_user_confirm"])
        self.assertEqual(evidence["status"], "hypothesis")
        self.assertEqual(synthesis[0]["kind"], "catalog")

    def test_empty_signals_build_without_missing_evidence_when_channels_not_run(self) -> None:
        decision, synthesis, evidence = RESEARCH.decide(
            "seo audit",
            None,
            self._empty_local(),
            self._empty_local(),
            self._remote("empty", []),
            {"status": "not_run", "opportunity_hits": [], "gap_hits": []},
        )
        self.assertEqual(decision["action"], "build")
        self.assertNotIn("local_inventory", evidence["missing"], "not_run channels are scope choices, not evidence gaps")
        self.assertIn("remote_empty", evidence["missing"])
        self.assertEqual(evidence["status"], "hypothesis")
        self.assertEqual(synthesis[-1]["verdict"], "invent")

    def test_degraded_catalog_channel_records_missing_evidence(self) -> None:
        local_inv = {"status": "not_run", "hits": []}
        local_content = {"status": "not_run", "hits": []}
        usage = {"status": "not_run", "opportunity_hits": [], "gap_hits": []}
        query_runs = [
            {"query": "seo", "skills_sh": "ok", "skillsmp": "ok", "github": "ok", "clawhub": "error"}
        ]
        block = RESEARCH.run_decision_stage(
            args(decide=True),
            [],
            None,
            None,
            query_runs,
            {"catalog": None, "status": "not_run"},
        )
        self.assertEqual(block["evidence"]["status"], "missing_evidence")
        self.assertIn("clawhub", block["evidence"]["missing"])
        self.assertEqual(block["decision"]["action"], "build")

    def test_research_with_decide_returns_handoff_fields(self) -> None:
        def skills_sh(_query: str, _timeout: float) -> list[dict[str, object]]:
            return [
                {
                    "source": "skills.sh",
                    "query": "seo audit",
                    "owner_repo": "acme/repo",
                    "skill_name": "seo",
                    "family_key": "acme/repo:seo",
                    "skills_sh_installs": 20000,
                    "skills_sh_installs_display": "20K",
                }
            ]

        def skillsmp(_query: str, _args: object) -> list[dict[str, object]]:
            return [
                {
                    "source": "skillsmp",
                    "query": "seo audit",
                    "name": "seo",
                    "family_key": "acme/repo:seo",
                    "repo_stars": 1500,
                }
            ]

        result = RESEARCH.research(args(decide=True, skip_github=True, skip_clawhub=True), skills_sh, skillsmp)
        block = result["decision"]
        self.assertIsNotNone(block)
        self.assertEqual(block["decision"]["action"], "adapt")
        self.assertEqual(block["handoff"]["SEEK_ACTION"], "adapt")
        self.assertIn("install_requires_user_confirm", block["handoff"]["SEEK_FLAGS"])

    def test_summary_view_includes_decision_summary(self) -> None:
        result = {
            "ok": True,
            "complete": True,
            "researched_at": "2026-08-05",
            "queries": ["seo"],
            "official_catalog_run": {"catalog": None, "status": "not_run"},
            "candidate_family_count": 1,
            "query_runs": [],
            "missing_evidence": [],
            "candidates": [],
            "decision": {
                "decision": {"action": "adapt", "confidence": "medium", "target": "/skills/seo", "rationale": "r"},
                "evidence": {"status": "hypothesis"},
                "handoff": {"SEEK_ACTION": "adapt", "SEEK_TARGET": "/skills/seo"},
            },
        }
        summary = RESEARCH.summary_view(result)
        self.assertEqual(summary["decision"]["action"], "adapt")
        self.assertEqual(summary["decision"]["handoff"]["SEEK_ACTION"], "adapt")


if __name__ == "__main__":
    unittest.main()
