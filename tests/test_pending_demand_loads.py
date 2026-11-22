from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest
from unittest import mock

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
    RequestLine,
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

    def test_planning_snapshot_reuses_complete_scoped_demands_once(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D5",
                    legacy_demand_number="DMO-2026-1005",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("24"),
                    resource_count=1,
                )
            )

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            with mock.patch.object(
                queries._demands,
                "list",
                wraps=queries._demands.list,
            ) as demand_list:
                snapshot = queries.planning_snapshot(
                    start=MONDAY,
                    end=FRIDAY,
                    project_ids=("P1",),
                    demand_ids=("D5",),
                )

        self.assertEqual(len(snapshot.demands), 1)
        self.assertEqual(len(snapshot.pending_loads), 1)
        self.assertEqual(snapshot.pending_loads[0].projected_hours, 24.0)
        demand_list.assert_called_once_with(
            project_ids=("P1",),
            demand_ids=("D5",),
        )

    def test_pending_window_uses_complete_scope_before_snapshot_date_filter(self) -> None:
        october_start = date(2026, 10, 1)
        october_end = date(2026, 10, 2)
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D6",
                    legacy_demand_number="DMO-2026-1006",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Tentative",
                    desired_start=october_start,
                    desired_end=october_end,
                    resource_count=1,
                )
            )
            session.flush()
            SqlDemandPeriodRepository(session).replace_for_demand(
                "DMO-2026-1006",
                (
                    DemandPeriodDefinition(
                        period_id="SEP-WINDOW",
                        start_date=MONDAY,
                        end_date=FRIDAY,
                        hours=40,
                    ),
                ),
            )

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            snapshot = queries.planning_snapshot(
                start=MONDAY,
                end=FRIDAY,
                project_ids=("P1",),
                demand_ids=("D6",),
            )

        self.assertEqual(snapshot.demands, ())
        self.assertEqual(len(snapshot.pending_loads), 1)
        pending = snapshot.pending_loads[0]
        self.assertEqual(pending.projected_hours, 40.0)
        self.assertEqual(pending.window_hours, 40.0)
        self.assertEqual(len(pending.candidate_windows), 1)
        self.assertEqual(
            pending.candidate_windows[0].candidate_key,
            "DMO-2026-1006:SEP-WINDOW",
        )

    def test_line_mode_snapshot_pending_load_matches_standalone_projection(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D7",
                    legacy_demand_number="DMO-2026-1007",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    resource_count=1,
                    line_mode=True,
                )
            )
            session.add(
                RequestLine(
                    id="L7",
                    workforce_request_id="D7",
                    position=0,
                    kind="WORKFORCE",
                    slot_count=1,
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("16"),
                    confirmation="Tentative",
                    proposed_resource_id="R1",
                    active=True,
                )
            )

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            standalone = queries.list_pending_loads(
                start=MONDAY,
                end=FRIDAY,
                project_ids=("P1",),
                demand_ids=("D7",),
            )
            snapshot = queries.planning_snapshot(
                start=MONDAY,
                end=FRIDAY,
                project_ids=("P1",),
                demand_ids=("D7",),
            )

        self.assertEqual(snapshot.pending_loads, standalone)
        self.assertEqual(len(snapshot.pending_loads), 1)
        pending = snapshot.pending_loads[0]
        self.assertEqual(pending.projected_hours, 16.0)
        self.assertEqual(pending.window_hours, 16.0)
        self.assertEqual(len(pending.candidate_windows), 1)
        self.assertEqual(
            pending.candidate_windows[0].proposed_resource_id,
            "R1",
        )

    def test_selected_alternative_snapshot_matches_standalone_projection(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D10",
                    legacy_demand_number="DMO-2026-1010",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=TUESDAY,
                    resource_count=1,
                )
            )
            session.flush()
            periods = SqlDemandPeriodRepository(session)
            periods.replace_for_demand(
                "DMO-2026-1010",
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
            periods.select_alternative(
                "DMO-2026-1010",
                "DAY-CHOICE",
                "TUE",
            )

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            standalone = queries.list_pending_loads(
                start=MONDAY,
                end=FRIDAY,
                demand_ids=("D10",),
            )
            snapshot = queries.planning_snapshot(
                start=MONDAY,
                end=FRIDAY,
                demand_ids=("D10",),
            )

        self.assertEqual(snapshot.pending_loads, standalone)
        self.assertEqual(len(snapshot.pending_loads), 1)
        pending = snapshot.pending_loads[0]
        self.assertEqual(pending.projected_hours, 8.0)
        selected = [
            period.period_id
            for period in pending.periods
            if period.selected
        ]
        self.assertEqual(selected, ["TUE"])

    def test_standalone_pending_load_still_fetches_its_own_projection(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D8",
                    legacy_demand_number="DMO-2026-1008",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("12"),
                    resource_count=1,
                )
            )

        with self.factory() as session:
            queries = SqlPlannerQueryRepository(session)
            with mock.patch.object(
                queries._demands,
                "list",
                wraps=queries._demands.list,
            ) as demand_list:
                loads = queries.list_pending_loads(
                    start=MONDAY,
                    end=FRIDAY,
                    project_ids=("P1",),
                    demand_ids=("D8",),
                )

        self.assertEqual(len(loads), 1)
        self.assertEqual(loads[0].projected_hours, 12.0)
        demand_list.assert_called_once_with(
            project_ids=("P1",),
            demand_ids=("D8",),
        )

    def test_snapshot_reuse_does_not_cross_request_boundary(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D9",
                    legacy_demand_number="DMO-2026-1009",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("16"),
                    resource_count=1,
                )
            )

        with self.factory() as session:
            first = SqlPlannerQueryRepository(session).planning_snapshot(
                start=MONDAY,
                end=FRIDAY,
                project_ids=("P1",),
                demand_ids=("D9",),
            )
        self.assertEqual(first.pending_loads[0].projected_hours, 16.0)

        with transactional_session(self.factory) as session:
            request = session.get(WorkforceRequest, "D9")
            assert request is not None
            request.estimated_hours = Decimal("24")

        with self.factory() as session:
            second = SqlPlannerQueryRepository(session).planning_snapshot(
                start=MONDAY,
                end=FRIDAY,
                project_ids=("P1",),
                demand_ids=("D9",),
            )
        self.assertEqual(second.pending_loads[0].projected_hours, 24.0)

    def test_macro_hours_are_prorated_to_the_displayed_window(self) -> None:
        self.assertEqual(
            projected_hours_in_window(40, MONDAY, FRIDAY, MONDAY, MONDAY),
            8.0,
        )


if __name__ == "__main__":
    unittest.main()
