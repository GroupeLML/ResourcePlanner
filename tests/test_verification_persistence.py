from __future__ import annotations

import json
import unittest

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.application.errors import ApplicationConflictError
from app.domain.delivery import DeliveryItem, DeliveryItemType, DeliveryPlan
from app.domain.verification import (
    StoryVerificationDecision,
    VerificationDecisionKind,
    VerificationPhase,
    VerificationRequirement,
    VerificationRequirementRevision,
    VerificationRetestRequest,
    VerificationScope,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    CommandIdempotencyReceipt,
    Project,
    SqlDeliveryRepository,
    SqlVerificationRepository,
    VerificationChangeHistory,
    VerificationRequirementRow,
    VerificationScopeRow,
    WorkPackage,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.verification_repository import VerificationVersionConflict


class VerificationPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add(Project(id="P-1", number="P-1", name="Projet"))
            session.add(WorkPackage(id="WP-1", project_id="P-1", name="WP 1"))
            session.add(WorkPackage(id="WP-2", project_id="P-1", name="WP 2"))
            session.add_all(
                [
                    AppUser(
                        id="USER-LEAD",
                        issuer="test",
                        subject="lead",
                        display_name="Lead Verification",
                        roles_json='["TECHNICIAN"]',
                    ),
                    AppUser(
                        id="USER-ACTOR",
                        issuer="test",
                        subject="actor",
                        display_name="Acteur Verification",
                        roles_json='["TECHNICIAN"]',
                    ),
                ]
            )
            delivery = SqlDeliveryRepository(session)
            delivery.add_plan(DeliveryPlan(id="DP-1", work_package_id="WP-1"))
            delivery.add_item(
                DeliveryItem(
                    id="STORY-1",
                    delivery_plan_id="DP-1",
                    item_type=DeliveryItemType.STORY,
                    title="Story WP1",
                )
            )
            delivery.add_plan(DeliveryPlan(id="DP-2", work_package_id="WP-2"))
            delivery.add_item(
                DeliveryItem(
                    id="STORY-2",
                    delivery_plan_id="DP-2",
                    item_type=DeliveryItemType.STORY,
                    title="Story WP2",
                )
            )
            delivery.add_item(
                DeliveryItem(
                    id="EPIC-1",
                    delivery_plan_id="DP-1",
                    item_type=DeliveryItemType.EPIC,
                    title="Epic WP1",
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _scope() -> VerificationScope:
        return VerificationScope(
            id="VS-1",
            work_package_id="WP-1",
            lead_user_id="USER-LEAD",
        )

    @staticmethod
    def _requirement(
        *,
        identifier: str = "REQ-1",
        revision_id: str = "REV-1",
        story_id: str = "STORY-1",
    ) -> VerificationRequirement:
        return VerificationRequirement(
            id=identifier,
            verification_scope_id="VS-1",
            story_id=story_id,
            phase=VerificationPhase.FAT,
            current_revision_id=revision_id,
        )

    @staticmethod
    def _revision(
        *,
        identifier: str = "REV-1",
        requirement_id: str = "REQ-1",
        number: int = 1,
        method: str = "Actionner la pompe",
    ) -> VerificationRequirementRevision:
        return VerificationRequirementRevision(
            id=identifier,
            requirement_id=requirement_id,
            revision_number=number,
            objective="Valider le démarrage",
            method=method,
            expected_result="La pompe démarre sans alarme",
            prerequisites=("Alimentation disponible",),
            criticality="HIGH",
        )

    def _create_scope(self) -> None:
        with self.factory.begin() as session:
            SqlVerificationRepository(session).add_scope(self._scope())

    def test_scope_is_unique_per_work_package_and_has_independent_cas(self) -> None:
        self._create_scope()
        with self.factory.begin() as session:
            repository = SqlVerificationRepository(session)
            self.assertEqual(
                repository.compare_and_increment_version(
                    "VS-1", expected_verification_version=1
                ),
                2,
            )
            with self.assertRaises(VerificationVersionConflict) as raised:
                repository.compare_and_increment_version(
                    "VS-1", expected_verification_version=1
                )
            self.assertEqual(raised.exception.code, "verification_version_conflict")

        with self.factory() as session:
            scope = SqlVerificationRepository(session).get_scope_for_work_package("WP-1")
            self.assertIsNotNone(scope)
            assert scope is not None
            self.assertEqual(scope.verification_version, 2)

        with self.factory() as session:
            repository = SqlVerificationRepository(session)
            with self.assertRaises(IntegrityError):
                repository.add_scope(
                    VerificationScope(id="VS-2", work_package_id="WP-1")
                )
            session.rollback()

    def test_requirement_revision_source_and_withdrawal_are_persisted(self) -> None:
        self._create_scope()
        with self.factory.begin() as session:
            repository = SqlVerificationRepository(session)
            repository.add_requirement(self._requirement(), self._revision())
            revised = repository.add_revision(
                "REQ-1",
                self._revision(
                    identifier="REV-2",
                    number=2,
                    method="Actionner puis couper la pompe",
                ),
            )
            self.assertEqual(revised.current_revision_id, "REV-2")
            withdrawn = repository.withdraw_requirement(
                "REQ-1", reason="Essai devenu hors périmètre"
            )
            self.assertEqual(withdrawn.id, "REQ-1")
            self.assertEqual(withdrawn.current_revision_id, "REV-2")

        with self.factory() as session:
            repository = SqlVerificationRepository(session)
            requirement = repository.get_requirement("REQ-1")
            self.assertIsNotNone(requirement)
            assert requirement is not None
            self.assertEqual(requirement.current_revision_id, "REV-2")
            self.assertEqual(requirement.withdrawal_reason, "Essai devenu hors périmètre")
            revisions = repository.list_revisions("REQ-1")
            self.assertEqual([revision.id for revision in revisions], ["REV-1", "REV-2"])
            self.assertEqual(revisions[0].method, "Actionner la pompe")
            self.assertEqual(revisions[0].prerequisites, ("Alimentation disponible",))

    def test_requirement_source_must_be_story_from_same_work_package(self) -> None:
        self._create_scope()
        with self.factory() as session:
            repository = SqlVerificationRepository(session)
            with self.assertRaisesRegex(ValueError, "same WorkPackage"):
                repository.add_requirement(
                    self._requirement(story_id="STORY-2"),
                    self._revision(),
                )
            session.rollback()

        with self.factory() as session:
            repository = SqlVerificationRepository(session)
            with self.assertRaisesRegex(ValueError, "Delivery STORY"):
                repository.add_requirement(
                    self._requirement(story_id="EPIC-1"),
                    self._revision(),
                )
            session.rollback()

    def test_story_decisions_and_retest_intent_preserve_history(self) -> None:
        self._create_scope()
        with self.factory.begin() as session:
            repository = SqlVerificationRepository(session)
            repository.add_requirement(self._requirement(), self._revision())
            repository.add_story_decision(
                StoryVerificationDecision(
                    id="DEC-1",
                    verification_scope_id="VS-1",
                    story_id="STORY-1",
                    kind=VerificationDecisionKind.TESTS_DEFINED,
                    requirement_ids=("REQ-1",),
                )
            )
            repository.add_story_decision(
                StoryVerificationDecision(
                    id="DEC-2",
                    verification_scope_id="VS-1",
                    story_id="STORY-1",
                    kind=VerificationDecisionKind.NO_TEST_REQUIRED,
                    justification="Reconfirmation historique après réouverture",
                )
            )
            repository.add_retest_request(
                VerificationRetestRequest(
                    id="RETEST-1",
                    requirement_id="REQ-1",
                    revision_id="REV-1",
                    after_execution_sequence=4,
                    reason="Rejouer après correction terrain",
                )
            )

        with self.factory() as session:
            repository = SqlVerificationRepository(session)
            first = repository.get_story_decision("DEC-1")
            second = repository.get_story_decision("DEC-2")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertEqual(first.requirement_ids, ("REQ-1",))
            self.assertEqual(second.requirement_ids, ())
            self.assertEqual(second.kind, VerificationDecisionKind.NO_TEST_REQUIRED)
            retest = repository.get_retest_request("RETEST-1")
            self.assertIsNotNone(retest)
            assert retest is not None
            self.assertEqual(retest.after_execution_sequence, 4)

    def test_keyed_mutation_replays_cas_business_rows_audit_and_receipt_atomically(self) -> None:
        self._create_scope()
        payload = {"story_id": "STORY-1", "phase": "FAT"}

        with self.factory.begin() as session:
            repository = SqlVerificationRepository(session)

            def action() -> dict[str, object]:
                version = repository.compare_and_increment_version(
                    "VS-1", expected_verification_version=1
                )
                repository.add_requirement(self._requirement(), self._revision())
                audit_id = repository.append_history(
                    scope_id="VS-1",
                    entity_type="REQUIREMENT",
                    entity_id="REQ-1",
                    story_id="STORY-1",
                    actor_user_id="USER-ACTOR",
                    action="REQUIREMENT_DEFINED",
                    verification_version=version,
                    details={"phase": "FAT"},
                )
                return {
                    "requirement_id": "REQ-1",
                    "verification_version": version,
                    "audit_id": audit_id,
                }

            first = repository.replay_or_execute_mutation(
                scope_id="VS-1",
                actor_user_id="USER-ACTOR",
                command_scope="define_requirement",
                idempotency_key="verification-key-1",
                request_payload=payload,
                action=action,
            )

        with self.factory.begin() as session:
            repository = SqlVerificationRepository(session)
            second = repository.replay_or_execute_mutation(
                scope_id="VS-1",
                actor_user_id="USER-ACTOR",
                command_scope="define_requirement",
                idempotency_key="verification-key-1",
                request_payload=payload,
                action=lambda: self.fail("idempotent replay must not re-execute"),
            )
            self.assertEqual(second, first)
            with self.assertRaises(ApplicationConflictError) as raised:
                repository.replay_or_execute_mutation(
                    scope_id="VS-1",
                    actor_user_id="USER-ACTOR",
                    command_scope="define_requirement",
                    idempotency_key="verification-key-1",
                    request_payload={"story_id": "STORY-1", "phase": "SAT"},
                    action=lambda: {},
                )
            self.assertEqual(raised.exception.code, "idempotency_key_conflict")

        with self.factory() as session:
            scope = session.get(VerificationScopeRow, "VS-1")
            assert scope is not None
            self.assertEqual(scope.verification_version, 2)
            self.assertEqual(
                session.scalar(select(func.count()).select_from(VerificationRequirementRow)),
                1,
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(VerificationChangeHistory)),
                1,
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(CommandIdempotencyReceipt)),
                1,
            )
            audit = session.scalar(select(VerificationChangeHistory))
            assert audit is not None
            self.assertEqual(json.loads(audit.details_json), {"phase": "FAT"})

    def test_unkeyed_failed_mutation_rolls_back_cas_inside_outer_transaction(self) -> None:
        self._create_scope()
        with self.factory.begin() as session:
            repository = SqlVerificationRepository(session)

            def fail_after_cas() -> dict[str, object]:
                repository.compare_and_increment_version(
                    "VS-1", expected_verification_version=1
                )
                raise RuntimeError("boom")

            with self.assertRaisesRegex(RuntimeError, "boom"):
                repository.replay_or_execute_mutation(
                    scope_id="VS-1",
                    actor_user_id="USER-ACTOR",
                    command_scope="failing_mutation",
                    idempotency_key=None,
                    request_payload={},
                    action=fail_after_cas,
                )

        with self.factory() as session:
            scope = SqlVerificationRepository(session).get_scope("VS-1")
            assert scope is not None
            self.assertEqual(scope.verification_version, 1)


if __name__ == "__main__":
    unittest.main()
