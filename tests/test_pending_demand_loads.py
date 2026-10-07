from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from app.domain.demand_periods import (
    PERIOD_KIND_ALTERNATIVE,
    DemandPeriodDefinition,
    projected_hours_in_window,
)
from app.domain.workload import (
    LOAD_POTENTIAL,
    PENDING_LOAD_ADDITIVE,
    PENDING_LOAD_REPLACEMENT,
)
from app.infrastructure.sql import (
    Base,
    ORIGIN_REQUEST,
    Project,
    Resource,
    ResourceRequirement,
    Shift,
    SqlDemandPeriodRepository,
    SqlPlannerQueryRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


MONDAY = date(2026, 9, 14)
TUESDAY = date(2026, 9, 15)
FRIDAY = date(2026, 9, 18)


class PendingDemandLoadProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(
                Project(
                    id="P1",
                    number="P-1",
                    name="Projet test",
                    status="Actif",
                )
            )
            session.add_all(
                [
                    Resource(id="R1", name="Alice", active=True, sort_order=1),
                    Resource(id="R2", name="Bob", active=True, sort_order=2),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_new_submitted_request_is_additive_potential_load(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DMO-2026-1001",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Confirmée",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("40"),
                    resource_count=1,
                    required_competencies="SCADA",
                    proposed_resource_id="R1",
                )
            )

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            loads = queries.list_pending_loads(start=MONDAY, end=FRIDAY)
            snapshot = queries.planning_snapshot(start=MONDAY, end=FRIDAY)

        self.assertEqual(len(loads), 1)
        row = loads[0]
        self.assertEqual(row.mode, PENDING_LOAD_ADDITIVE)
        self.assertEqual(row.load_kind, LOAD_POTENTIAL)
        self.assertEqual(row.projected_hours, 40.0)
        self.assertEqual(row.window_hours, 40.0)
        self.assertEqual(row.required_competencies, "SCADA")
        self.assertEqual(row.proposed_resource, "Alice")
        self.assertEqual(len(row.candidate_windows), 1)
        candidate = row.candidate_windows[0]
        self.assertEqual(candidate.proposed_resource_id, "R1")
        self.assertEqual(candidate.proposed_resource, "Alice")
        self.assertEqual(candidate.window_hours, 40.0)
        self.assertEqual(snapshot.firm_hours, 0.0)
        self.assertEqual(snapshot.potential_hours, 40.0)
        self.assertEqual(snapshot.replacement_proposal_hours, 0.0)

    def test_pending_change_to_approved_plan_is_replacement_not_additive(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D2",
                    legacy_demand_number="DMO-2026-1002",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Confirmée",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("50"),
                    resource_count=1,
                )
            )
            session.flush()
            session.add(
                ResourceRequirement(
                    id="REQ2",
                    legacy_segment_id="SEG-2",
                    project_id="P1",
                    workforce_request_id="D2",
                    assigned_resource_id="R1",
                    start_date=MONDAY,
                    end_date=FRIDAY,
                    planned_hours=Decimal("40"),
                    status="Planifié",
                    planning_type="Flexible",
                    priority="Normale",
                    confirmation="Confirmée",
                    origin=ORIGIN_REQUEST,
                )
            )
            session.flush()
            session.add(
                Shift(
                    id="S2",
                    resource_requirement_id="REQ2",
                    resource_id="R1",
                    work_date=MONDAY,
                    hours=Decimal("40"),
                    source="MANUAL",
                    locked=True,
                )
            )

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            loads = queries.list_pending_loads(start=MONDAY, end=FRIDAY)
            snapshot = queries.planning_snapshot(start=MONDAY, end=FRIDAY)

        self.assertEqual(len(loads), 1)
        row = loads[0]
        self.assertEqual(row.mode, PENDING_LOAD_REPLACEMENT)
        self.assertEqual(row.projected_hours, 50.0)
        self.assertEqual(row.window_hours, 50.0)
        self.assertEqual(row.current_plan_hours, 40.0)
        self.assertEqual(row.delta_hours, 10.0)
        self.assertEqual(row.candidate_windows, ())
        self.assertEqual(snapshot.firm_hours, 40.0)
        self.assertEqual(snapshot.potential_hours, 0.0)
        self.assertEqual(snapshot.replacement_proposal_hours, 50.0)

    def test_unresolved_exclusive_alternatives_count_once(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D3",
                    legacy_demand_number="DMO-2026-1003",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=TUESDAY,
                    resource_count=1,
                )
            )
            session.flush()
            SqlDemandPeriodRepository(session).replace_for_demand(
                "DMO-2026-1003",
                (
                    DemandPeriodDefinition(
                        period_id="MON",
                        start_date=MONDAY,
                        end_date=MONDAY,
                        hours=8,
                        kind=PERIOD_KIND_ALTERNATIVE,
                        alternative_group="DAY-CHOICE",
                        proposed_resource="Alice",
                    ),
                    DemandPeriodDefinition(
                        period_id="TUE",
                        start_date=TUESDAY,
                        end_date=TUESDAY,
                        hours=8,
                        kind=PERIOD_KIND_ALTERNATIVE,
                        alternative_group="DAY-CHOICE",
                        proposed_resource="Bob",
                    ),
                ),
            )

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            loads = queries.list_pending_loads(start=MONDAY, end=FRIDAY)
            snapshot = queries.planning_snapshot(start=MONDAY, end=FRIDAY)

        self.assertEqual(len(loads), 1)
        row = loads[0]
        self.assertEqual(row.mode, PENDING_LOAD_ADDITIVE)
        self.assertEqual(row.projected_hours, 8.0)
        self.assertEqual(row.window_hours, 8.0)
        self.assertEqual(len(row.periods), 2)
        self.assertFalse(any(period.selected for period in row.periods))
        self.assertEqual(len(row.candidate_windows), 2)
        self.assertEqual({candidate.proposed_resource_id for candidate in row.candidate_windows}, {"R1", "R2"})
        self.assertTrue(all(candidate.period_kind == PERIOD_KIND_ALTERNATIVE for candidate in row.candidate_windows))
        self.assertEqual(snapshot.potential_hours, 8.0)

    def test_unassigned_candidate_is_projected_without_synthetic_resource(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D4",
                    legacy_demand_number="DMO-2026-1004",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=TUESDAY,
                    estimated_hours=Decimal("16"),
                    resource_count=1,
                    required_competencies="PLC",
                )
            )

        with self.factory() as session:
            snapshot = SqlPlannerQueryRepository(session).planning_snapshot(
                start=MONDAY,
                end=FRIDAY,
                project_ids=("P1",),
            )

        self.assertEqual(len(snapshot.pending_loads), 1)
        candidate = snapshot.pending_loads[0].candidate_windows[0]
        self.assertIsNone(candidate.proposed_resource_id)
        self.assertIsNone(candidate.proposed_resource)
        self.assertEqual(candidate.window_hours, 16.0)
        self.assertEqual(candidate.required_competencies, "PLC")
        self.assertEqual(snapshot.resources, ())

    def test_macro_hours_are_prorated_to_the_displayed_window(self) -> None:
        self.assertEqual(
            projected_hours_in_window(40, MONDAY, FRIDAY, MONDAY, MONDAY),
            8.0,
        )


if __name__ == "__main__":
    unittest.main()
