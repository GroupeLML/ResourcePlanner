from __future__ import annotations

from datetime import date, time
from decimal import Decimal
import unittest

from app.infrastructure.sql import (
    AvailabilityRuleResourceClass,
    Base,
    ORIGIN_REQUEST,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceClassConfig,
    ResourceRequirement,
    Shift,
    SqlPlannerQueryRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


MONDAY = date(2026, 9, 14)
FRIDAY = date(2026, 9, 18)


class MediumTermCapacityReadModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet test", status="Actif"))
            session.add_all(
                [
                    ResourceClassConfig(code="Programmation", label="Programmation", active=True),
                    ResourceClassConfig(code="Installation", label="Installation", active=True),
                    Resource(id="R1", name="Alice", resource_class="Programmation", active=True),
                    Resource(id="R2", name="Bob", resource_class="Installation", active=True),
                ]
            )
            session.flush()
            for resource_id in ("R1", "R2"):
                session.add(
                    ResourceAvailabilityRule(
                        id=f"STD-{resource_id}",
                        resource_id=resource_id,
                        availability_type="Horaire standard",
                        weekdays="Lun,Mar,Mer,Jeu,Ven",
                        start_time=time(8, 0),
                        end_time=time(16, 0),
                        active=True,
                    )
                )

            session.add_all(
                [
                    WorkforceRequest(
                        id="D1",
                        legacy_demand_number="DMO-FIRM",
                        project_id="P1",
                        status="En planification",
                        confirmation="Confirmée",
                        desired_start=MONDAY,
                        desired_end=FRIDAY,
                    ),
                    WorkforceRequest(
                        id="D2",
                        legacy_demand_number="DMO-TENTATIVE",
                        project_id="P1",
                        status="En planification",
                        confirmation="Tentative",
                        desired_start=MONDAY,
                        desired_end=FRIDAY,
                    ),
                    WorkforceRequest(
                        id="D3",
                        legacy_demand_number="DMO-SUBMITTED",
                        project_id="P1",
                        status="Soumise",
                        confirmation="Confirmée",
                        desired_start=MONDAY,
                        desired_end=FRIDAY,
                        estimated_hours=Decimal("10"),
                        proposed_resource_id="R1",
                    ),
                    WorkforceRequest(
                        id="D4",
                        legacy_demand_number="DMO-REPLACEMENT",
                        project_id="P1",
                        status="Soumise",
                        confirmation="Confirmée",
                        desired_start=MONDAY,
                        desired_end=FRIDAY,
                        estimated_hours=Decimal("20"),
                        proposed_resource_id="R2",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ResourceRequirement(
                        id="REQ1",
                        project_id="P1",
                        workforce_request_id="D1",
                        assigned_resource_id="R1",
                        start_date=MONDAY,
                        end_date=FRIDAY,
                        planned_hours=Decimal("16"),
                        status="Planifié",
                        confirmation="Confirmée",
                        origin=ORIGIN_REQUEST,
                    ),
                    ResourceRequirement(
                        id="REQ2",
                        project_id="P1",
                        workforce_request_id="D2",
                        assigned_resource_id="R1",
                        start_date=MONDAY,
                        end_date=FRIDAY,
                        planned_hours=Decimal("8"),
                        status="Planifié",
                        confirmation="Tentative",
                        origin=ORIGIN_REQUEST,
                    ),
                    ResourceRequirement(
                        id="REQ4",
                        project_id="P1",
                        workforce_request_id="D4",
                        assigned_resource_id="R2",
                        start_date=MONDAY,
                        end_date=FRIDAY,
                        planned_hours=Decimal("4"),
                        status="Planifié",
                        confirmation="Confirmée",
                        origin=ORIGIN_REQUEST,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    Shift(
                        id="S1",
                        resource_requirement_id="REQ1",
                        resource_id="R1",
                        work_date=MONDAY,
                        hours=Decimal("16"),
                        source="MANUAL",
                        locked=True,
                    ),
                    Shift(
                        id="S2",
                        resource_requirement_id="REQ2",
                        resource_id="R1",
                        work_date=MONDAY,
                        hours=Decimal("8"),
                        source="MANUAL",
                        locked=True,
                    ),
                    Shift(
                        id="S4",
                        resource_requirement_id="REQ4",
                        resource_id="R2",
                        work_date=MONDAY,
                        hours=Decimal("4"),
                        source="MANUAL",
                        locked=True,
                    ),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_weekly_capacity_separates_firm_potential_and_replacement(self) -> None:
        with self.factory() as session:
            snapshot = SqlPlannerQueryRepository(session).planning_snapshot(
                start=MONDAY,
                end=FRIDAY,
            )

        total = next(bucket for bucket in snapshot.capacity_buckets if bucket.resource_class is None)
        programming = next(
            bucket for bucket in snapshot.capacity_buckets if bucket.resource_class == "Programmation"
        )
        installation = next(
            bucket for bucket in snapshot.capacity_buckets if bucket.resource_class == "Installation"
        )

        self.assertEqual(total.capacity_hours, 80.0)
        self.assertEqual(total.firm_hours, 20.0)
        self.assertEqual(total.current_potential_hours, 8.0)
        self.assertEqual(total.submitted_hours, 10.0)
        self.assertEqual(total.replacement_proposal_hours, 20.0)
        self.assertEqual(total.replacement_delta_hours, 16.0)
        self.assertEqual(total.exposure_hours, 38.0)
        self.assertEqual(total.residual_hours, 42.0)

        self.assertEqual(programming.capacity_hours, 40.0)
        self.assertEqual(programming.firm_hours, 16.0)
        self.assertEqual(programming.current_potential_hours, 8.0)
        self.assertEqual(programming.submitted_hours, 10.0)
        self.assertEqual(programming.exposure_hours, 34.0)
        self.assertEqual(programming.utilization_pct, 85.0)
        self.assertEqual(programming.state, "warning")

        self.assertEqual(installation.capacity_hours, 40.0)
        self.assertEqual(installation.firm_hours, 4.0)
        self.assertEqual(installation.replacement_proposal_hours, 20.0)
        self.assertEqual(installation.exposure_hours, 4.0)
        self.assertEqual(installation.residual_hours, 36.0)

    def test_class_scoped_holiday_reduces_only_target_class_capacity(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                ResourceAvailabilityRule(
                    id="HOL-PROG",
                    availability_type="Jour férié",
                    start_date=MONDAY,
                    end_date=MONDAY,
                    active=True,
                )
            )
            session.flush()
            session.add(
                AvailabilityRuleResourceClass(
                    availability_rule_id="HOL-PROG",
                    resource_class_code="Programmation",
                )
            )

        with self.factory() as session:
            snapshot = SqlPlannerQueryRepository(session).planning_snapshot(
                start=MONDAY,
                end=FRIDAY,
            )

        total = next(bucket for bucket in snapshot.capacity_buckets if bucket.resource_class is None)
        programming = next(
            bucket for bucket in snapshot.capacity_buckets if bucket.resource_class == "Programmation"
        )
        installation = next(
            bucket for bucket in snapshot.capacity_buckets if bucket.resource_class == "Installation"
        )
        self.assertEqual(total.capacity_hours, 72.0)
        self.assertEqual(programming.capacity_hours, 32.0)
        self.assertEqual(installation.capacity_hours, 40.0)

    def test_vacation_reduces_capacity_without_changing_load(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                ResourceAvailabilityRule(
                    id="VAC-R1",
                    resource_id="R1",
                    availability_type="Vacances",
                    start_date=MONDAY,
                    end_date=MONDAY,
                    active=True,
                )
            )

        with self.factory() as session:
            snapshot = SqlPlannerQueryRepository(session).planning_snapshot(
                start=MONDAY,
                end=FRIDAY,
            )

        programming = next(
            bucket for bucket in snapshot.capacity_buckets if bucket.resource_class == "Programmation"
        )
        self.assertEqual(programming.capacity_hours, 32.0)
        self.assertEqual(programming.exposure_hours, 34.0)
        self.assertEqual(programming.state, "overloaded")
        self.assertEqual(programming.residual_hours, -2.0)


if __name__ == "__main__":
    unittest.main()
