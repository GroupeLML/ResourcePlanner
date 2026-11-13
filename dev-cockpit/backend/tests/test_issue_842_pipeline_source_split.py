import unittest

from app.config import (
    PIPELINE_SOURCE_DEDICATED_STRICT,
    PIPELINE_SOURCE_LEGACY_EMBEDDED,
    Settings,
    canonical_pipeline_is_strict,
)
from app.github import GitHubError
from app.pipeline_source import load_pipeline_source
from app.roadmap import resolve_canonical_pipeline_source
from app.service import build_dashboard


HUMAN_ROADMAP = """
# Roadmap humain #55

La prochaine tranche active est #999.

<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
999 | WORK | READY | #999 | MAIN | stale embedded state
<!-- /COCKPIT_PIPELINE_V1 -->
"""

CANDIDATE = """<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
840 | WORK | DONE | #840 | MAIN | delivered
842 | WORK | READY | #842 | MAIN | migration
ASTRA-644 | ARCHITECTURE_GATE | BLOCKED | #644 | MAIN | architecture gate
<!-- /COCKPIT_PIPELINE_V1 -->"""


class FakeGitHub:
    def __init__(self, *, candidate_body=CANDIDATE, missing_parent=None, missing_candidate=False):
        self.candidate_body = candidate_body
        self.missing_parent = missing_parent
        self.missing_candidate = missing_candidate
        self.issue_reads = []

    def validate_repo(self, repo):
        return repo

    async def get_issue(self, repo, number):
        self.issue_reads.append(number)
        if number == 55:
            return {
                "number": 55,
                "title": "Roadmap humain",
                "body": HUMAN_ROADMAP,
                "state": "open",
                "html_url": "https://github.test/issues/55",
                "updated_at": "2026-11-13T00:00:00Z",
            }
        if number == 851:
            if self.missing_candidate:
                raise GitHubError(404, "Not Found")
            return {
                "number": 851,
                "title": "Roadmap machine candidate",
                "body": self.candidate_body,
                "state": "open",
                "html_url": "https://github.test/issues/851",
                "updated_at": "2026-11-13T00:01:00Z",
            }
        if number == self.missing_parent:
            raise GitHubError(404, "Not Found")
        if number in {840, 842, 644, 999}:
            return {
                "number": number,
                "title": f"Issue {number}",
                "body": "",
                "state": "open",
                "html_url": f"https://github.test/issues/{number}",
                "updated_at": "2026-11-13T00:00:00Z",
            }
        raise GitHubError(404, "Not Found")

    async def list_pulls(self, repo, state="open", per_page=20):
        return []

    async def latest_commit(self, repo):
        return {
            "sha": "main-sha",
            "html_url": "https://github.test/commit/main-sha",
            "commit": {
                "message": "main",
                "author": {"date": "2026-11-13T00:00:00Z"},
            },
        }

    async def get_text_file(self, repo, path, ref=None):
        if path == "AGENTS.md":
            return "# AGENTS\n"
        return ""

    async def list_directory(self, repo, path, ref=None):
        return []

    async def list_all_branches(self, repo, **kwargs):
        return []


