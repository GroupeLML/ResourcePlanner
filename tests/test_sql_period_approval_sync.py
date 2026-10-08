from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from sqlalchemy import select

from app.domain.demand_periods import (
    DemandPeriodDefinition,
    PERIOD_KIND_ALTERNATIVE,
    PERIOD_KIND_CUMULATIVE,
    PROPOSED_RESOURCE_MODE_EXPLICIT,
    PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
)
from app.infrastructure.sql import (
    Base,
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    SqlDemandPeriodRepository,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    WorkforceRequest,
    WorkforceRequestPeriodRequirement,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.same_resource_periods import SqlSameResourcePeriodCoordinator
from app.infrastructure.sql.query_repository import SqlPlannerQueryRepository


D1 = date(2026, 9, 7)
D2 = date(2026, 9, 8)
D3 = date(2026, 9, 9)


class SqlPeriodApprovalSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet périodes"))
            session.add_all(
                [
                    Resource(id="R1", name="Alice", active=True),
                    Resource(id="R2", name="Bob", active=True),
                ]
            )
            session.add(
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DEM-1",
                    project_id="P1",
                    desired_start=D1,
                    desired_end=D3,
                    estimated_hours=Decimal("8"),
                    resource_count=1,
                    required_competencies="Programmation",
                    priority="Normale",
                    description="Support projet",
                    status="En planification",
                    approved_by_name="coord-test-user",
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _alternatives() -> tuple[DemandPeriodDefinition, ...]:
        return (
            DemandPeriodDefinition(
                period_id="OPT-A",
                start_date=D1,
                end_date=D1,
                hours=8,
                kind=PERIOD_KIND_ALTERNATIVE,
                alternative_group="VISITE",
                proposed_resource="Alice",
            ),
            DemandPeriodDefinition(
                period_id="OPT-B",
                start_date=D2,
                end_date=D2,
                hours=8,
                kind=PERIOD_KIND_ALTERNATIVE,
                alternative_group="VISITE",
                proposed_resource="Bob",
            ),
        )

    def _active_requirements(self, session) -> list[ResourceRequirement]:
        return list(
            session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.workforce_request_id == "D1",
                    ResourceRequirement.status != "Annulé",
                )
            ).all()
        )

    def test_unresolved_alternative_group_materializes_no_requirement(self) -> None:
        with transactional_session(self.factory) as session:
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand("DEM-1", self._alternatives())

            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")

            self.assertEqual(self._active_requirements(session), [])

    def test_only_selected_alternative_materializes_and_switch_replaces_it(self) -> None:
        with transactional_session(self.factory) as session:
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand("DEM-1", self._alternatives())
            periods.select_alternative("DEM-1", "VISITE", "OPT-A")
            sync = SqlPeriodAwareApprovedDemandSyncAdapter(session)

            sync.sync_approved("DEM-1")
            active = self._active_requirements(session)
            self.assertEqual(len(active), 1)
            first = active[0]
            self.assertEqual(first.start_date, D1)
            self.assertEqual(first.end_date, D1)
            self.assertEqual(first.planned_hours, Decimal("8.00"))
            self.assertEqual(first.assigned_resource_id, "R1")
            first_link = session.get(WorkforceRequestPeriodRequirement, first.id)
            self.assertIsNotNone(first_link)

            periods.select_alternative("DEM-1", "VISITE", "OPT-B")
            sync.sync_approved("DEM-1")

            active = self._active_requirements(session)
            self.assertEqual(len(active), 1)
            second = active[0]
            self.assertNotEqual(second.id, first.id)
            self.assertEqual(second.start_date, D2)
            self.assertEqual(second.assigned_resource_id, "R2")
            self.assertEqual(session.get(ResourceRequirement, first.id).status, "Annulé")

    def test_cumulative_period_and_selected_alternative_are_both_effective(self) -> None:
        with transactional_session(self.factory) as session:
            definitions = (
                DemandPeriodDefinition(
                    period_id="PREP",
                    start_date=D1,
                    end_date=D1,
                    hours=4,
                    kind=PERIOD_KIND_CUMULATIVE,
                    proposed_resource="Alice",
                ),
                *self._alternatives(),
            )
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand("DEM-1", definitions)
            periods.select_alternative("DEM-1", "VISITE", "OPT-B")

            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")

            active = sorted(self._active_requirements(session), key=lambda row: row.start_date)
            self.assertEqual(len(active), 2)
            self.assertEqual([(row.start_date, float(row.planned_hours)) for row in active], [(D1, 4.0), (D2, 8.0)])
            self.assertEqual(sum(float(row.planned_hours) for row in active), 12.0)

    def test_same_as_period_materialization_can_be_resolved_collectively(self) -> None:
        with transactional_session(self.factory) as session:
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand(
                "DEM-1",
                (
                    DemandPeriodDefinition(
                        period_id="ROOT",
                        start_date=D1,
                        end_date=D1,
                        hours=4,
                        kind=PERIOD_KIND_CUMULATIVE,
                        proposed_resource="Alice",
                        proposed_resource_mode=PROPOSED_RESOURCE_MODE_EXPLICIT,
                    ),
                    DemandPeriodDefinition(
                        period_id="FOLLOW",
                        start_date=D2,
                        end_date=D2,
                        hours=4,
                        kind=PERIOD_KIND_CUMULATIVE,
                        proposed_resource_mode=PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
                        same_as_period_id="ROOT",
                    ),
                ),
            )

            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")
            active = sorted(self._active_requirements(session), key=lambda row: row.start_date)
            self.assertEqual(len(active), 2)
            self.assertEqual(active[0].assigned_resource_id, "R1")
            self.assertIsNone(active[1].assigned_resource_id)

            summary = SqlSameResourcePeriodCoordinator(session).enforce_targets()

            self.assertEqual(summary.group_count, 1)
            self.assertEqual(summary.unresolved_group_count, 0)
            self.assertEqual({row.assigned_resource_id for row in active}, {"R1"})

    def _set_legacy_resource_class(self, session, class_code: str) -> None:
        """Mirror a simple request created via the React class selector (#535)."""
        session.add(
            RequestLine(
                id="D1",
                workforce_request_id="D1",
                position=0,
                kind="WORKFORCE",
                required_resource_class=class_code,
                desired_start=D1,
                desired_end=D3,
                estimated_hours=Decimal("8"),
                confirmation="Confirmée",
                active=True,
            )
        )
        session.flush()

    def test_legacy_simple_class_reaches_unplanned_planning_action(self) -> None:
        with transactional_session(self.factory) as session:
            self._set_legacy_resource_class(session, "INSTALL_ELEC")
            sync = SqlPeriodAwareApprovedDemandSyncAdapter(session)
            sync.sync_approved("DEM-1")

            active = self._active_requirements(session)
            self.assertEqual(len(active), 1)
            requirement = active[0]
            self.assertIsNone(requirement.assigned_resource_id)
            self.assertEqual(requirement.required_resource_class, "INSTALL_ELEC")

            actions = SqlPlannerQueryRepository(session).list_planning_actions(
                start=D1, end=D3,
            )
            unplanned = [action for action in actions if action.kind == "ASSIGNMENT"]
            self.assertEqual(len(unplanned), 1)
            self.assertEqual(unplanned[0].required_resource_class, "INSTALL_ELEC")
            self.assertEqual(unplanned[0].planned_hours, 8.0)

            # Restore an already approved requirement from its immutable approval
            # rather than silently reapproving the current editable request.
            requirement.required_resource_class = None
            session.flush()
            sync.sync_operational_choices("DEM-1")
            repaired = self._active_requirements(session)
            self.assertEqual(len(repaired), 1)
            self.assertEqual(repaired[0].id, requirement.id)
            self.assertEqual(repaired[0].required_resource_class, "INSTALL_ELEC")
            self.assertIsNone(repaired[0].assigned_resource_id)

    def test_legacy_periods_keep_class_on_each_materialized_requirement(self) -> None:
        with transactional_session(self.factory) as session:
            self._set_legacy_resource_class(session, "INSTALL_ELEC")
            periods = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            periods.replace_for_demand(
                "DEM-1",
                (
                    DemandPeriodDefinition(
                        period_id="PREP",
                        start_date=D1,
                        end_date=D1,
                        hours=4,
                        kind=PERIOD_KIND_CUMULATIVE,
                    ),
                    DemandPeriodDefinition(
                        period_id="FIELD",
                        start_date=D2,
                        end_date=D2,
                        hours=4,
                        kind=PERIOD_KIND_CUMULATIVE,
                    ),
                ),
            )
            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")
            requirements = self._active_requirements(session)
            self.assertEqual(len(requirements), 2)
            self.assertEqual(
                {row.required_resource_class for row in requirements},
                {"INSTALL_ELEC"},
            )
            self.assertTrue(all(row.assigned_resource_id is None for row in requirements))
            self.assertEqual(sum(float(row.planned_hours) for row in requirements), 8.0)

    def test_request_without_detailed_periods_preserves_legacy_sync(self) -> None:
        with transactional_session(self.factory) as session:
            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved("DEM-1")

            active = self._active_requirements(session)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].start_date, D1)
            self.assertEqual(active[0].end_date, D3)
            self.assertEqual(active[0].planned_hours, Decimal("8.00"))
            self.assertIsNone(
                session.get(WorkforceRequestPeriodRequirement, active[0].id)
            )


if __name__ == "__main__":
    unittest.main()
