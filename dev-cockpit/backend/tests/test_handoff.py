import unittest

from app.execution import build_execution_control
from app.handoff import build_handoff_packs


AGENTS = """
When asked to implement an approved issue or sub-issue, continue autonomously through:
→ implementation
→ targeted tests
→ create/update PR
→ CI
→ CI green
→ automatic merge

Do not modify a failing test merely to make CI green.
A PR should represent one coherent issue or sub-issue.
Always synchronize with main after the previous PR is merged before beginning the next sub-item.
Do not automatically consume arbitrary backlog issues outside the approved block.
A task is not complete merely because code exists on a branch.
"""


def active_work(**patch):
    base = {
        "key": "276A",
        "issue_number": 276,
        "title": "contrat multi-approbation",
        "issue": {
            "number": 276,
            "title": "Multi-approbation par ligne",
            "url": "https://github.test/issues/276",
        },
        "subitem_key": "276A",
        "states": ["READY"],
        "failed_jobs": [],
        "primary_pr": None,
        "active_branch": None,
        "last_commit": None,
        "active_runs": [],
        "merged_but_unmarked_pr": None,
        "can_chain_block": True,
        "remaining_subitems": ["276A", "276B", "276C", "276D"],
    }
    base.update(patch)
    return base


def execution_for(
    work,
    *,
    reconciliation=None,
    pipeline_valid=True,
    canonical_pipeline_issue=55,
    canonical_pipeline_url=None,
):
    return build_execution_control(
        roadmap_issue=canonical_pipeline_issue,
        roadmap_url=canonical_pipeline_url or f"https://github.test/issues/{canonical_pipeline_issue}",
        roadmap_updated_at="2026-09-24T18:00:00Z",
        pipeline_valid=pipeline_valid,
        pipeline_now={
            "key": "276A",
            "kind": "WORK",
            "issue_number": 276,
            "status": "READY",
            "title": "contrat et référentiels de multi-approbation",
        }
        if pipeline_valid
        else None,
        reconciliation=reconciliation
        or {
            "status": "coherent",
            "summary": "Aucun écart.",
            "findings": [],
            "proposal": None,
        },
        active_work=work,
        fallback_next_action="Démarrer 276A.",
        fallback_dev_prompt="Implémente 276A selon AGENTS.md.",
    )


def packs(
    work,
    *,
    reconciliation=None,
    pipeline_valid=True,
    issue_body=None,
    canonical_pipeline_issue=55,
    canonical_pipeline_url=None,
):
    execution = execution_for(
        work,
        reconciliation=reconciliation,
        pipeline_valid=pipeline_valid,
        canonical_pipeline_issue=canonical_pipeline_issue,
        canonical_pipeline_url=canonical_pipeline_url,
    )
    return build_handoff_packs(
        roadmap_issue=55,
        roadmap_url="https://github.test/issues/55",
        canonical_pipeline_issue=canonical_pipeline_issue,
        canonical_pipeline_url=canonical_pipeline_url
        or f"https://github.test/issues/{canonical_pipeline_issue}",
        pipeline_now={
            "key": "276A",
            "kind": "WORK",
            "issue_number": 276,
            "status": "READY",
            "title": "contrat et référentiels de multi-approbation",
        }
        if pipeline_valid
        else None,
        execution=execution,
        active_work=work,
        issue_body=issue_body
        or """
# #276 — multi-approbation

### #276A — contrat et référentiels
Définir les identités stables, les classes métier admissibles et les tests de contrat.

### #276B — cycles persistants
Suite.
""",
        referenced_adrs=[
            {
                "name": "ADR-010-multi-approval.md",
                "url": "https://github.test/blob/main/docs/architecture/ADR-010-multi-approval.md",
            }
        ],
        agents_text=AGENTS,
    )


