from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.application.security import (
    AuthPrincipal,
    ROLE_DELIVERY_CONTRIBUTOR,
    ROLE_TECHNICIAN,
)
from app.domain.delivery import (
    DeliveryItem,
    DeliveryItemStatus,
    DeliveryItemType,
    DeliveryPlan,
    DeliveryPlanStatus,
)
from app.domain.verification import (
    VerificationPhase,
    VerificationRequirement,
    VerificationRequirementRevision,
    VerificationScope,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    DeliveryItemRow,
    Project,
    SqlDeliveryRepository,
    SqlVerificationRepository,
    WorkPackage,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


def principal(user_id: str, *roles: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=user_id,
        issuer="urn:test",
        subject=f"subject-{user_id}",
        display_name=user_id,
        email=None,
        roles=roles,
        auth_mode="test",
    )


class VerificationExecutionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "verification-execution.db"
        bootstrap = self._app(principal("LEAD", ROLE_DELIVERY_CONTRIBUTOR))
        self.factory = bootstrap.state.session_factory
        Base.metadata.create_all(self.factory.kw["bind"])

        with self.factory.begin() as session:
            session.add(Project(id="P-1", number="P-1", name="Projet"))
            session.add(WorkPackage(id="WP-1", project_id="P-1", name="WP"))
            session.add_all(
                [
                    AppUser(
                        id="LEAD",
                        issuer="urn:test",
                        subject="subject-LEAD",
                        display_name="Lead",
                        roles_json=json.dumps([ROLE_DELIVERY_CONTRIBUTOR]),
                        active=True,
                    ),
                    AppUser(
                        id="TECH",
                        issuer="urn:test",
                        subject="subject-TECH",
                        display_name="Technicien",
                        roles_json=json.dumps([ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR]),
                        active=True,
                    ),
                    AppUser(
                        id="TECH2",
                        issuer="urn:test",
                        subject="subject-TECH2",
                        display_name="Autre technicien",
                        roles_json=json.dumps([ROLE_TECHNICIAN]),
                        active=True,
                    ),
                ]
            )

        with self.factory.begin() as session:
            delivery = SqlDeliveryRepository(session)
            delivery.add_plan(
                DeliveryPlan(
                    id="DP-1",
                    work_package_id="WP-1",
                    status=DeliveryPlanStatus.ACTIVE,
                    lead_user_id="LEAD",
                )
            )
            delivery.add_item(
                DeliveryItem(
                    id="STORY-1",
                    delivery_plan_id="DP-1",
                    item_type=DeliveryItemType.STORY,
                    title="FAT pompe",
                    status=DeliveryItemStatus.IN_PROGRESS,
                    assignee_user_id="TECH",
                    current_estimate_hours=4,
                    reference_estimate_hours=4,
                    remaining_hours=1,
                )
            )
            verification = SqlVerificationRepository(session)
            verification.add_scope(
                VerificationScope(
                    id="VS-1",
                    work_package_id="WP-1",
                    lead_user_id="LEAD",
                )
            )
            verification.add_requirement(
                VerificationRequirement(
                    id="REQ-1",
                    verification_scope_id="VS-1",
                    story_id="STORY-1",
                    phase=VerificationPhase.FAT,
                    current_revision_id="REV-1",
                ),
                VerificationRequirementRevision(
                    id="REV-1",
                    requirement_id="REQ-1",
                    revision_number=1,
                    objective="Valider démarrage pompe",
                    method="Démarrer et observer",
                    expected_result="Pompe en marche sans alarme",
                    prerequisites=("Alimentation disponible",),
                    criticality="HIGH",
                ),
            )
            story = session.get(DeliveryItemRow, "STORY-1")
            assert story is not None
            story.status = "DONE"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _app(self, actor: AuthPrincipal):
        return create_api_app(
            f"sqlite+pysqlite:///{self.database_path.as_posix()}",
            auth_resolver=static_auth_resolver(actor),
        )

    def test_assignment_execution_evidence_retest_and_projection(self) -> None:
        lead_app = self._app(principal("LEAD", ROLE_DELIVERY_CONTRIBUTOR))
        with TestClient(lead_app) as client:
            assigned = client.post(
                "/api/v1/verification/requirements/REQ-1/assignments",
                headers={"Idempotency-Key": "363d-assign"},
                json={
                    "executor_user_id": "TECH",
                    "expected_verification_version": 1,
                },
            )
            self.assertEqual(assigned.status_code, 200, assigned.text)
            self.assertEqual(assigned.json()["verification_version"], 2)

            replay = client.post(
                "/api/v1/verification/requirements/REQ-1/assignments",
                headers={"Idempotency-Key": "363d-assign"},
                json={
                    "executor_user_id": "TECH",
                    "expected_verification_version": 1,
                },
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), assigned.json())

        other_app = self._app(principal("TECH2", ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR))
        with TestClient(other_app) as client:
            denied = client.post(
                "/api/v1/verification/requirements/REQ-1/executions",
                headers={"Idempotency-Key": "363d-not-assigned"},
                json={
                    "result": "PASS",
                    "expected_verification_version": 2,
                },
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(
                denied.json()["error"]["code"],
                "verification_action_denied",
            )

        tech_app = self._app(principal("TECH", ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR))
        with TestClient(tech_app) as client:
            executed = client.post(
                "/api/v1/verification/requirements/REQ-1/executions",
                headers={"Idempotency-Key": "363d-pass"},
                json={
                    "result": "PASS",
                    "measurements": {"pressure_bar": 5.4, "alarm": False},
                    "comments": "FAT conforme",
                    "expected_verification_version": 2,
                },
            )
            self.assertEqual(executed.status_code, 200, executed.text)
            self.assertEqual(executed.json()["sequence"], 1)
            self.assertEqual(executed.json()["verification_version"], 3)
            execution_id = executed.json()["execution_id"]

            invalid_evidence = client.post(
                f"/api/v1/verification/executions/{execution_id}/evidence-links",
                headers={"Idempotency-Key": "363d-http-proof"},
                json={
                    "url": "http://files.example/proof",
                    "expected_verification_version": 3,
                },
            )
            self.assertEqual(invalid_evidence.status_code, 422, invalid_evidence.text)
            self.assertEqual(
                invalid_evidence.json()["error"]["code"],
                "verification_evidence_invalid",
            )

            evidence = client.post(
                f"/api/v1/verification/executions/{execution_id}/evidence-links",
                headers={"Idempotency-Key": "363d-https-proof"},
                json={
                    "url": "https://files.example/fat/proof-1",
                    "label": "Capture FAT",
                    "provenance": "external_https",
                    "expected_verification_version": 3,
                },
            )
            self.assertEqual(evidence.status_code, 200, evidence.text)
            self.assertEqual(evidence.json()["verification_version"], 4)

            package = client.get(
                "/api/v1/verification/work-packages/WP-1/package"
            )
            self.assertEqual(package.status_code, 200, package.text)
            payload = package.json()
            self.assertEqual(payload["requirements"][0]["projected_status"], "PASS")
            self.assertEqual(payload["phases"]["FAT"]["PASS"], 1)
            self.assertEqual(payload["pass_rate"], 1.0)
            self.assertFalse(payload["story_decision_coverage"]["complete"])
            self.assertEqual(
                payload["story_decision_coverage"]["undocumented_done_story_ids"],
                ["STORY-1"],
            )
            self.assertEqual(
                payload["requirements"][0]["executions"][0]["evidence_links"][0]["url"],
                "https://files.example/fat/proof-1",
            )

            test_plan = client.get(
                "/api/v1/verification/work-packages/WP-1/documents/test-plan"
            )
            self.assertEqual(test_plan.status_code, 200, test_plan.text)
            self.assertIn("text/html", test_plan.headers["content-type"])
            self.assertIn("inline;", test_plan.headers["content-disposition"])
            self.assertIn("Plan de test Verification", test_plan.text)
            self.assertIn("Valider démarrage pompe", test_plan.text)
            self.assertIn("REV-1", test_plan.text)

            fat_report = client.get(
                "/api/v1/verification/work-packages/WP-1/documents/reports/FAT"
            )
            self.assertEqual(fat_report.status_code, 200, fat_report.text)
            self.assertIn("Rapport Verification — FAT", fat_report.text)
            self.assertIn("FAT conforme", fat_report.text)
            self.assertIn("https://files.example/fat/proof-1", fat_report.text)

            traceability = client.get(
                "/api/v1/verification/work-packages/WP-1/documents/traceability.csv"
            )
            self.assertEqual(traceability.status_code, 200, traceability.text)
            self.assertIn("text/csv", traceability.headers["content-type"])
            self.assertIn("attachment;", traceability.headers["content-disposition"])
            self.assertIn("current_revision_id", traceability.text)
            self.assertIn("REV-1", traceability.text)
            self.assertIn("FAT conforme", traceability.text)

        with TestClient(lead_app) as client:
            retest = client.post(
                "/api/v1/verification/requirements/REQ-1/retests",
                headers={"Idempotency-Key": "363d-retest"},
                json={
                    "reason": "Valider après correction",
                    "expected_verification_version": 4,
                },
            )
            self.assertEqual(retest.status_code, 200, retest.text)
            self.assertEqual(retest.json()["after_execution_sequence"], 1)
            self.assertEqual(retest.json()["verification_version"], 5)

        with TestClient(tech_app) as client:
            pending = client.get(
                "/api/v1/verification/work-packages/WP-1/package"
            )
            self.assertEqual(pending.status_code, 200, pending.text)
            self.assertEqual(
                pending.json()["requirements"][0]["projected_status"],
                "NOT_RUN",
            )
            failed = client.post(
                "/api/v1/verification/requirements/REQ-1/executions",
                headers={"Idempotency-Key": "363d-fail"},
                json={
                    "result": "FAIL",
                    "comments": "Retest non conforme",
                    "expected_verification_version": 5,
                },
            )
            self.assertEqual(failed.status_code, 200, failed.text)
            self.assertEqual(failed.json()["sequence"], 2)
            self.assertEqual(failed.json()["verification_version"], 6)

            final = client.get(
                "/api/v1/verification/work-packages/WP-1/package"
            )
            self.assertEqual(final.status_code, 200, final.text)
            final_payload = final.json()
            self.assertEqual(
                final_payload["requirements"][0]["projected_status"],
                "FAIL",
            )
            self.assertEqual(final_payload["phases"]["FAT"]["FAIL"], 1)
            self.assertEqual(
                len(final_payload["requirements"][0]["executions"]),
                2,
            )

        with TestClient(lead_app) as client:
            replay_after_later_execution = client.post(
                "/api/v1/verification/requirements/REQ-1/retests",
                headers={"Idempotency-Key": "363d-retest"},
                json={
                    "reason": "Valider après correction",
                    "expected_verification_version": 4,
                },
            )
            self.assertEqual(
                replay_after_later_execution.status_code,
                200,
                replay_after_later_execution.text,
            )
            self.assertEqual(replay_after_later_execution.json(), retest.json())


if __name__ == "__main__":
    unittest.main()
