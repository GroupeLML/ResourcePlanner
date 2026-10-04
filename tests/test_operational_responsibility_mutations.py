from __future__ import annotations

from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from app.application.errors import (
    ApplicationConflictError,
    ApplicationValidationError,
)
from app.infrastructure.sql import (
    Base,
    BusinessContact,
    CommandIdempotencyReceipt,
    PlanningChangeHistory,
    PlanningMutationState,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    SqlAllocationCommandAdapter,
    SqlOperationalResponsibilityMutationRepository,
    create_session_factory,
    create_sql_engine,
)


DAY = date(2026, 10, 6)


class OperationalResponsibilityMutationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        path = Path(self._directory.name) / "operational-responsibility-594c.db"
        self.engine = create_sql_engine(f"sqlite+pysqlite:///{path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add_all(
                [
                    BusinessContact(
                        id="C-ACTIVE",
                        display_name="Responsable actif",
                        active=True,
                    ),
                    BusinessContact(
                        id="C-INACTIVE",
                        display_name="Responsable inactif",
                        active=False,
                    ),
                    Project(
                        id="P1",
                        number="P-594C",
                        name="Projet 594C",
                    ),
                    Resource(
                        id="R1",
                        name="Alice",
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add(
                ResourceAvailabilityRule(
                    id="STD-R1",
                    resource_id="R1",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                )
            )
            session.add(
                ResourceRequirement(
                    id="REQ1",
                    legacy_segment_id="SEG-594C",
                    project_id="P1",
                    workforce_request_id=None,
                    assigned_resource_id="R1",
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=8,
                    status="Planifié",
                    planning_type="Flexible",
                    confirmation="Confirmée",
                    origin="AD_HOC",
                )
            )
            session.flush()
            session.add(
                Shift(
                    id="SHIFT1",
                    legacy_allocation_id="ALLOC-594C",
                    resource_requirement_id="REQ1",
                    resource_id="R1",
                    work_date=DAY,
                    hours=8,
                    allocation_type="Flexible",
                    source="AUTO",
                    locked=False,
                    outside_standard_hours=False,
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self._directory.cleanup()

    def _planning_version(self) -> int:
        with self.factory() as session:
            value = session.scalar(
                select(PlanningMutationState.version).where(
                    PlanningMutationState.id == "GLOBAL"
                )
            )
            return int(value or 1)

    def _count(self, model) -> int:
        with self.factory() as session:
            return int(session.scalar(select(func.count()).select_from(model)) or 0)

    def test_project_override_uses_dedicated_cas_audit_and_idempotent_replay(self) -> None:
        with self.factory.begin() as session:
            first = SqlOperationalResponsibilityMutationRepository(
                session
            ).set_project_override(
                "P1",
                "C-ACTIVE",
                actor_user_id="U-ACTOR",
                expected_version=1,
                idempotency_key="project-override-1",
            )
        self.assertEqual(first.project_override_version, 2)
        self.assertEqual(first.override_contact_id, "C-ACTIVE")

        with self.factory.begin() as session:
            replay = SqlOperationalResponsibilityMutationRepository(
                session
            ).set_project_override(
                "P1",
                "C-ACTIVE",
                actor_user_id="U-ACTOR",
                expected_version=1,
                idempotency_key="project-override-1",
            )
        self.assertEqual(replay, first)

        with self.factory() as session:
            project = session.get(Project, "P1")
            assert project is not None
            self.assertEqual(project.operational_responsible_override_contact_id, "C-ACTIVE")
            self.assertEqual(project.operational_responsible_override_version, 2)
            events = session.scalars(
                select(PlanningChangeHistory).where(
                    PlanningChangeHistory.entity_type == "PROJECT",
                    PlanningChangeHistory.entity_id == "P1",
                    PlanningChangeHistory.action
                    == "Modification responsable opérationnel projet",
                )
            ).all()
            self.assertEqual(len(events), 1)
        self.assertEqual(self._count(CommandIdempotencyReceipt), 1)

        with self.assertRaises(ApplicationConflictError) as raised:
            with self.factory.begin() as session:
                SqlOperationalResponsibilityMutationRepository(
                    session
                ).set_project_override(
                    "P1",
                    None,
                    actor_user_id="U-ACTOR",
                    expected_version=1,
                    idempotency_key="project-override-stale",
                )
        self.assertEqual(
            raised.exception.code,
            "project_operational_responsibility_version_conflict",
        )

    def test_invalid_project_contact_rolls_back_local_version(self) -> None:
        with self.assertRaises(ApplicationValidationError) as raised:
            with self.factory.begin() as session:
                SqlOperationalResponsibilityMutationRepository(
                    session
                ).set_project_override(
                    "P1",
                    "C-INACTIVE",
                    actor_user_id="U-ACTOR",
                    expected_version=1,
                    idempotency_key="project-invalid-contact",
                )
        self.assertEqual(
            raised.exception.code,
            "operational_responsibility_contact_inactive",
        )

        with self.factory() as session:
            project = session.get(Project, "P1")
            assert project is not None
            self.assertIsNone(project.operational_responsible_override_contact_id)
            self.assertEqual(project.operational_responsible_override_version, 1)
        self.assertEqual(self._count(CommandIdempotencyReceipt), 0)

    def test_requirement_override_uses_planning_cas_and_replays_before_stale_check(self) -> None:
        with self.factory.begin() as session:
            first = SqlOperationalResponsibilityMutationRepository(
                session
            ).set_requirement_override(
                "REQ1",
                "C-ACTIVE",
                actor_user_id="U-ACTOR",
                expected_planning_version=1,
                idempotency_key="requirement-override-1",
            )
        self.assertEqual(first.planning_version, 2)
        self.assertEqual(self._planning_version(), 2)

        with self.factory.begin() as session:
            replay = SqlOperationalResponsibilityMutationRepository(
                session
            ).set_requirement_override(
                "REQ1",
                "C-ACTIVE",
                actor_user_id="U-ACTOR",
                expected_planning_version=1,
                idempotency_key="requirement-override-1",
            )
        self.assertEqual(replay, first)
        self.assertEqual(self._planning_version(), 2)

        with self.factory() as session:
            requirement = session.get(ResourceRequirement, "REQ1")
            assert requirement is not None
            self.assertEqual(
                requirement.operational_responsible_override_contact_id,
                "C-ACTIVE",
            )
            events = session.scalars(
                select(PlanningChangeHistory).where(
                    PlanningChangeHistory.entity_type == "SEGMENT",
                    PlanningChangeHistory.entity_id == "REQ1",
                    PlanningChangeHistory.action
                    == "Modification responsable opérationnel segment",
                )
            ).all()
            self.assertEqual(len(events), 1)

        with self.assertRaises(ApplicationConflictError) as raised:
            with self.factory.begin() as session:
                SqlOperationalResponsibilityMutationRepository(
                    session
                ).set_requirement_override(
                    "REQ1",
                    None,
                    actor_user_id="U-ACTOR",
                    expected_planning_version=1,
                    idempotency_key="requirement-stale",
                )
        self.assertEqual(raised.exception.code, "planning_version_conflict")
        self.assertEqual(self._planning_version(), 2)

    def test_shift_override_stabilizes_auto_and_clear_does_not_release_manual(self) -> None:
        with self.factory.begin() as session:
            first = SqlOperationalResponsibilityMutationRepository(
                session
            ).set_shift_override(
                "SHIFT1",
                "C-ACTIVE",
                actor_user_id="U-ACTOR",
                expected_planning_version=1,
                idempotency_key="shift-override-1",
            )
        self.assertTrue(first.auto_source_converted)
        self.assertEqual(first.planning_version, 2)

        with self.factory() as session:
            shift = session.get(Shift, "SHIFT1")
            assert shift is not None
            self.assertEqual(shift.source, "MANUAL")
            self.assertTrue(shift.locked)
            self.assertEqual(
                shift.operational_responsible_override_contact_id,
                "C-ACTIVE",
            )

        with self.assertRaises(ApplicationConflictError) as raised:
            with self.factory.begin() as session:
                SqlAllocationCommandAdapter(session).release_manual("ALLOC-594C")
        self.assertEqual(
            raised.exception.code,
            "shift_operational_responsibility_override_must_be_cleared",
        )
        self.assertEqual(self._planning_version(), 2)

        with self.factory.begin() as session:
            cleared = SqlOperationalResponsibilityMutationRepository(
                session
            ).set_shift_override(
                "SHIFT1",
                None,
                actor_user_id="U-ACTOR",
                expected_planning_version=2,
                idempotency_key="shift-override-clear",
            )
        self.assertFalse(cleared.auto_source_converted)
        self.assertEqual(cleared.planning_version, 3)

        with self.factory() as session:
            shift = session.get(Shift, "SHIFT1")
            assert shift is not None
            self.assertIsNone(shift.operational_responsible_override_contact_id)
            self.assertEqual(shift.source, "MANUAL")
            self.assertTrue(shift.locked)

        with self.factory.begin() as session:
            SqlAllocationCommandAdapter(session).release_manual("ALLOC-594C")

        with self.factory() as session:
            shifts = session.scalars(
                select(Shift).where(Shift.resource_requirement_id == "REQ1")
            ).all()
            self.assertEqual(len(shifts), 1)
            self.assertEqual(shifts[0].source, "AUTO")
            self.assertFalse(shifts[0].locked)
            self.assertIsNone(
                shifts[0].operational_responsible_override_contact_id
            )

    def test_failure_after_auto_stabilization_rolls_back_version_shift_audit_and_receipt(
        self,
    ) -> None:
        with patch(
            "app.infrastructure.sql.operational_responsibility_mutation_repository."
            "SqlPlanningAuditJournal.append",
            side_effect=RuntimeError("audit failure after rebuild"),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit failure"):
                with self.factory.begin() as session:
                    SqlOperationalResponsibilityMutationRepository(
                        session
                    ).set_shift_override(
                        "SHIFT1",
                        "C-ACTIVE",
                        actor_user_id="U-ACTOR",
                        expected_planning_version=1,
                        idempotency_key="shift-rollback",
                    )

        self.assertEqual(self._planning_version(), 1)
        self.assertEqual(self._count(CommandIdempotencyReceipt), 0)
        with self.factory() as session:
            shift = session.get(Shift, "SHIFT1")
            assert shift is not None
            self.assertEqual(shift.source, "AUTO")
            self.assertFalse(shift.locked)
            self.assertIsNone(
                shift.operational_responsible_override_contact_id
            )
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count())
                        .select_from(PlanningChangeHistory)
                        .where(PlanningChangeHistory.entity_id == "SHIFT1")
                    )
                    or 0
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
