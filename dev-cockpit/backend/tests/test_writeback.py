import unittest
from unittest.mock import AsyncMock, patch

from app.config import (
    PIPELINE_SOURCE_DEDICATED_STRICT,
    PIPELINE_SOURCE_LEGACY_EMBEDDED,
    Settings,
)
from app.github import GitHubError
from app.roadmap import parse_cockpit_pipeline
from app.writeback import (
    RoadmapWritebackError,
    _sha256,
    apply_roadmap_writeback,
    build_roadmap_writeback_preview,
    replace_canonical_block,
)


CURRENT_BLOCK = """<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
399C | WORK | DONE | #399 | MAIN | concurrence
399D | WORK | READY | #399 | MAIN | React
276A | WORK | BLOCKED | #276 | MAIN | contrat
408 | WORK | READY | #408 | PARALLEL | filtres
<!-- /COCKPIT_PIPELINE_V1 -->"""

PROPOSED_BLOCK = """<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
399C | WORK | DONE | #399 | MAIN | concurrence
399D | WORK | DONE | #399 | MAIN | React
276A | WORK | READY | #276 | MAIN | contrat
408 | WORK | READY | #408 | PARALLEL | filtres
<!-- /COCKPIT_PIPELINE_V1 -->"""

BODY = f"""# Roadmap maître

Texte humain à préserver.

{CURRENT_BLOCK}

## Notes humaines
Ne jamais modifier cette section.
"""


def dashboard(*, status="stale", proposed_block=PROPOSED_BLOCK):
    contract = parse_cockpit_pipeline(BODY)
    return {
        "pipeline": {
            "source": "canonical_v1",
            "valid": True,
            "errors": [],
            "steps": [step.to_dict() for step in contract.steps],
        },
        "reconciliation": {
            "status": status,
            "summary": "stale" if status == "stale" else "ok",
            "findings": [],
            "proposal": (
                {
                    "changes": [
                        {"key": "399D", "from": "READY", "to": "DONE"},
                        {"key": "276A", "from": "BLOCKED", "to": "READY"},
                    ],
                    "pipeline_block": proposed_block,
                }
                if status == "stale"
                else None
            ),
        },
    }


class FakeGitHub:
    def __init__(
        self,
        *,
        body=BODY,
        updated_at="2026-09-24T19:30:00Z",
        update_error=None,
        post_write_get_body=None,
    ):
        self.body = body
        self.updated_at = updated_at
        self.update_error = update_error
        self.post_write_get_body = post_write_get_body
        self.update_calls = []
        self.get_calls = []
        self._written = False

    def validate_repo(self, repo):
        return repo

    async def get_issue(self, repo, number):
        self.get_calls.append(number)
        body = (
            self.post_write_get_body
            if self._written and self.post_write_get_body is not None
            else self.body
        )
        return {
            "number": number,
            "body": body,
            "updated_at": self.updated_at,
            "html_url": f"https://github.test/{repo}/issues/{number}",
        }

    async def update_issue_body(self, repo, number, body):
        self.update_calls.append((repo, number, body))
        if self.update_error:
            raise self.update_error
        self.body = body
        self._written = True
        self.updated_at = "2026-09-24T19:31:00Z"
        return {
            "number": number,
            "body": body,
            "updated_at": self.updated_at,
            "html_url": f"https://github.test/{repo}/issues/{number}",
        }


class RoadmapWritebackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
            canonical_pipeline_issue=55,
            canonical_pipeline_source_mode=PIPELINE_SOURCE_LEGACY_EMBEDDED,
        )

    def test_replace_changes_only_canonical_block(self):
        result = replace_canonical_block(BODY, PROPOSED_BLOCK)

        before_prefix, before_suffix = BODY.split(CURRENT_BLOCK)
        after_prefix, after_suffix = result.split(PROPOSED_BLOCK)
        self.assertEqual(before_prefix, after_prefix)
        self.assertEqual(before_suffix, after_suffix)
        self.assertIn("Ne jamais modifier cette section.", result)
        self.assertTrue(parse_cockpit_pipeline(result).valid)

    async def test_preview_requires_stale_and_returns_snapshot_and_diff(self):
        client = FakeGitHub()
        with patch("app.writeback.build_dashboard", new=AsyncMock(return_value=dashboard())):
            preview = await build_roadmap_writeback_preview(
                client,
                self.settings,
                "tchi99/RessourcePlanner",
            )

        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["expected_body_sha256"], _sha256(BODY))
        self.assertEqual(preview["proposal_sha256"], _sha256(PROPOSED_BLOCK))
        self.assertTrue(preview["safety"]["only_canonical_block"])
        self.assertIn("-399D | WORK | READY", preview["diff"])
        self.assertIn("+399D | WORK | DONE", preview["diff"])
        self.assertNotIn("Notes humaines", preview["diff"])

    async def test_preview_rejects_coherent_pipeline(self):
        client = FakeGitHub()
        with patch(
            "app.writeback.build_dashboard",
            new=AsyncMock(return_value=dashboard(status="coherent")),
        ):
            with self.assertRaises(RoadmapWritebackError) as caught:
                await build_roadmap_writeback_preview(
                    client,
                    self.settings,
                    "tchi99/RessourcePlanner",
                )
        self.assertEqual(caught.exception.code, "NO_SAFE_PROPOSAL")
        self.assertEqual(client.update_calls, [])

    async def test_preview_rejects_invalid_pipeline(self):
        invalid = dashboard()
        invalid["pipeline"]["valid"] = False
        invalid["pipeline"]["errors"] = ["bad"]
        client = FakeGitHub()
        with patch("app.writeback.build_dashboard", new=AsyncMock(return_value=invalid)):
            with self.assertRaises(RoadmapWritebackError) as caught:
                await build_roadmap_writeback_preview(
                    client,
                    self.settings,
                    "tchi99/RessourcePlanner",
                )
        self.assertEqual(caught.exception.code, "PIPELINE_INVALID")

    async def test_apply_requires_explicit_confirmation(self):
        client = FakeGitHub()
        with self.assertRaises(RoadmapWritebackError) as caught:
            await apply_roadmap_writeback(
                client,
                self.settings,
                "tchi99/RessourcePlanner",
                expected_updated_at=client.updated_at,
                expected_body_sha256=_sha256(BODY),
                expected_proposal_sha256=_sha256(PROPOSED_BLOCK),
                confirm=False,
            )
        self.assertEqual(caught.exception.code, "CONFIRMATION_REQUIRED")
        self.assertEqual(client.update_calls, [])

    async def test_apply_fails_closed_when_body_changed_since_preview(self):
        client = FakeGitHub(body=BODY + "\nConcurrent edit")
        with patch("app.writeback.build_dashboard", new=AsyncMock(return_value=dashboard())):
            with self.assertRaises(RoadmapWritebackError) as caught:
                await apply_roadmap_writeback(
                    client,
                    self.settings,
                    "tchi99/RessourcePlanner",
                    expected_updated_at="2026-09-24T19:30:00Z",
                    expected_body_sha256=_sha256(BODY),
                    expected_proposal_sha256=_sha256(PROPOSED_BLOCK),
                    confirm=True,
                )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "ROADMAP_CHANGED")
        self.assertEqual(client.update_calls, [])

    async def test_apply_fails_when_recomputed_proposal_changed(self):
        changed_proposal = PROPOSED_BLOCK.replace(
            "408 | WORK | READY",
            "408 | WORK | DONE",
        )
        client = FakeGitHub()
        with patch(
            "app.writeback.build_dashboard",
            new=AsyncMock(return_value=dashboard(proposed_block=changed_proposal)),
        ):
            with self.assertRaises(RoadmapWritebackError) as caught:
                await apply_roadmap_writeback(
                    client,
                    self.settings,
                    "tchi99/RessourcePlanner",
                    expected_updated_at=client.updated_at,
                    expected_body_sha256=_sha256(BODY),
                    expected_proposal_sha256=_sha256(PROPOSED_BLOCK),
                    confirm=True,
                )
        self.assertEqual(caught.exception.code, "PROPOSAL_CHANGED")
        self.assertEqual(client.update_calls, [])

    async def test_apply_recomputes_and_updates_only_canonical_block(self):
        client = FakeGitHub()
        with patch("app.writeback.build_dashboard", new=AsyncMock(return_value=dashboard())):
            result = await apply_roadmap_writeback(
                client,
                self.settings,
                "tchi99/RessourcePlanner",
                expected_updated_at=client.updated_at,
                expected_body_sha256=_sha256(BODY),
                expected_proposal_sha256=_sha256(PROPOSED_BLOCK),
                confirm=True,
            )

        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(client.update_calls), 1)
        written = client.update_calls[0][2]
        self.assertIn(PROPOSED_BLOCK, written)
        self.assertIn("Texte humain à préserver.", written)
        self.assertIn("Ne jamais modifier cette section.", written)
        self.assertNotIn(CURRENT_BLOCK, written)
        self.assertEqual(result["pipeline_block"], PROPOSED_BLOCK)
        self.assertGreaterEqual(client.get_calls.count(55), 4)

    async def test_apply_targets_configured_pipeline_issue_not_human_roadmap(self):
        client = FakeGitHub(body=CURRENT_BLOCK)
        settings = Settings(
            github_token="test",
            repository="tchi99/RessourcePlanner",
            roadmap_issue=55,
        )
        self.assertEqual(settings.canonical_pipeline_issue, 851)
        self.assertEqual(
            settings.canonical_pipeline_source_mode,
            PIPELINE_SOURCE_DEDICATED_STRICT,
        )
        strict_dashboard = dashboard()
        strict_dashboard["pipeline"]["steps"] = [
            step.to_dict() for step in parse_cockpit_pipeline(CURRENT_BLOCK).steps
        ]
        with patch(
            "app.writeback.build_dashboard",
            new=AsyncMock(return_value=strict_dashboard),
        ):
            result = await apply_roadmap_writeback(
                client,
                settings,
                "tchi99/RessourcePlanner",
                expected_updated_at=client.updated_at,
                expected_body_sha256=_sha256(CURRENT_BLOCK),
                expected_proposal_sha256=_sha256(PROPOSED_BLOCK),
                confirm=True,
            )

        self.assertEqual(client.update_calls[0][1], 851)
        self.assertNotIn(55, [number for _, number, _ in client.update_calls])
        self.assertEqual(result["human_roadmap_issue"], 55)
        self.assertEqual(result["pipeline_issue"], 851)

    async def test_apply_fails_if_independent_post_write_get_differs(self):
        client = FakeGitHub(post_write_get_body=PROPOSED_BLOCK + "\nconcurrent tail")
        with patch("app.writeback.build_dashboard", new=AsyncMock(return_value=dashboard())):
            with self.assertRaises(RoadmapWritebackError) as caught:
                await apply_roadmap_writeback(
                    client,
                    self.settings,
                    "tchi99/RessourcePlanner",
                    expected_updated_at=client.updated_at,
                    expected_body_sha256=_sha256(BODY),
                    expected_proposal_sha256=_sha256(PROPOSED_BLOCK),
                    confirm=True,
                )

        self.assertEqual(caught.exception.code, "WRITEBACK_VERIFICATION_FAILED")
        self.assertEqual(caught.exception.status_code, 502)

    async def test_github_permission_failure_is_not_swallowed(self):
        client = FakeGitHub(update_error=GitHubError(403, "Resource not accessible"))
        with patch("app.writeback.build_dashboard", new=AsyncMock(return_value=dashboard())):
            with self.assertRaises(GitHubError) as caught:
                await apply_roadmap_writeback(
                    client,
                    self.settings,
                    "tchi99/RessourcePlanner",
                    expected_updated_at=client.updated_at,
                    expected_body_sha256=_sha256(BODY),
                    expected_proposal_sha256=_sha256(PROPOSED_BLOCK),
                    confirm=True,
                )
        self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
