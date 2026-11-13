import unittest

from app.execution import build_execution_control


def active_work(**patch):
    base = {
        "key": "276",
        "issue_number": 276,
        "issue": {
            "number": 276,
            "title": "Approval routing",
            "url": "https://github.test/issues/276",
        },
        "states": ["READY"],
        "failed_jobs": [],
        "primary_pr": None,
        "active_branch": None,
        "last_commit": None,
        "active_runs": [],
        "merged_but_unmarked_pr": None,
    }
    base.update(patch)
    return base


def control(
    *,
    work=None,
    reconciliation=None,
    pipeline_valid=True,
    pipeline_now=None,
    pipeline_issue=55,
):
    return build_execution_control(
        roadmap_issue=pipeline_issue,
        roadmap_url=f"https://github.test/issues/{pipeline_issue}",
        roadmap_updated_at="2026-09-24T17:00:00Z",
        pipeline_valid=pipeline_valid,
        pipeline_now=pipeline_now
        or {
            "key": "276",
            "kind": "WORK",
            "issue_number": 276,
            "title": "approval routing",
        },
        reconciliation=reconciliation
        or {
            "status": "coherent",
            "summary": "Aucun écart.",
            "findings": [],
            "proposal": None,
        },
        active_work=work,
        fallback_next_action="Démarrer 276.",
        fallback_dev_prompt="Implémente #276 selon AGENTS.md.",
    )