class HandoffPackTests(unittest.TestCase):
    def test_ready_developer_pack_is_complete_with_section_adr_and_chain(self):
        result = packs(active_work())
        pack = result["packs"]["developer"]

        self.assertEqual(result["active_key"], "276A")
        self.assertEqual(pack["confidence"], "COMPLETE")
        self.assertEqual(pack["missing"], [])
        self.assertEqual(pack["section"]["title"], "#276A — contrat et référentiels")
        self.assertIn("Définir les identités stables", pack["prompt"])
        self.assertIn("ADR-010-multi-approval.md", pack["prompt"])
        self.assertIn("276A → 276B → 276C → 276D", pack["prompt"])
        self.assertIn("Ne pas affaiblir", pack["prompt"])
        self.assertIn("Ne recommence pas l'analyse", pack["prompt"])
        self.assertTrue(any(source["kind"] == "adr" for source in pack["sources"]))

    def test_developing_without_branch_or_pr_is_partial(self):
        result = packs(
            active_work(
                states=["IN_PROGRESS"],
                active_branch=None,
                primary_pr=None,
            )
        )
        pack = result["packs"]["developer"]

        self.assertEqual(pack["confidence"], "PARTIAL")
        self.assertIn("branche ou PR active observable", pack["missing"])

    def test_ci_red_pack_contains_pr_and_failed_jobs(self):
        work = active_work(
            states=["IN_PROGRESS", "CI_RED"],
            failed_jobs=["approval-contract-tests", "privacy-scan"],
            primary_pr={
                "number": 431,
                "title": "feat(276A): approval contract",
                "state": "open",
                "url": "https://github.test/pull/431",
                "runs": [],
            },
            active_branch={
                "name": "feat/276a-approval-contract",
                "url": "https://github.test/tree/feat/276a-approval-contract",
            },
        )
        pack = packs(work)["packs"]["developer"]

        self.assertEqual(pack["confidence"], "COMPLETE")
        self.assertIn("PR : #431", pack["prompt"])
        self.assertIn("approval-contract-tests, privacy-scan", pack["prompt"])

    def test_roadmap_stale_blocks_developer_but_gives_po_complete_pack(self):
        reconciliation = {
            "status": "stale",
            "summary": "Roadmap stale.",
            "findings": [],
            "proposal": {
                "changes": [
                    {"key": "276A", "from": "READY", "to": "DONE"},
                    {"key": "276B", "from": "BLOCKED", "to": "READY"},
                ]
            },
        }
        result = packs(active_work(), reconciliation=reconciliation)

        self.assertEqual(result["packs"]["developer"]["confidence"], "BLOCKED")
        self.assertEqual(result["packs"]["product-owner"]["confidence"], "COMPLETE")
        self.assertIn(
            "Réconcilier le pipeline #55",
            result["packs"]["product-owner"]["prompt"],
        )

    def test_split_identity_labels_human_roadmap_and_canonical_pipeline(self):
        reconciliation = {
            "status": "stale",
            "summary": "Pipeline stale.",
            "findings": [],
            "proposal": {
                "changes": [
                    {"key": "276A", "from": "READY", "to": "DONE"},
                    {"key": "276B", "from": "BLOCKED", "to": "READY"},
                ]
            },
        }
        result = packs(
            active_work(),
            reconciliation=reconciliation,
            canonical_pipeline_issue=851,
            canonical_pipeline_url="https://github.test/issues/851",
        )
        prompt = result["packs"]["product-owner"]["prompt"]
        labels = [source["label"] for source in result["packs"]["product-owner"]["sources"]]

        self.assertIn("- Roadmap humain : #55", prompt)
        self.assertIn("- Pipeline canonique : #851", prompt)
        self.assertIn("Réconcilier le pipeline #851", prompt)
        self.assertIn("Roadmap humain : #55.", prompt)
        self.assertIn("Pipeline canonique COCKPIT_PIPELINE_V1 : #851.", prompt)
        self.assertIn("Roadmap humain #55", labels)
        self.assertIn("Pipeline canonique #851", labels)

    def test_invalid_pipeline_blocks_handoff(self):
        execution = build_execution_control(
            roadmap_issue=55,
            roadmap_url="https://github.test/issues/55",
            roadmap_updated_at="2026-09-24T18:00:00Z",
            pipeline_valid=False,
            pipeline_now=None,
            reconciliation={
                "status": "invalid",
                "summary": "Pipeline invalide.",
                "findings": [],
                "proposal": None,
            },
            active_work=None,
            fallback_next_action="Corriger #55.",
            fallback_dev_prompt="Ne démarre rien.",
        )
        result = build_handoff_packs(
            roadmap_issue=55,
            roadmap_url="https://github.test/issues/55",
            pipeline_now=None,
            execution=execution,
            active_work=None,
            issue_body="",
            referenced_adrs=[],
            agents_text=AGENTS,
        )

        self.assertEqual(result["packs"]["developer"]["confidence"], "BLOCKED")
        self.assertEqual(result["packs"]["product-owner"]["confidence"], "BLOCKED")
        self.assertIn("pipeline canonique", result["packs"]["developer"]["prompt"].lower())

    def test_reviewer_pack_is_targeted_to_pr_ci_and_risk(self):
        work = active_work(
            states=["IN_PROGRESS", "CI_RED"],
            failed_jobs=["quorum-tests"],
            primary_pr={
                "number": 432,
                "title": "feat(276A): quorum contract",
                "state": "open",
                "url": "https://github.test/pull/432",
                "runs": [],
            },
        )
        pack = packs(work)["packs"]["reviewer"]

        self.assertEqual(pack["confidence"], "COMPLETE")
        self.assertIn("quorum-tests", pack["prompt"])
        self.assertIn("diff/PR", pack["prompt"])
        self.assertIn("risques de régression", pack["prompt"])


if __name__ == "__main__":
    unittest.main()
