from __future__ import annotations

from datetime import date, time
from decimal import Decimal
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

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
from app.infrastructure.sql.demand_period_repository import SqlDemandPeriodRepository
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

    def test_candidate_period_edits_do_not_rewrite_the_active_same_as_graph(self) -> None:
        with transactional_session(self.factory) as session:
            root, child = self._same_as_plan(session, root_target="R1")
            approved_periods = session.scalars(
                select(WorkforceRequestPeriod).where(
                    WorkforceRequestPeriod.id.in_(("PER-A", "PER-B"))
                )
            ).all()
            for period in approved_periods:
                period.active = False
            session.add_all(
                [
                    WorkforceRequestPeriod(
                        id="PER-A-CAND",
                        period_key="A",
                        workforce_request_id="D-SAME",
                        request_line_id="L-SAME",
                        sequence=1,
                        kind="CUMULATIVE",
                        start_date=D1,
                        end_date=D1,
                        hours=Decimal("8"),
                        inheritance_contract_version=1,
                        confirmation_mode="EXPLICIT",
                        confirmation="Confirmée",
                        proposed_resource_mode="EXPLICIT",
                        resource_count=1,
                        active=True,
                    ),
                    WorkforceRequestPeriod(
                        id="PER-B-CAND",
                        period_key="B",
                        workforce_request_id="D-SAME",
                        request_line_id="L-SAME",
                        sequence=2,
                        kind="CUMULATIVE",
                        start_date=D2,
                        end_date=D2,
                        hours=Decimal("8"),
                        inheritance_contract_version=1,
                        confirmation_mode="EXPLICIT",
                        confirmation="Confirmée",
                        proposed_resource_mode="EXPLICIT",
                        resource_count=1,
                        active=True,
                    ),
                ]
            )
            session.flush()

            summary = SqlPlanningCommandAdapter(session).rebuild()

            self.assertEqual(summary["same_resource_groups"], 1)
            self.assertEqual(root.assigned_resource_id, "R1")
            self.assertEqual(child.assigned_resource_id, "R1")

    def test_unassigned_component_remains_unresolved_without_inventing_a_resource(self) -> None:
        with transactional_session(self.factory) as session:
            root, child = self._same_as_plan(session, root_target=None)

            summary = SqlPlanningCommandAdapter(session).rebuild()

            self.assertIsNone(root.assigned_resource_id)
            self.assertIsNone(child.assigned_resource_id)
            self.assertEqual(summary["same_resource_groups"], 1)
            self.assertEqual(summary["same_resource_unresolved_groups"], 1)
            shifts = session.scalars(
                select(Shift).where(
                    Shift.resource_requirement_id.in_((root.id, child.id))
                )
            ).all()
            self.assertEqual(shifts, [])

    def test_overlapping_periods_share_capacity_on_the_common_resource(self) -> None:
        with transactional_session(self.factory) as session:
            root, child = self._same_as_plan(session, root_target="R1")
            child.start_date = D1
            child.end_date = D1
            session.flush()

            summary = SqlPlanningCommandAdapter(session).rebuild()

            self.assertEqual(root.assigned_resource_id, "R1")
            self.assertEqual(child.assigned_resource_id, "R1")
            self.assertEqual(summary["requested_hours"], 16.0)
            self.assertEqual(summary["allocated_hours"], 8.0)
            self.assertEqual(summary["unallocated_hours"], 8.0)
            shifts = session.scalars(
                select(Shift).where(
                    Shift.resource_requirement_id.in_((root.id, child.id))
                )
            ).all()
            self.assertEqual({shift.resource_id for shift in shifts}, {"R1"})

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

    def test_locked_component_rejects_a_conflicting_explicit_retarget(self) -> None:
        with transactional_session(self.factory) as session:
            root, child = self._same_as_plan(session, root_target="R1")
            session.add(
                Shift(
                    id="LOCK-A",
                    resource_requirement_id=root.id,
                    resource_id="R1",
                    work_date=D1,
                    hours=Decimal("4"),
                    allocation_type="Flexible",
                    source="MANUAL",
                    locked=True,
                )
            )
            session.flush()

            with self.assertRaisesRegex(ValueError, "fixe déjà la ressource"):
                SqlAllocationCommandAdapter(session).assign_segment(child.id, "R2")

            self.assertEqual(root.assigned_resource_id, "R1")
            self.assertIsNone(child.assigned_resource_id)

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


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(
    IS_MSSQL,
    "Validation 655C réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class SameResourcePeriodSqlServerAcceptanceTests(unittest.TestCase):
    def test_authority_projection_and_real_shifts_keep_one_resource(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P655C-{marker}"
        request_id = f"D655C-{marker}"
        request_number = f"DMO-655C-{marker}"
        line_id = f"L655C-{marker}"
        root_period_id = f"PER-A-{marker}"
        child_period_id = f"PER-B-{marker}"
        root_requirement_id = f"REQ-A-{marker}"
        child_requirement_id = f"REQ-B-{marker}"
        r1 = f"R655CA-{marker}"
        r2 = f"R655CB-{marker}"
        schedule_ids = (f"SCH-A-{marker}", f"SCH-B-{marker}")

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add(Project(id=project_id, number=f"P-655C-{marker}", name="655C SQL Server"))
                session.add_all(
                    [
                        Resource(id=r1, name=f"Alice 655C {marker}", active=True, sort_order=1),
                        Resource(id=r2, name=f"Bob 655C {marker}", active=True, sort_order=2),
                    ]
                )
                session.flush()
                session.add_all(
                    [
                        ResourceAvailabilityRule(
                            id=schedule_ids[0],
                            resource_id=r1,
                            availability_type="Horaire standard",
                            weekdays="Lun,Mar,Mer,Jeu,Ven",
                            start_time=time(8, 0),
                            end_time=time(16, 0),
                            active=True,
                        ),
                        ResourceAvailabilityRule(
                            id=schedule_ids[1],
                            resource_id=r2,
                            availability_type="Horaire standard",
                            weekdays="Lun,Mar,Mer,Jeu,Ven",
                            start_time=time(8, 0),
                            end_time=time(16, 0),
                            active=True,
                        ),
                    ]
                )
                request = WorkforceRequest(
                    id=request_id,
                    legacy_demand_number=request_number,
                    project_id=project_id,
                    line_mode=True,
                    status="En planification",
                )
                session.add(request)
                session.flush()
                line = RequestLine(
                    id=line_id,
                    workforce_request_id=request_id,
                    position=0,
                    kind="WORKFORCE",
                    slot_count=1,
                    desired_start=D1,
                    desired_end=D2,
                    estimated_hours=Decimal("16"),
                    confirmation="Confirmée",
                    proposed_resource_id=r1,
                    active=True,
                )
                session.add(line)
                session.flush()
                root_period = WorkforceRequestPeriod(
                    id=root_period_id,
                    period_key="ROOT",
                    workforce_request_id=request_id,
                    request_line_id=line_id,
                    sequence=1,
                    kind="CUMULATIVE",
                    start_date=D1,
                    end_date=D1,
                    hours=Decimal("8"),
                    inheritance_contract_version=1,
                    confirmation_mode="INHERIT_MASTER",
                    confirmation="Confirmée",
                    proposed_resource_mode="INHERIT_MASTER",
                    proposed_resource_id=r1,
                    resource_count=1,
                    active=True,
                )
                child_period = WorkforceRequestPeriod(
                    id=child_period_id,
                    period_key="FOLLOW",
                    workforce_request_id=request_id,
                    request_line_id=line_id,
                    sequence=2,
                    kind="CUMULATIVE",
                    start_date=D2,
                    end_date=D2,
                    hours=Decimal("8"),
                    inheritance_contract_version=1,
                    confirmation_mode="EXPLICIT",
                    confirmation="Tentative",
                    proposed_resource_mode="SAME_AS_PERIOD",
                    same_as_period_key="ROOT",
                    resource_count=1,
                    active=True,
                )
                session.add_all([root_period, child_period])
                session.flush()
                root = ResourceRequirement(
                    id=root_requirement_id,
                    legacy_segment_id=f"SEG-A-{marker}",
                    project_id=project_id,
                    workforce_request_id=request_id,
                    source_request_line_id=line_id,
                    assigned_resource_id=r1,
                    start_date=D1,
                    end_date=D1,
                    planned_hours=Decimal("8"),
                    status="Planifié",
                    origin="REQUEST",
                    confirmation="Confirmée",
                )
                child = ResourceRequirement(
                    id=child_requirement_id,
                    legacy_segment_id=f"SEG-B-{marker}",
                    project_id=project_id,
                    workforce_request_id=request_id,
                    source_request_line_id=line_id,
                    start_date=D2,
                    end_date=D2,
                    planned_hours=Decimal("8"),
                    status="À assigner",
                    origin="REQUEST",
                    confirmation="Tentative",
                )
                session.add_all([root, child])
                session.flush()
                session.add_all(
                    [
                        WorkforceRequestPeriodRequirement(
                            resource_requirement_id=root_requirement_id,
                            period_id=root_period_id,
                        ),
                        WorkforceRequestPeriodRequirement(
                            resource_requirement_id=child_requirement_id,
                            period_id=child_period_id,
                        ),
                    ]
                )
                session.flush()

                periods = SqlDemandPeriodRepository(session).list_for_demand(
                    request_number,
                    request_line_id=line_id,
                )
                by_id = {row.period_id: row for row in periods}
                self.assertEqual(by_id["ROOT"].confirmation_mode, "INHERIT_MASTER")
                self.assertEqual(by_id["ROOT"].confirmation_provenance, "MASTER")
                self.assertEqual(by_id["ROOT"].proposed_resource_mode, "INHERIT_MASTER")
                self.assertEqual(by_id["ROOT"].proposed_resource_id, r1)
                self.assertEqual(by_id["ROOT"].resource_count_provenance, "MASTER")
                self.assertEqual(by_id["FOLLOW"].proposed_resource_mode, "SAME_AS_PERIOD")
                self.assertEqual(by_id["FOLLOW"].same_as_period_id, "ROOT")
                self.assertEqual(by_id["FOLLOW"].same_as_state, "ACTIVE")

                summary = SqlPlanningCommandAdapter(session).rebuild()
                self.assertEqual(summary["same_resource_groups"], 1)
                self.assertEqual(summary["same_resource_unresolved_groups"], 0)

            with factory() as session:
                requirements = {
                    row.id: row
                    for row in session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.id.in_(
                                (root_requirement_id, child_requirement_id)
                            )
                        )
                    ).all()
                }
                self.assertEqual(requirements[root_requirement_id].assigned_resource_id, r1)
                self.assertEqual(requirements[child_requirement_id].assigned_resource_id, r1)
                shifts = session.scalars(
                    select(Shift).where(
                        Shift.resource_requirement_id.in_(
                            (root_requirement_id, child_requirement_id)
                        )
                    )
                ).all()
                self.assertTrue(shifts)
                self.assertEqual({row.resource_id for row in shifts}, {r1})
        finally:
            with factory.begin() as session:
                session.execute(
                    delete(Shift).where(
                        Shift.resource_requirement_id.in_(
                            (root_requirement_id, child_requirement_id)
                        )
                    )
                )
                session.execute(
                    delete(WorkforceRequestPeriodRequirement).where(
                        WorkforceRequestPeriodRequirement.resource_requirement_id.in_(
                            (root_requirement_id, child_requirement_id)
                        )
                    )
                )
                session.execute(
                    delete(ResourceRequirement).where(
                        ResourceRequirement.id.in_(
                            (root_requirement_id, child_requirement_id)
                        )
                    )
                )
                session.execute(
                    delete(WorkforceRequestPeriod).where(
                        WorkforceRequestPeriod.id.in_(
                            (root_period_id, child_period_id)
                        )
                    )
                )
                session.execute(delete(RequestLine).where(RequestLine.id == line_id))
                session.execute(
                    delete(WorkforceRequest).where(WorkforceRequest.id == request_id)
                )
                session.execute(
                    delete(ResourceAvailabilityRule).where(
                        ResourceAvailabilityRule.id.in_(schedule_ids)
                    )
                )
                session.execute(delete(Resource).where(Resource.id.in_((r1, r2))))
                session.execute(delete(Project).where(Project.id == project_id))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