class PipelineSourceSplitTests(unittest.IsolatedAsyncioTestCase):
    def test_default_authority_remains_55_and_candidate_is_explicit(self):
        settings = Settings()
        self.assertEqual(settings.roadmap_issue, 55)
        self.assertEqual(settings.canonical_pipeline_issue, 55)
        self.assertEqual(settings.canonical_pipeline_source_mode, PIPELINE_SOURCE_LEGACY_EMBEDDED)
        self.assertFalse(canonical_pipeline_is_strict(settings))
        self.assertEqual(settings.migration_candidate_pipeline_issue, 851)

    def test_strict_resolver_refuses_missing_block_and_unknown_version(self):
        missing = resolve_canonical_pipeline_source("# human prose only")
        self.assertFalse(missing.valid)
        self.assertEqual(missing.source, "canonical_v1")
        self.assertIn("Aucun fallback", " ".join(missing.errors))

        unknown = resolve_canonical_pipeline_source(
            "<!-- COCKPIT_PIPELINE_V2 -->\nvalue\n<!-- /COCKPIT_PIPELINE_V2 -->"
        )
        self.assertFalse(unknown.valid)
        self.assertIn("non supportée", " ".join(unknown.errors))

        valid_v1 = resolve_canonical_pipeline_source(CANDIDATE)
        self.assertTrue(valid_v1.valid)

        mixed_v1_v2 = resolve_canonical_pipeline_source(
            CANDIDATE
            + "\n<!-- COCKPIT_PIPELINE_V2 -->\nvalue\n<!-- /COCKPIT_PIPELINE_V2 -->"
        )
        self.assertFalse(mixed_v1_v2.valid)
        self.assertIn("V2", " ".join(mixed_v1_v2.errors))

        mixed_v2_v1 = resolve_canonical_pipeline_source(
            "<!-- COCKPIT_PIPELINE_V2 -->\nvalue\n<!-- /COCKPIT_PIPELINE_V2 -->\n"
            + CANDIDATE
        )
        self.assertFalse(mixed_v2_v1.valid)
        self.assertIn("V2", " ".join(mixed_v2_v1.errors))

    async def test_strict_source_validates_referenced_parent_issues(self):
        client = FakeGitHub(missing_parent=644)
        _, contract = await load_pipeline_source(
            client,
            "GroupeLML/ResourcePlanner",
            851,
            strict=True,
            validate_references=True,
        )
        self.assertFalse(contract.valid)
        self.assertIn("#644 introuvable", " ".join(contract.errors))

    async def test_missing_configured_machine_issue_fails_closed(self):
        client = FakeGitHub(missing_candidate=True)
        issue, contract = await load_pipeline_source(
            client,
            "GroupeLML/ResourcePlanner",
            851,
            strict=True,
            validate_references=True,
        )
        self.assertEqual(issue["number"], 851)
        self.assertFalse(contract.valid)
        self.assertEqual(contract.steps, [])
        self.assertIn("#851 introuvable", " ".join(contract.errors))

    async def test_dedicated_source_collision_with_human_issue_still_fails_closed(self):
        client = FakeGitHub(candidate_body="# human prose without canonical V1")
        settings = Settings(
            github_token="test",
            repository="GroupeLML/ResourcePlanner",
            roadmap_issue=851,
            canonical_pipeline_issue=851,
            canonical_pipeline_source_mode=PIPELINE_SOURCE_DEDICATED_STRICT,
            migration_candidate_pipeline_issue=851,
        )
        dashboard = await build_dashboard(
            client,
            settings,
            "GroupeLML/ResourcePlanner",
        )
        self.assertEqual(dashboard["roadmap"]["number"], 851)
        self.assertEqual(dashboard["pipeline_source"]["number"], 851)
        self.assertTrue(dashboard["pipeline_source"]["strict"])
        self.assertEqual(
            dashboard["pipeline_source"]["mode"],
            PIPELINE_SOURCE_DEDICATED_STRICT,
        )
        self.assertFalse(dashboard["pipeline"]["valid"])
        self.assertIsNone(dashboard["pipeline"]["now"])

    async def test_incompatible_legacy_source_configuration_fails_closed(self):
        client = FakeGitHub()
        settings = Settings(
            github_token="test",
            repository="GroupeLML/ResourcePlanner",
            roadmap_issue=55,
            canonical_pipeline_issue=851,
            canonical_pipeline_source_mode=PIPELINE_SOURCE_LEGACY_EMBEDDED,
            migration_candidate_pipeline_issue=851,
        )
        with self.assertRaises(ValueError):
            await build_dashboard(
                client,
                settings,
                "GroupeLML/ResourcePlanner",
            )

    async def test_dashboard_explicit_machine_source_ignores_stale_55_pipeline(self):
        client = FakeGitHub()
        settings = Settings(
            github_token="test",
            repository="GroupeLML/ResourcePlanner",
            roadmap_issue=55,
            canonical_pipeline_issue=851,
            canonical_pipeline_source_mode=PIPELINE_SOURCE_DEDICATED_STRICT,
            migration_candidate_pipeline_issue=851,
        )
        dashboard = await build_dashboard(
            client,
            settings,
            "GroupeLML/ResourcePlanner",
        )
        self.assertEqual(dashboard["roadmap"]["number"], 55)
        self.assertEqual(dashboard["pipeline_source"]["number"], 851)
        self.assertTrue(dashboard["pipeline_source"]["strict"])
        self.assertTrue(dashboard["pipeline"]["valid"])
        self.assertEqual(dashboard["pipeline"]["now"]["key"], "842")
        self.assertNotEqual(dashboard["pipeline"]["now"]["key"], "999")
        self.assertIn(55, client.issue_reads)
        self.assertIn(851, client.issue_reads)


if __name__ == "__main__":
    unittest.main()
