from __future__ import annotations

from datetime import date, time
from decimal import Decimal
import unittest

from sqlalchemy import select

from app.infrastructure.sql import (
    Base,
    Project,
    RequestLine,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    SqlAllocationCommandAdapter,
    SqlPlanningCommandAdapter,
    WorkforceRequest,
    WorkforceRequestPeriod,
    WorkforceRequestPeriodRequirement,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.same_resource_periods import SqlSameResourcePeriodCoordinator


D1 = date(2026, 10, 12)
D2 = date(2026, 10, 13)


class SameResourcePeriodPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-655B", name="Projet 655B"))
            session.add_all(
                [
                    Resource(id="R1", name="Alice", active=True, sort_order=1),
                    Resource(id="R2", name="Bob", active=True, sort_order=2),
                ]
            )
            session.flush()
            for resource_id in ("R1", "R2"):
                session.add(
                    ResourceAvailabilityRule(
                        id=f"SCH-{resource_id}",
                        resource_id=resource_id,
                        availability_type="Horaire standard",
                        weekdays="Lun,Mar,Mer,Jeu,Ven",
                        start_time=time(8, 0),
                        end_time=time(16, 0),
                        active=True,
                    )
                )

    def tearDown(self) -> None:
        self.engine.dispose()

    def _same_as_plan(
        self,
        session,
        *,
        root_target: str | None = "R1",
        child_target: str | None = None,
    ) -> tuple[ResourceRequirement, ResourceRequirement]:
        request = WorkforceRequest(
            id="D-SAME",
            legacy_demand_number="DEM-SAME",
            project_id="P1",
            line_mode=True,
            status="En planification",
        )
        line = RequestLine(
            id="L-SAME",
            workforce_request_id=request.id,
            position=0,
            kind="WORKFORCE",
            slot_count=1,
            desired_start=D1,
            desired_end=D2,
            estimated_hours=Decimal("16"),
            confirmation="Confirmée",
            active=True,
        )
        root_period = WorkforceRequestPeriod(
            id="PER-A",
            period_key="A",
            workforce_request_id=request.id,
            request_line_id=line.id,
            sequence=1,
            kind="CUMULATIVE",
            start_date=D1,
            end_date=D1,
            hours=Decimal("8"),
            inheritance_contract_version=1,
            confirmation_mode="EXPLICIT",
            confirmation="Confirmée",
            proposed_resource_mode="EXPLICIT",
            proposed_resource_id=root_target,
            resource_count=1,
            active=True,
        )
        child_period = WorkforceRequestPeriod(
            id="PER-B",
            period_key="B",
            workforce_request_id=request.id,
            request_line_id=line.id,
            sequence=2,
            kind="CUMULATIVE",
            start_date=D2,
            end_date=D2,
            hours=Decimal("8"),
            inheritance_contract_version=1,
            confirmation_mode="EXPLICIT",
            confirmation="Confirmée",
            proposed_resource_mode="SAME_AS_PERIOD",
            same_as_period_key="A",
            resource_count=1,
            active=True,
        )
        root = ResourceRequirement(
            id="REQ-A",
            legacy_segment_id="SEG-A",
            project_id="P1",
            workforce_request_id=request.id,
            source_request_line_id=line.id,
            assigned_resource_id=root_target,
            start_date=D1,
            end_date=D1,
            planned_hours=Decimal("8"),
            status="Planifié" if root_target else "À assigner",
            origin="REQUEST",
        )
        child = ResourceRequirement(
            id="REQ-B",
            legacy_segment_id="SEG-B",
            project_id="P1",
            workforce_request_id=request.id,
            source_request_line_id=line.id,
            assigned_resource_id=child_target,
            start_date=D2,
            end_date=D2,
            planned_hours=Decimal("8"),
            status="Planifié" if child_target else "À assigner",
            origin="REQUEST",
        )
        session.add(request)
        session.flush()
        session.add(line)
        session.flush()
        session.add_all([root_period, child_period])
        session.flush()
        session.add_all([root, child])
        session.flush()
        session.add_all(
            [
                WorkforceRequestPeriodRequirement(
                    resource_requirement_id=root.id,
                    period_id=root_period.id,
                ),
                WorkforceRequestPeriodRequirement(
                    resource_requirement_id=child.id,
                    period_id=child_period.id,
                ),
            ]
        )
        session.flush()
        return root, child

    def test_rebuild_propagates_root_target_and_real_shifts_use_same_resource(self) -> None:
        with transactional_session(self.factory) as session:
            root, child = self._same_as_plan(session)

            summary = SqlPlanningCommandAdapter(session).rebuild()

            self.assertEqual(root.assigned_resource_id, "R1")
            self.assertEqual(child.assigned_resource_id, "R1")
            shifts = session.scalars(
                select(Shift).where(
                    Shift.resource_requirement_id.in_((root.id, child.id))
                )
            ).all()
            self.assertTrue(shifts)
            self.assertEqual({shift.resource_id for shift in shifts}, {"R1"})
            self.assertEqual(summary["same_resource_groups"], 1)
            self.assertEqual(summary["same_resource_unresolved_groups"], 0)

    def test_locked_shift_fixes_the_resource_for_the_whole_component(self) -> None:
        with transactional_session(self.factory) as session:
            root, child = self._same_as_plan(session, root_target="R1")
            session.add(
                Shift(
                    id="LOCK-B",
                    resource_requirement_id=child.id,
                    resource_id="R2",
                    work_date=D2,
                    hours=Decimal("4"),
                    allocation_type="Flexible",
                    source="MANUAL",
                    locked=True,
                )
            )
            session.flush()

            SqlPlanningCommandAdapter(session).rebuild()

            self.assertEqual(root.assigned_resource_id, "R2")
            self.assertEqual(child.assigned_resource_id, "R2")
            shifts = session.scalars(
                select(Shift).where(
                    Shift.resource_requirement_id.in_((root.id, child.id))
                )
            ).all()
            self.assertEqual({shift.resource_id for shift in shifts}, {"R2"})

    def test_contradictory_locked_resources_are_rejected(self) -> None:
        with transactional_session(self.factory) as session:
            root, child = self._same_as_plan(session, root_target=None)
            session.add_all(
                [
                    Shift(
                        id="LOCK-A",
                        resource_requirement_id=root.id,
                        resource_id="R1",
                        work_date=D1,
                        hours=Decimal("4"),
                        allocation_type="Flexible",
                        source="MANUAL",
                        locked=True,
                    ),
                    Shift(
                        id="LOCK-B",
                        resource_requirement_id=child.id,
                        resource_id="R2",
                        work_date=D2,
                        hours=Decimal("4"),
                        allocation_type="Flexible",
                        source="MANUAL",
                        locked=True,
                    ),
                ]
            )
            session.flush()

            with self.assertRaisesRegex(ValueError, "ressources différentes"):
                SqlPlanningCommandAdapter(session).rebuild()

    def test_explicit_target_change_retargets_the_component_atomically(self) -> None:
        with transactional_session(self.factory) as session:
            root, child = self._same_as_plan(session, root_target="R1")

            SqlAllocationCommandAdapter(session).assign_segment(child.id, "R2")

            self.assertEqual(root.assigned_resource_id, "R2")
            self.assertEqual(child.assigned_resource_id, "R2")
            SqlSameResourcePeriodCoordinator(session).assert_shift_invariant()
            shifts = session.scalars(
                select(Shift).where(
                    Shift.resource_requirement_id.in_((root.id, child.id))
                )
            ).all()
            self.assertEqual({shift.resource_id for shift in shifts}, {"R2"})


if __name__ == "__main__":
    unittest.main()