class ExecutionControllerTests(unittest.TestCase):
    def test_ready_phase_and_developer_mission(self):
        result = control(work=active_work())

        self.assertEqual(result["phase"], "READY")
        self.assertEqual(result["responsible_role"], "developer")
        self.assertIn("Démarrer", result["next_action"])
        self.assertEqual(result["missions"]["developer"]["state"], "action")
        self.assertIn("276", result["missions"]["developer"]["prompt"])

    def test_branch_without_pr_is_developing(self):
        result = control(
            work=active_work(
                states=["IN_PROGRESS"],
                active_branch={
                    "name": "feat/276-routing",
                    "url": "https://github.test/tree/feat/276-routing",
                    "sha": "abc",
                },
            )
        )

        self.assertEqual(result["phase"], "DEVELOPING")
        self.assertIn("Poursuivre", result["next_action"])
        self.assertEqual(
            result["primary_link"]["url"],
            "https://github.test/tree/feat/276-routing",
        )

    def test_ci_red_prioritizes_fix_and_reviewer_mission(self):
        result = control(
            work=active_work(
                states=["IN_PROGRESS", "CI_RED"],
                failed_jobs=["approval-routing-tests"],
                primary_pr={
                    "number": 430,
                    "state": "open",
                    "url": "https://github.test/pull/430",
                },
            )
        )

        self.assertEqual(result["phase"], "CI_RED")
        self.assertIn("Corriger la CI", result["next_action"])
        self.assertIn("approval-routing-tests", result["prompt"])
        self.assertEqual(result["missions"]["reviewer"]["state"], "action")
        self.assertIn(
            "approval-routing-tests",
            result["missions"]["reviewer"]["detail"],
        )

    def test_mergeable_pr_is_ready_to_merge(self):
        result = control(
            work=active_work(
                states=["IN_PROGRESS", "MERGEABLE"],
                primary_pr={
                    "number": 430,
                    "state": "open",
                    "url": "https://github.test/pull/430",
                },
            )
        )

        self.assertEqual(result["phase"], "READY_TO_MERGE")
        self.assertEqual(result["responsible_role"], "reviewer")
        self.assertEqual(result["missions"]["reviewer"]["state"], "action")
        self.assertEqual(result["missions"]["developer"]["state"], "waiting")

    def test_stale_roadmap_overrides_active_delivery_state(self):
        result = control(
            work=active_work(
                states=["IN_PROGRESS", "MERGEABLE"],
                primary_pr={
                    "number": 430,
                    "state": "open",
                    "url": "https://github.test/pull/430",
                },
            ),
            reconciliation={
                "status": "stale",
                "summary": "Roadmap stale.",
                "findings": [],
                "proposal": {
                    "changes": [
                        {"key": "276", "from": "READY", "to": "DONE"},
                        {"key": "410", "from": "BLOCKED", "to": "READY"},
                    ]
                },
            },
        )

        self.assertEqual(result["phase"], "ROADMAP_UPDATE_REQUIRED")
        self.assertEqual(result["responsible_role"], "product-owner")
        self.assertIn("276: READY → DONE", result["next_action"])
        self.assertEqual(result["missions"]["developer"]["state"], "blocked")
        self.assertEqual(result["missions"]["product-owner"]["state"], "action")

    def test_invalid_pipeline_blocks_all_new_dev_work(self):
        result = control(
            work=None,
            pipeline_valid=False,
            reconciliation={
                "status": "invalid",
                "summary": "Pipeline invalide.",
                "findings": [],
                "proposal": None,
            },
        )

        self.assertEqual(result["phase"], "PIPELINE_INVALID")
        self.assertEqual(result["missions"]["product-owner"]["state"], "blocked")
        self.assertEqual(result["missions"]["developer"]["state"], "blocked")
        self.assertIn("Ne démarre aucune tranche", result["prompt"])

    def test_architecture_gate_assigns_architect_and_blocks_developer(self):
        result = control(
            work=active_work(
                key="ASTRA-362",
                issue_number=362,
                issue={
                    "number": 362,
                    "title": "Delivery architecture",
                    "url": "https://github.test/issues/362",
                },
            ),
            pipeline_now={
                "key": "ASTRA-362",
                "kind": "ARCHITECTURE_GATE",
                "issue_number": 362,
                "title": "analyse architecture Delivery",
            },
        )

        self.assertEqual(result["phase"], "ARCHITECTURE_GATE")
        self.assertEqual(result["responsible_role"], "architect")
        self.assertEqual(result["missions"]["architect"]["state"], "action")
        self.assertEqual(result["missions"]["developer"]["state"], "blocked")

    def test_split_source_operational_guidance_targets_canonical_pipeline(self):
        architecture = control(
            work=active_work(
                key="ASTRA-362",
                issue_number=362,
                issue={
                    "number": 362,
                    "title": "Delivery architecture",
                    "url": "https://github.test/issues/362",
                },
            ),
            pipeline_issue=851,
            pipeline_now={
                "key": "ASTRA-362",
                "kind": "ARCHITECTURE_GATE",
                "issue_number": 362,
                "title": "analyse architecture Delivery",
            },
        )
        architect_prompt = architecture["missions"]["architect"]["prompt"]
        self.assertIn("pipeline #851", architect_prompt)
        self.assertIn("mets le pipeline #851 à jour", architect_prompt)
        self.assertNotIn("mets #55 à jour", architect_prompt)

        ready_to_merge = control(
            pipeline_issue=851,
            work=active_work(
                states=["IN_PROGRESS", "MERGEABLE"],
                primary_pr={
                    "number": 430,
                    "state": "open",
                    "url": "https://github.test/pull/430",
                },
            ),
        )
        self.assertIn(
            "réconciliation du pipeline #851",
            ready_to_merge["prompt"],
        )
        self.assertNotIn("réconciliation de #55", ready_to_merge["prompt"])

        delivery_unverified = control(
            pipeline_issue=851,
            work=active_work(
                states=["READY"],
                merged_but_unmarked_pr={
                    "number": 430,
                    "url": "https://github.test/pull/430",
                },
            ),
        )
        self.assertEqual(delivery_unverified["phase"], "DELIVERY_UNVERIFIED")
        self.assertIn(
            "réconcilier le pipeline #851",
            delivery_unverified["next_action"],
        )
        self.assertNotIn("réconcilier #55", delivery_unverified["next_action"])

    def test_timeline_is_derived_from_gitHub_timestamps(self):
        result = control(
            work=active_work(
                states=["IN_PROGRESS", "CI_RUNNING"],
                active_branch={
                    "name": "feat/276-routing",
                    "url": "https://github.test/tree/feat/276-routing",
                    "sha": "abc",
                },
                last_commit={
                    "sha": "abc123",
                    "short_sha": "abc123",
                    "message": "feat(276): route approval",
                    "date": "2026-09-24T17:20:00Z",
                    "url": "https://github.test/commit/abc123",
                },
                primary_pr={
                    "number": 430,
                    "title": "feat(276): route approval",
                    "state": "open",
                    "url": "https://github.test/pull/430",
                    "created_at": "2026-09-24T17:10:00Z",
                    "runs": [],
                },
                active_runs=[
                    {
                        "id": 900,
                        "name": "CI",
                        "run_number": 900,
                        "status": "in_progress",
                        "conclusion": None,
                        "created_at": "2026-09-24T17:25:00Z",
                        "updated_at": "2026-09-24T17:25:00Z",
                        "url": "https://github.test/actions/900",
                    }
                ],
            )
        )

        self.assertEqual(result["phase"], "CI_RUNNING")
        labels = [event["label"] for event in result["timeline"]]
        self.assertEqual(labels[0], "CI #900 démarrée")
        self.assertIn("Commit abc123", labels)
        self.assertIn("PR #430 ouverte", labels)
        self.assertIn("Pipeline #55 mis à jour", labels)


if __name__ == "__main__":
    unittest.main()
