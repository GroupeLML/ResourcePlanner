from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

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
from app.domain.verification import VerificationScope
from app.infrastructure.sql import (
    AppUser,
    Base,
    CommandIdempotencyReceipt,
    DeliveryChangeHistory,
    DeliveryItemRow,
    DeliveryPlanRow,
    Project,
    SqlDeliveryRepository,
    SqlVerificationRepository,
    StoryVerificationDecisionRow,
    VerificationChangeHistory,
    VerificationRequirementRevisionRow,
    VerificationRequirementRow,
    VerificationScopeRow,
    WorkPackage,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


def principal(user_id: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=user_id,
        issuer="urn:test",
        subject=f"subject-{user_id}",
        display_name=user_id,
        email=None,
        roles=(ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR),
        auth_mode="test",
    )


class StoryVerificationClosureApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "story-verification.db"
        bootstrap = self._app(principal("LEAD"))
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
                        roles_json=json.dumps(
                            [ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR]
                        ),
                        active=True,
                    ),
                    AppUser(
                        id="TECH",
                        issuer="urn:test",
                        subject="subject-TECH",
                        display_name="Technicien",
                        roles_json=json.dumps(
                            [ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR]
                        ),
                        active=True,
                    ),
                ]
            )
        with self.factory.begin() as session:
            repository = SqlDeliveryRepository(session)
            repository.add_plan(
                DeliveryPlan(
                    id="DP-1",
                    work_package_id="WP-1",
                    status=DeliveryPlanStatus.ACTIVE,
                    lead_user_id="LEAD",
                )
            )
            repository.add_item(
                DeliveryItem(
                    id="STORY-1",
                    delivery_plan_id="DP-1",
                    item_type=DeliveryItemType.STORY,
                    title="Story",
                    status=DeliveryItemStatus.IN_PROGRESS,
                    assignee_user_id="TECH",
                    current_estimate_hours=8,
                    reference_estimate_hours=8,
                    remaining_hours=2,
                )
            )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _app(self, actor: AuthPrincipal):
        return create_api_app(
            f"sqlite+pysqlite:///{self.database_path.as_posix()}",
            auth_resolver=static_auth_resolver(actor),
        )

    @staticmethod
    def _tests_defined_body(
        *,
        expected_delivery_version: int,
        expected_verification_version: int | None,
        existing_requirement_ids: list[str] | None = None,
        objective: str = "Valider le démarrage",
    ) -> dict[str, object]:
        decision: dict[str, object] = {
            "kind": "TESTS_DEFINED",
            "existing_requirement_ids": existing_requirement_ids or [],
            "new_requirements": [],
        }
        if not existing_requirement_ids:
            decision["new_requirements"] = [
                {
                    "phase": "FAT",
                    "objective": objective,
                    "method": "Actionner la commande",
                    "expected_result": "La machine démarre sans alarme",
                    "prerequisites": ["Alimentation disponible"],
                    "criticality": "HIGH",
                }
            ]
        return {
            "expected_delivery_version": expected_delivery_version,
            "expected_verification_version": expected_verification_version,
            "decision": decision,
        }

    def test_generic_done_paths_are_rejected_without_verification_decision(self) -> None:
        tech_app = self._app(principal("TECH"))
        with TestClient(tech_app) as client:
            response = client.patch(
                "/api/v1/delivery/items/STORY-1",
                json={
                    "expected_delivery_version": 1,
                    "status": "DONE",
                    "remaining_hours": 0,
                },
            )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(
                response.json()["error"]["code"],
                "story_verification_decision_required",
            )

        lead_app = self._app(principal("LEAD"))
        with TestClient(lead_app) as client:
            response = client.post(
                "/api/v1/delivery/plans/DP-1/items",
                json={
                    "expected_delivery_version": 1,
                    "item_type": "STORY",
                    "title": "Direct DONE",
                    "status": "DONE",
                },
            )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(
                response.json()["error"]["code"],
                "story_verification_decision_required",
            )

        with self.factory() as session:
            plan = session.get(DeliveryPlanRow, "DP-1")
            story = session.get(DeliveryItemRow, "STORY-1")
            assert plan is not None and story is not None
            self.assertEqual(plan.delivery_version, 1)
            self.assertEqual(story.status, "IN_PROGRESS")
            self.assertEqual(
                session.scalar(select(func.count()).select_from(VerificationScopeRow)),
                0,
            )

    def test_atomic_close_replays_and_reopen_requires_explicit_reconfirmation(self) -> None:
        app = self._app(principal("TECH"))
        first_body = self._tests_defined_body(
            expected_delivery_version=1,
            expected_verification_version=None,
        )
        with TestClient(app) as client:
            first = client.post(
                "/api/v1/delivery/items/STORY-1/complete-with-verification",
                headers={"Idempotency-Key": "363c-close-1"},
                json=first_body,
            )
            self.assertEqual(first.status_code, 200, first.text)
            first_payload = first.json()
            self.assertEqual(first_payload["story_status"], "DONE")
            self.assertEqual(first_payload["delivery_version"], 2)
            self.assertEqual(first_payload["verification_version"], 1)
            self.assertFalse(first_payload["historical"])
            self.assertEqual(len(first_payload["requirement_ids"]), 1)
            requirement_id = first_payload["requirement_ids"][0]

            replay = client.post(
                "/api/v1/delivery/items/STORY-1/complete-with-verification",
                headers={"Idempotency-Key": "363c-close-1"},
                json=first_body,
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), first_payload)

            conflict_body = self._tests_defined_body(
                expected_delivery_version=1,
                expected_verification_version=None,
                objective="Autre définition",
            )
            conflict = client.post(
                "/api/v1/delivery/items/STORY-1/complete-with-verification",
                headers={"Idempotency-Key": "363c-close-1"},
                json=conflict_body,
            )
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(
                conflict.json()["error"]["code"],
                "idempotency_key_conflict",
            )

            reopened = client.patch(
                "/api/v1/delivery/items/STORY-1",
                json={
                    "expected_delivery_version": 2,
                    "status": "IN_PROGRESS",
                },
            )
            self.assertEqual(reopened.status_code, 200, reopened.text)
            self.assertEqual(reopened.json()["plan"]["delivery_version"], 3)

            reconfirm = client.post(
                "/api/v1/delivery/items/STORY-1/complete-with-verification",
                headers={"Idempotency-Key": "363c-close-2"},
                json=self._tests_defined_body(
                    expected_delivery_version=3,
                    expected_verification_version=1,
                    existing_requirement_ids=[requirement_id],
                ),
            )
            self.assertEqual(reconfirm.status_code, 200, reconfirm.text)
            self.assertEqual(reconfirm.json()["delivery_version"], 4)
            self.assertEqual(reconfirm.json()["verification_version"], 2)
            self.assertEqual(reconfirm.json()["requirement_ids"], [requirement_id])

        with self.factory() as session:
            plan = session.get(DeliveryPlanRow, "DP-1")
            story = session.get(DeliveryItemRow, "STORY-1")
            scope = session.scalar(select(VerificationScopeRow))
            assert plan is not None and story is not None and scope is not None
            self.assertEqual(plan.delivery_version, 4)
            self.assertEqual(story.status, "DONE")
            self.assertEqual(scope.verification_version, 2)
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(VerificationRequirementRow)
                ),
                1,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(
                        VerificationRequirementRevisionRow
                    )
                ),
                1,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(StoryVerificationDecisionRow)
                ),
                2,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(CommandIdempotencyReceipt)
                ),
                2,
            )
            delivery_actions = session.scalars(
                select(DeliveryChangeHistory.action).order_by(
                    DeliveryChangeHistory.delivery_version
                )
            ).all()
            self.assertEqual(
                delivery_actions,
                [
                    "STORY_COMPLETED_WITH_VERIFICATION",
                    "ITEM_UPDATED",
                    "STORY_COMPLETED_WITH_VERIFICATION",
                ],
            )
            verification_actions = session.scalars(
                select(VerificationChangeHistory.action).order_by(
                    VerificationChangeHistory.occurred_at,
                    VerificationChangeHistory.id,
                )
            ).all()
            self.assertIn("VERIFICATION_SCOPE_CREATED", verification_actions)
            self.assertIn(
                "REQUIREMENT_DEFINED_AT_STORY_CLOSURE",
                verification_actions,
            )
            self.assertEqual(
                verification_actions.count("STORY_DECISION_RECORDED"),
                2,
            )

    def test_verification_cas_conflict_rolls_back_delivery_and_story(self) -> None:
        with self.factory.begin() as session:
            SqlVerificationRepository(session).add_scope(
                VerificationScope(
                    id="VS-1",
                    work_package_id="WP-1",
                    lead_user_id="LEAD",
                    verification_version=2,
                )
            )

        app = self._app(principal("TECH"))
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/delivery/items/STORY-1/complete-with-verification",
                headers={"Idempotency-Key": "363c-stale-verification"},
                json=self._tests_defined_body(
                    expected_delivery_version=1,
                    expected_verification_version=1,
                ),
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(
                response.json()["error"]["code"],
                "verification_version_conflict",
            )

        with self.factory() as session:
            plan = session.get(DeliveryPlanRow, "DP-1")
            story = session.get(DeliveryItemRow, "STORY-1")
            scope = session.get(VerificationScopeRow, "VS-1")
            assert plan is not None and story is not None and scope is not None
            self.assertEqual(plan.delivery_version, 1)
            self.assertEqual(story.status, "IN_PROGRESS")
            self.assertEqual(scope.verification_version, 2)
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(StoryVerificationDecisionRow)
                ),
                0,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(VerificationChangeHistory)
                ),
                0,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(CommandIdempotencyReceipt)
                ),
                0,
            )

    def test_historical_done_story_records_decision_without_mutating_delivery(self) -> None:
        with self.factory.begin() as session:
            plan = session.get(DeliveryPlanRow, "DP-1")
            story = session.get(DeliveryItemRow, "STORY-1")
            assert plan is not None and story is not None
            plan.status = "ARCHIVED"
            plan.delivery_version = 4
            story.status = "DONE"

        app = self._app(principal("LEAD"))
        body = {
            "expected_delivery_version": 4,
            "expected_verification_version": None,
            "decision": {
                "kind": "NO_TEST_REQUIRED",
                "justification": "Décision historique documentée explicitement.",
            },
        }
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/delivery/items/STORY-1/historical-verification-decision",
                headers={"Idempotency-Key": "363c-history-1"},
                json=body,
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertTrue(payload["historical"])
            self.assertEqual(payload["delivery_version"], 4)
            self.assertEqual(payload["verification_version"], 1)
            self.assertEqual(payload["story_status"], "DONE")
            self.assertEqual(payload["requirement_ids"], [])

            duplicate = client.post(
                "/api/v1/delivery/items/STORY-1/historical-verification-decision",
                headers={"Idempotency-Key": "363c-history-2"},
                json={
                    **body,
                    "expected_verification_version": 1,
                },
            )
            self.assertEqual(duplicate.status_code, 409, duplicate.text)
            self.assertEqual(
                duplicate.json()["error"]["code"],
                "story_verification_already_documented",
            )

        with self.factory() as session:
            plan = session.get(DeliveryPlanRow, "DP-1")
            story = session.get(DeliveryItemRow, "STORY-1")
            scope = session.scalar(select(VerificationScopeRow))
            decision = session.scalar(select(StoryVerificationDecisionRow))
            assert (
                plan is not None
                and story is not None
                and scope is not None
                and decision is not None
            )
            self.assertEqual(plan.status, "ARCHIVED")
            self.assertEqual(plan.delivery_version, 4)
            self.assertEqual(story.status, "DONE")
            self.assertEqual(scope.verification_version, 1)
            self.assertEqual(decision.kind, "NO_TEST_REQUIRED")
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(DeliveryChangeHistory)
                ),
                0,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(VerificationChangeHistory)
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
