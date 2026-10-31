from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from sqlalchemy import select

from app.domain.active_days import (
    normalize_active_day_target,
    split_total_workforce_hours,
)
from app.domain.demand_periods import DemandPeriodDefinition, projected_hours_without_double_counting
from app.domain.planning_engine import (
    LockedAllocationInput,
    SegmentInput,
    build_allocation_plan,
)
from app.infrastructure.sql import (
    Base,
    Project,
    Resource,
    ResourceRequirement,
    SqlDemandPeriodRepository,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


D1 = date(2026, 9, 14)
D2 = date(2026, 9, 15)
D3 = date(2026, 9, 16)
D4 = date(2026, 9, 17)
D5 = date(2026, 9, 18)
DAYS = (D1, D2, D3, D4, D5)


class ActiveDayPolicyTests(unittest.TestCase):
    def test_total_hours_are_split_without_multiplying_resource_count(self) -> None:
        self.assertEqual(split_total_workforce_hours(48, 2), (24.0, 24.0))
        thirds = split_total_workforce_hours(10, 3)
        self.assertEqual(round(sum(thirds), 2), 10.0)
        self.assertEqual(thirds, (3.34, 3.33, 3.33))

        period = DemandPeriodDefinition(
            period_id="P-48",
            start_date=D1,
            end_date=D5,
            hours=48,
            resource_count=2,
            desired_active_days=3,
        )
        self.assertEqual(projected_hours_without_double_counting((period,)), 48.0)

    def test_active_day_target_must_be_whole_positive_and_fit_window(self) -> None:
        self.assertEqual(normalize_active_day_target(3, start=D1, end=D5), 3)
        with self.assertRaisesRegex(ValueError, "entier positif"):
            normalize_active_day_target(2.5, start=D1, end=D5)
        with self.assertRaisesRegex(ValueError, "dépasse"):
            normalize_active_day_target(6, start=D1, end=D5)

    def test_flexible_work_prefers_exact_active_day_target_when_capacity_allows(self) -> None:
        segment = SegmentInput(
            segment_id="S1",
            resource_id="Alice",
            start=D1,
            end=D5,
            hours=24,
            desired_active_days=3,
        )
        capacity = {("Alice", day): 8.0 for day in DAYS}

        result = build_allocation_plan((segment,), (), capacity)

        real = [row for row in result.allocations if row.counts_as_allocated]
        self.assertEqual([(row.day, row.hours) for row in real], [(D1, 8.0), (D2, 8.0), (D3, 8.0)])
        self.assertEqual(result.active_day_diagnostics, ())

    def test_capacity_can_expand_beyond_target_with_explicit_diagnostic(self) -> None:
        segment = SegmentInput(
            segment_id="S1",
            resource_id="Alice",
            start=D1,
            end=D5,
            hours=24,
            desired_active_days=2,
        )
        capacity = {("Alice", day): 8.0 for day in DAYS}

        result = build_allocation_plan((segment,), (), capacity)

        self.assertEqual(len({row.day for row in result.allocations if row.counts_as_allocated}), 3)
        self.assertEqual(len(result.active_day_diagnostics), 1)
        diagnostic = result.active_day_diagnostics[0]
        self.assertEqual(diagnostic.code, "CAPACITY_REQUIRES_MORE_DAYS")
        self.assertEqual(diagnostic.desired_active_days, 2)
        self.assertEqual(diagnostic.planned_active_days, 3)

    def test_locked_days_count_toward_target(self) -> None:
        segment = SegmentInput(
            segment_id="S1",
            resource_id="Alice",
            start=D1,
            end=D5,
            hours=24,
            desired_active_days=3,
        )
        locked = (
            LockedAllocationInput(
                segment_id="S1",
                resource_id="Alice",
                day=D1,
                hours=8,
            ),
        )
        capacity = {("Alice", day): 8.0 for day in DAYS}

        result = build_allocation_plan((segment,), locked, capacity)

        real = [row for row in result.allocations if row.counts_as_allocated]
        self.assertEqual({row.day for row in real}, {D1, D2, D3})
        self.assertEqual(result.active_day_diagnostics, ())

    def test_fixed_segment_ignores_active_day_distribution_target(self) -> None:
        segment = SegmentInput(
            segment_id="FIXED",
            resource_id="Alice",
            start=D1,
            end=D2,
            hours=12,
            plan_type="Fixe",
            desired_active_days=1,
        )
        capacity = {("Alice", D1): 8.0, ("Alice", D2): 8.0}

        result = build_allocation_plan((segment,), (), capacity)

        self.assertEqual(round(result.allocated_hours, 2), 12.0)
        self.assertEqual(result.active_day_diagnostics, ())


class EstimatedDaysSqlSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet #68"))
            session.add_all(
                [
                    Resource(id="R1", name="Alice", active=True),
                    Resource(id="R2", name="Bob", active=True),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _requirements(session, request_id: str) -> list[ResourceRequirement]:
        return list(
            session.scalars(
                select(ResourceRequirement)
                .where(
                    ResourceRequirement.workforce_request_id == request_id,
                    ResourceRequirement.status != "Annulé",
                )
                .order_by(ResourceRequirement.created_at, ResourceRequirement.id)
            ).all()
        )

    def test_legacy_request_48h_3days_2resources_materializes_48h_total(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DEM-68",
                    project_id="P1",
                    desired_start=D1,
                    desired_end=D5,
                    estimated_hours=Decimal("48"),
                    estimated_days=Decimal("3"),
                    resource_count=2,
                    proposed_resource_id="R1",
                    status="En planification",
                )
            )
            session.flush()

            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-68")

            rows = self._requirements(session, "D1")
            self.assertEqual(len(rows), 2)
            self.assertEqual([row.planned_hours for row in rows], [Decimal("24.00"), Decimal("24.00")])
            self.assertEqual(sum(row.planned_hours for row in rows), Decimal("48.00"))
            self.assertEqual([row.desired_active_days for row in rows], [3, 3])
            self.assertTrue(all(row.assigned_resource_id is None for row in rows))

    def test_days_only_new_request_is_rejected_without_inventing_hours(self) -> None:
        with self.assertRaisesRegex(ValueError, "ne définissent pas les heures"):
            with transactional_session(self.factory) as session:
                session.add(
                    WorkforceRequest(
                        id="D2",
                        legacy_demand_number="DEM-DAYS",
                        project_id="P1",
                        desired_start=D1,
                        desired_end=D5,
                        estimated_hours=None,
                        estimated_days=Decimal("3"),
                        resource_count=1,
                        status="En planification",
                    )
                )
                session.flush()
                SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-DAYS")

        with transactional_session(self.factory) as session:
            self.assertEqual(self._requirements(session, "D2"), [])

    def test_period_48h_3days_2resources_splits_total_without_promoting_proposal(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                WorkforceRequest(
                    id="D3",
                    legacy_demand_number="DEM-PER",
                    project_id="P1",
                    desired_start=D1,
                    desired_end=D5,
                    estimated_hours=Decimal("48"),
                    resource_count=2,
                    status="En planification",
                )
            )
            session.flush()
            periods = SqlDemandPeriodRepository(session)
            periods.replace_for_demand(
                "DEM-PER",
                (
                    DemandPeriodDefinition(
                        period_id="PER-1",
                        start_date=D1,
                        end_date=D5,
                        hours=48,
                        resource_count=2,
                        desired_active_days=3,
                        proposed_resource="Alice",
                    ),
                ),
            )

            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-PER")

            rows = self._requirements(session, "D3")
            self.assertEqual(len(rows), 2)
            self.assertEqual(sum(row.planned_hours for row in rows), Decimal("48.00"))
            self.assertEqual(sorted(row.planned_hours for row in rows), [Decimal("24.00"), Decimal("24.00")])
            self.assertEqual([row.desired_active_days for row in rows], [3, 3])
            self.assertTrue(all(row.assigned_resource_id is None for row in rows))


if __name__ == "__main__":
    unittest.main()
