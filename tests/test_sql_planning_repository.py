from __future__ import annotations

import ast
from datetime import date, time
from pathlib import Path
import unittest

from app.domain.planning_projection import project_planning_snapshot
from app.infrastructure.sql import (
    AvailabilityRuleResourceClass,
    Base,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceClassConfig,
    ResourceRequirement,
    Shift,
    SqlPlanningReadRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


ROOT = Path(__file__).resolve().parents[1]
SQL_REPOSITORY = ROOT / "app" / "infrastructure" / "sql" / "planning_repository.py"
D1 = date(2026, 8, 24)  # Monday
D2 = date(2026, 8, 25)


class SqlPlanningReadRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)

        with transactional_session(self.factory) as session:
            session.add_all(
                [
                    ResourceClassConfig(
                        code="Installation",
                        label="Installation",
                        active=True,
                    ),
                    Project(
                        id="P1",
                        number="P-1",
                        name="Projet SQL",
                        client="Client",
                        project_manager_name="CP",
                    ),
                    Resource(
                        id="R1",
                        name="Alice",
                        resource_class="Programmation",
                        active=True,
                        sort_order=1,
                    ),
                    Resource(
                        id="R2",
                        name="Inactive",
                        active=False,
                        sort_order=2,
                    ),
                ]
            )
            session.flush()
            session.add(
                WorkforceRequest(
                    id="D-UUID",
                    legacy_demand_number="DMO-2026-0001",
                    project_id="P1",
                    requester_name="Jean",
                    priority="Urgent",
                    confirmation="Confirmée",
                    desired_start=D1,
                    desired_end=D2,
                    status="En planification",
                )
            )
            session.flush()
            session.add(
                ResourceRequirement(
                    id="S-UUID",
                    legacy_segment_id="SEG-2026-0001",
                    project_id="P1",
                    workforce_request_id="D-UUID",
                    assigned_resource_id="R1",
                    start_date=D1,
                    end_date=D2,
                    planned_hours=12,
                    status="Planifié",
                    required_competency="Programmation",
                    planning_type="Flexible",
                    priority="",
                    outside_standard_hours_allowed=True,
                    origin="REQUEST",
                )
            )
            session.flush()
            session.add_all(
                [
                    ResourceAvailabilityRule(
                        id="AV-STD",
                        legacy_id="STD-R1",
                        resource_id="R1",
                        availability_type="Horaire standard",
                        weekdays="Lun,Mar,Mer,Jeu,Ven",
                        start_time=time(8, 0),
                        end_time=time(16, 0),
                        active=True,
                    ),
                    ResourceAvailabilityRule(
                        id="AV-HOLIDAY",
                        legacy_id="HOLIDAY-1",
                        resource_id=None,
                        availability_type="Jour férié",
                        start_date=D2,
                        end_date=D2,
                        active=True,
                    ),
                    ResourceAvailabilityRule(
                        id="AV-HOLIDAY-INSTALLATION",
                        legacy_id="HOLIDAY-INSTALLATION",
                        resource_id=None,
                        availability_type="Jour férié",
                        start_date=D1,
                        end_date=D1,
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add(
                AvailabilityRuleResourceClass(
                    availability_rule_id="AV-HOLIDAY-INSTALLATION",
                    resource_class_code="Installation",
                )
            )
            session.add_all(
                [
                    Shift(
                        id="A-LOCK-UUID",
                        legacy_allocation_id="A-LOCK",
                        resource_requirement_id="S-UUID",
                        resource_id="R1",
                        work_date=D1,
                        hours=2,
                        allocation_type="Fixe",
                        source="MANUAL",
                        locked=True,
                        outside_standard_hours=False,
                    ),
                    Shift(
                        id="A-AUTO-UUID",
                        legacy_allocation_id="A-AUTO",
                        resource_requirement_id="S-UUID",
                        resource_id="R1",
                        work_date=D1,
                        hours=6,
                        allocation_type="Flexible",
                        source="AUTO",
                        locked=False,
                        outside_standard_hours=False,
                    ),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_capture_projects_complete_compatibility_snapshot(self) -> None:
        with self.factory() as session:
            snapshot = SqlPlanningReadRepository(session).capture()

        self.assertEqual(len(snapshot.segments), 1)
        self.assertEqual(snapshot.segments[0]["IDSegment"], "SEG-2026-0001")
        self.assertEqual(snapshot.segments[0]["NoDemande"], "DMO-2026-0001")
        self.assertEqual(snapshot.segments[0]["Technicien"], "Alice")
        self.assertEqual(snapshot.segments[0]["HorsHoraireAutorise"], True)

        self.assertEqual(len(snapshot.demands), 1)
        self.assertEqual(snapshot.demands[0]["Priorite"], "Urgent")
        self.assertEqual(snapshot.demands[0]["ChargeProjet"], "CP")

        self.assertEqual(len(snapshot.allocations), 2)
        locked = next(row for row in snapshot.allocations if row["IDAllocation"] == "A-LOCK")
        self.assertTrue(locked["Verrouillee"])
        self.assertEqual(locked["Heures"], 2.0)

        global_holiday = next(
            row for row in snapshot.availability if row["ID"] == "HOLIDAY-1"
        )
        self.assertEqual(global_holiday["Technicien"], "")
        self.assertEqual(global_holiday["Type"], "Jour férié")
        class_holiday = next(
            row for row in snapshot.availability if row["ID"] == "HOLIDAY-INSTALLATION"
        )
        self.assertEqual(class_holiday["ClassesRessources"], ("Installation",))

        self.assertEqual(snapshot.technicians, ({
            "name": "Alice",
            "description": "",
            "team": "Programmation",
            "capacity": 0.0,
        },))

    def test_sql_snapshot_drives_existing_typed_projection_unchanged(self) -> None:
        with self.factory() as session:
            calculation = project_planning_snapshot(
                SqlPlanningReadRepository(session).capture()
            )

        self.assertEqual(len(calculation.segments), 1)
        self.assertEqual(calculation.segments[0].segment_id, "SEG-2026-0001")
        self.assertEqual(calculation.segments[0].resource_id, "Alice")
        self.assertEqual(calculation.segments[0].priority_rank, 0)
        self.assertTrue(calculation.segments[0].overtime_allowed)
        self.assertEqual(calculation.capacity_by_resource_day[("Alice", D1)], 8.0)
        self.assertEqual(calculation.capacity_by_resource_day[("Alice", D2)], 0.0)
        self.assertEqual(len(calculation.locked_allocations), 1)
        self.assertEqual(calculation.locked_allocations[0].hours, 2.0)
        self.assertEqual(len(calculation.persisted_allocations), 2)

    def test_sql_planning_repository_has_no_excel_or_v1_dependency(self) -> None:
        source = SQL_REPOSITORY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")

        for forbidden in (
            "xlwings",
            "nicegui",
            "excel_repository",
            "app.v13",
            "app.v14",
            "app.v15",
            "app.v16",
            "app.v17",
            "app.v18",
        ):
            self.assertFalse(
                any(forbidden in module for module in imports),
                f"SQL planning read leaks legacy dependency: {forbidden}",
            )


if __name__ == "__main__":
    unittest.main()
