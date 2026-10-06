from __future__ import annotations

import csv
from io import StringIO
import unittest

from app.application.verification_documents import (
    render_phase_report_html,
    render_test_plan_html,
    render_traceability_csv,
)


class VerificationDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "generated_at": "2026-10-06T18:30:00+00:00",
            "context": {
                "project": {
                    "id": "P-1",
                    "number": "P-001",
                    "name": "Projet & usine",
                    "client": "Client",
                },
                "work_package": {
                    "id": "WP-1",
                    "code": "WP-FAT",
                    "name": "Validation terrain",
                },
                "stories": {
                    "STORY-1": {
                        "id": "STORY-1",
                        "title": "Démarrage pompe",
                        "epic_id": "EPIC-1",
                        "epic_title": "Séquence principale",
                    },
                    "STORY-2": {
                        "id": "STORY-2",
                        "title": "Documentation",
                        "epic_id": None,
                        "epic_title": None,
                    },
                },
                "story_decisions": [
                    {
                        "id": "DEC-1",
                        "story_id": "STORY-1",
                        "kind": "TESTS_DEFINED",
                        "justification": None,
                        "created_at": "2026-10-06T17:00:00+00:00",
                        "requirement_ids": ["REQ-1"],
                    },
                    {
                        "id": "DEC-2",
                        "story_id": "STORY-2",
                        "kind": "NO_TEST_REQUIRED",
                        "justification": "Documentation seulement",
                        "created_at": "2026-10-06T17:30:00+00:00",
                        "requirement_ids": [],
                    },
                ],
            },
            "package": {
                "work_package_id": "WP-1",
                "verification_scope_id": "VS-1",
                "verification_version": 7,
                "story_decision_coverage": {
                    "done_story_count": 2,
                    "documented_done_story_count": 2,
                    "undocumented_done_story_ids": [],
                    "complete": True,
                    "coverage_rate": 1.0,
                },
                "phases": {
                    "FAT": {
                        "active": 1,
                        "PASS": 1,
                        "FAIL": 0,
                        "BLOCKED": 0,
                        "NOT_RUN": 0,
                    },
                    "SAT": {
                        "active": 0,
                        "PASS": 0,
                        "FAIL": 0,
                        "BLOCKED": 0,
                        "NOT_RUN": 0,
                    },
                    "COMMISSIONING": {
                        "active": 0,
                        "PASS": 0,
                        "FAIL": 0,
                        "BLOCKED": 0,
                        "NOT_RUN": 0,
                    },
                },
                "requirements": [
                    {
                        "id": "REQ-1",
                        "story_id": "STORY-1",
                        "phase": "FAT",
                        "state": "ACTIVE",
                        "withdrawal_reason": None,
                        "current_revision": {
                            "id": "REV-2",
                            "revision_number": 2,
                            "objective": "Valider <pompe>",
                            "method": "Démarrer & observer",
                            "expected_result": "Pompe en marche",
                            "prerequisites": ["Alimentation disponible"],
                            "criticality": "HIGH",
                        },
                        "projected_status": "PASS",
                        "latest_execution_id": "EX-2",
                        "executions": [
                            {
                                "id": "EX-1",
                                "revision_id": "REV-1",
                                "sequence": 1,
                                "result": "FAIL",
                                "executor_user_id": "TECH",
                                "executed_at": None,
                                "recorded_at": "2026-10-06T17:10:00+00:00",
                                "measurements": {},
                                "comments": "Ancienne révision",
                                "evidence_links": [],
                            },
                            {
                                "id": "EX-2",
                                "revision_id": "REV-2",
                                "sequence": 2,
                                "result": "PASS",
                                "executor_user_id": "TECH",
                                "executed_at": "2026-10-06T17:20:00+00:00",
                                "recorded_at": "2026-10-06T17:21:00+00:00",
                                "measurements": {"pression_bar": 5.4},
                                "comments": "Conforme",
                                "evidence_links": [
                                    {
                                        "url": "https://example.com/proof",
                                        "label": "Capture FAT",
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "id": "REQ-OLD",
                        "story_id": "STORY-1",
                        "phase": "SAT",
                        "state": "WITHDRAWN",
                        "withdrawal_reason": "Remplacée",
                        "current_revision": {
                            "id": "REV-OLD",
                            "revision_number": 1,
                            "objective": "Ancienne exigence",
                            "method": "Ancienne méthode",
                            "expected_result": "Ancien résultat",
                            "prerequisites": [],
                            "criticality": None,
                        },
                        "projected_status": None,
                        "latest_execution_id": None,
                        "executions": [],
                    },
                ],
            },
        }

    def test_printable_plan_uses_current_active_revision_and_escapes_html(self) -> None:
        html = render_test_plan_html(self.snapshot)

        self.assertIn("Plan de test Verification", html)
        self.assertIn("REV-2", html)
        self.assertIn("Valider &lt;pompe&gt;", html)
        self.assertNotIn("Valider <pompe>", html)
        self.assertNotIn("Ancienne exigence", html)
        self.assertIn("2 / 2", html)

    def test_phase_report_uses_selected_current_execution_and_evidence(self) -> None:
        html = render_phase_report_html(self.snapshot, "FAT")

        self.assertIn("Rapport Verification — FAT", html)
        self.assertIn("EX-2", html)
        self.assertIn("REV-2", html)
        self.assertIn("PASS", html)
        self.assertIn("Conforme", html)
        self.assertIn("https://example.com/proof", html)
        self.assertNotIn("Ancienne révision", html)

    def test_traceability_csv_preserves_current_requirement_withdrawn_history_and_no_test_decision(self) -> None:
        rows = list(csv.DictReader(StringIO(render_traceability_csv(self.snapshot))))

        self.assertEqual(len(rows), 3)
        active = next(row for row in rows if row["requirement_id"] == "REQ-1")
        self.assertEqual(active["current_revision_id"], "REV-2")
        self.assertEqual(active["selected_execution_id"], "EX-2")
        self.assertEqual(active["selected_result"], "PASS")
        self.assertEqual(active["decision_kind"], "TESTS_DEFINED")
        self.assertEqual(active["selected_by_latest_decision"], "True")
        self.assertIn("https://example.com/proof", active["evidence_urls"])

        withdrawn = next(row for row in rows if row["requirement_id"] == "REQ-OLD")
        self.assertEqual(withdrawn["requirement_state"], "WITHDRAWN")
        self.assertEqual(withdrawn["withdrawal_reason"], "Remplacée")
        self.assertEqual(withdrawn["selected_by_latest_decision"], "False")

        no_test = next(row for row in rows if row["decision_kind"] == "NO_TEST_REQUIRED")
        self.assertEqual(no_test["story_id"], "STORY-2")
        self.assertEqual(no_test["projected_status"], "NO_TEST_REQUIRED")
        self.assertEqual(no_test["decision_justification"], "Documentation seulement")


if __name__ == "__main__":
    unittest.main()
