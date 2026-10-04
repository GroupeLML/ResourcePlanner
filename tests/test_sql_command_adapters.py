from __future__ import annotations

import ast
from datetime import date, time
from decimal import Decimal
from pathlib import Path
import unittest

from sqlalchemy import func, select

from app.application.allocation_service import AllocationService
from app.application.demand_service import DemandService
from app.application.planning_service import PlanningService
from app.application.quick_shift_service import QuickShiftService
from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.infrastructure.sql import (
    Base,
    BusinessContact,
    ORIGIN_QUICK_SHIFT,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    SqlAllocationCommandAdapter,
    SqlApprovedDemandSyncAdapter,
    SqlDemandRepository,
    SqlPlanningCommandAdapter,
    SqlSegmentRepository,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


ROOT = Path(__file__).resolve().parents[1]
COMMAND_ADAPTERS = ROOT / "app" / "infrastructure" / "sql" / "command_adapters.py"
D1 = date(2026, 8, 24)  # lundi
D2 = date(2026, 8, 25)


class _FailingPlanning:
    def rebuild(self):
        raise RuntimeError("échec de recalcul simulé")


class SqlCommandAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet SQL"))
            session.add(
                BusinessContact(
                    id="C-MOVE-RESP",
                    display_name="Responsable move",
                    active=True,
                )
            )
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
                        id=f"STD-{resource_id}",
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

    def _add_requirement(
        self,
        session,
        *,
        identifier: str = "SEG-LOCK",
        hours: Decimal = Decimal("12"),
        resource_id: str | None = "R1",
    ) -> ResourceRequirement:
        requirement = ResourceRequirement(
            id=f"REQ-{identifier}",
            legacy_segment_id=identifier,
            project_id="P1",
            assigned_resource_id=resource_id,
            start_date=D1,
            end_date=D2,
            planned_hours=hours,
            status="Planifié",
            planning_type="Flexible",
            priority="Normale",
            origin="QUICK_SHIFT",
        )
        session.add(requirement)
        session.flush()
        return requirement

    def test_rebuild_preserves_locked_shift_and_is_repeatable(self) -> None:
        with transactional_session(self.factory) as session:
            requirement = self._add_requirement(session)
            session.add(
                Shift(
                    id="LOCK-1",
                    legacy_allocation_id="MAN-LOCK-1",
                    resource_requirement_id=requirement.id,
                    resource_id="R1",
                    work_date=D1,
                    hours=Decimal("4"),
                    allocation_type="Flexible",
                    source="MANUAL",
                    locked=True,
                )
            )
            session.flush()

            adapter = SqlPlanningCommandAdapter(session)
            first = adapter.rebuild()
            second = adapter.rebuild()

            locked = session.get(Shift, "LOCK-1")
            self.assertIsNotNone(locked)
            assert locked is not None
            self.assertTrue(locked.locked)
            self.assertEqual(locked.hours, Decimal("4.00"))
            self.assertEqual(first["locked_allocations"], 1)
            self.assertEqual(second["locked_allocations"], 1)
            self.assertEqual(first["allocated_hours"], 12.0)
            self.assertEqual(second["allocated_hours"], 12.0)

            shifts = session.scalars(
                select(Shift).where(Shift.resource_requirement_id == requirement.id)
            ).all()
            self.assertEqual(sum(float(row.hours) for row in shifts), 12.0)
            self.assertEqual(sum(1 for row in shifts if row.locked), 1)

    def test_rebuild_counts_locked_shift_capacity_without_automatic_target(self) -> None:
        with transactional_session(self.factory) as session:
            locked_requirement = self._add_requirement(
                session,
                identifier="SEG-LOCKED-ONLY",
                hours=Decimal("8"),
                resource_id=None,
            )
            target_requirement = self._add_requirement(
                session,
                identifier="SEG-BOB",
                hours=Decimal("8"),
                resource_id="R2",
            )
            target_requirement.end_date = D1
            session.add(
                Shift(
                    id="LOCK-NO-TARGET",
                    legacy_allocation_id="MAN-NO-TARGET",
                    resource_requirement_id=locked_requirement.id,
                    resource_id="R2",
                    work_date=D1,
                    hours=Decimal("8"),
                    allocation_type="Flexible",
                    source="MANUAL",
                    locked=True,
                )
            )
            session.flush()

            summary = SqlPlanningCommandAdapter(session).rebuild()

            locked = session.get(Shift, "LOCK-NO-TARGET")
            self.assertIsNotNone(locked)
            assert locked is not None
            self.assertTrue(locked.locked)
            self.assertEqual(locked.resource_id, "R2")
            self.assertIsNone(locked_requirement.assigned_resource_id)

            target_shifts = session.scalars(
                select(Shift).where(
                    Shift.resource_requirement_id == target_requirement.id
                )
            ).all()
            counted_target = [
                row
                for row in target_shifts
                if row.allocation_type != MISSING_ALLOCATION_TYPE
            ]
            self.assertEqual(counted_target, [])
            self.assertEqual(summary["locked_allocations"], 1)
            self.assertEqual(summary["allocated_hours"], 0.0)
            self.assertEqual(summary["unallocated_hours"], 8.0)

    def test_manual_shift_mutations_keep_automatic_target_and_accept_resource_ids(self) -> None:
        with transactional_session(self.factory) as session:
            requirement = self._add_requirement(
                session,
                identifier="SEG-TARGET-STABLE",
                hours=Decimal("8"),
                resource_id="R1",
            )
            adapter = SqlAllocationCommandAdapter(session)

            allocation_id = adapter.create_manual(
                "SEG-TARGET-STABLE",
                "R2",
                D1,
                2,
                False,
            )
            self.assertEqual(requirement.assigned_resource_id, "R1")

            manual = session.scalar(
                select(Shift).where(Shift.legacy_allocation_id == allocation_id)
            )
            self.assertIsNotNone(manual)
            assert manual is not None
            self.assertEqual(manual.resource_id, "R2")
            self.assertTrue(manual.locked)
            manual.operational_responsible_override_contact_id = "C-MOVE-RESP"
            session.flush()

            auto = session.scalars(
                select(Shift).where(
                    Shift.resource_requirement_id == requirement.id,
                    Shift.locked.is_(False),
                )
            ).all()
            self.assertTrue(auto)
            self.assertTrue(all(row.resource_id == "R1" for row in auto))
            self.assertEqual(sum(float(row.hours) for row in auto), 6.0)

            adapter.update_manual(allocation_id, "R2", D1, 3, False)
            self.assertEqual(requirement.assigned_resource_id, "R1")
            manual = session.scalar(
                select(Shift).where(Shift.legacy_allocation_id == allocation_id)
            )
            assert manual is not None
            self.assertEqual(manual.resource_id, "R2")
            self.assertEqual(manual.hours, Decimal("3.00"))

            adapter.move_manual(allocation_id, "R2", D2)
            self.assertEqual(requirement.assigned_resource_id, "R1")
            manual = session.scalar(
                select(Shift).where(Shift.legacy_allocation_id == allocation_id)
            )
            assert manual is not None
            self.assertEqual(manual.resource_id, "R2")
            self.assertEqual(manual.work_date, D2)
            self.assertEqual(
                manual.operational_responsible_override_contact_id,
                "C-MOVE-RESP",
            )

            auto = session.scalars(
                select(Shift).where(
                    Shift.resource_requirement_id == requirement.id,
                    Shift.locked.is_(False),
                )
            ).all()
            self.assertTrue(auto)
            self.assertTrue(all(row.resource_id == "R1" for row in auto))
            self.assertEqual(sum(float(row.hours) for row in auto), 5.0)

    def test_manual_shift_does_not_assign_unassigned_requirement(self) -> None:
        with transactional_session(self.factory) as session:
            requirement = self._add_requirement(
                session,
                identifier="SEG-NO-TARGET",
                hours=Decimal("8"),
                resource_id=None,
            )
            requirement.status = "À assigner"
            adapter = SqlAllocationCommandAdapter(session)

            allocation_id = adapter.create_manual(
                "SEG-NO-TARGET",
                "R2",
                D1,
                2,
                False,
            )

            self.assertIsNone(requirement.assigned_resource_id)
            self.assertEqual(requirement.status, "À assigner")
            manual = session.scalar(
                select(Shift).where(Shift.legacy_allocation_id == allocation_id)
            )
            self.assertIsNotNone(manual)
            assert manual is not None
            self.assertEqual(manual.resource_id, "R2")
            self.assertTrue(manual.locked)

    def test_manual_shift_create_release_and_total_guard(self) -> None:
        with transactional_session(self.factory) as session:
            requirement = self._add_requirement(
                session,
                identifier="SEG-MANUAL",
                hours=Decimal("8"),
            )
            adapter = SqlAllocationCommandAdapter(session)

            identifier = adapter.create_manual(
                "SEG-MANUAL",
                "Alice",
                D1,
                3,
                False,
                "Décision manuelle",
            )
            manual = session.scalar(
                select(Shift).where(Shift.legacy_allocation_id == identifier)
            )
            self.assertIsNotNone(manual)
            assert manual is not None
            self.assertTrue(manual.locked)
            self.assertEqual(manual.hours, Decimal("3.00"))
            self.assertEqual(manual.note, "Décision manuelle")

            shifts = session.scalars(
                select(Shift).where(Shift.resource_requirement_id == requirement.id)
            ).all()
            self.assertEqual(sum(float(row.hours) for row in shifts), 8.0)

            with self.assertRaises(ValueError):
                adapter.create_manual("SEG-MANUAL", "Alice", D1, 6, False)
            with self.assertRaises(ValueError):
                adapter.create_manual("SEG-MANUAL", "Alice", date(2026, 9, 1), 1, False)

            adapter.release_manual(identifier)
            self.assertIsNone(
                session.scalar(select(Shift).where(Shift.legacy_allocation_id == identifier))
            )
            rebuilt = session.scalars(
                select(Shift).where(Shift.resource_requirement_id == requirement.id)
            ).all()
            self.assertTrue(rebuilt)
            self.assertFalse(any(row.locked for row in rebuilt))
            self.assertEqual(sum(float(row.hours) for row in rebuilt), 8.0)

    def test_quick_shift_is_complete_without_fake_request(self) -> None:
        with transactional_session(self.factory) as session:
            service = QuickShiftService(
                SqlSegmentRepository(session),
                SqlAllocationCommandAdapter(session),
            )
            result = service.create(
                project_number="P-1",
                project_name="Projet SQL",
                technician="Alice",
                day_value=D1,
                hours_value=4,
                description="Intervention urgente",
            )

            requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.legacy_segment_id == result.segment_id
                )
            )
            self.assertIsNotNone(requirement)
            assert requirement is not None
            self.assertEqual(requirement.origin, ORIGIN_QUICK_SHIFT)
            self.assertIsNone(requirement.workforce_request_id)
            self.assertEqual(requirement.planned_hours, Decimal("4.00"))

            shift = session.scalar(
                select(Shift).where(Shift.legacy_allocation_id == result.allocation_id)
            )
            self.assertIsNotNone(shift)
            assert shift is not None
            self.assertTrue(shift.locked)
            self.assertEqual(shift.hours, Decimal("4.00"))
            self.assertEqual(
                int(session.scalar(select(func.count()).select_from(WorkforceRequest)) or 0),
                0,
            )

    def test_approved_sync_resizes_and_updates_requirements(self) -> None:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="D1",
                legacy_demand_number="DMO-2026-0001",
                project_id="P1",
                proposed_resource_id="R1",
                desired_start=D1,
                desired_end=D2,
                estimated_hours=Decimal("16"),
                resource_count=2,
                status="En planification",
                description="Programmation",
                required_competencies="PLC",
                priority="Élevée",
                approved_by_name="Jean",
            )
            session.add(request)
            session.flush()

            adapter = SqlApprovedDemandSyncAdapter(session)
            adapter.sync_approved("DMO-2026-0001")

            active = session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.workforce_request_id == request.id,
                    ResourceRequirement.status != "Annulé",
                )
            ).all()
            self.assertEqual(len(active), 2)
            self.assertEqual(
                sorted(row.planned_hours for row in active),
                [Decimal("8.00"), Decimal("8.00")],
            )
            self.assertEqual(sum(1 for row in active if row.assigned_resource_id == "R1"), 1)

            request.resource_count = 1
            request.estimated_hours = Decimal("10")
            request.desired_end = date(2026, 8, 26)
            adapter.sync_approved("DMO-2026-0001")

            all_requirements = session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.workforce_request_id == request.id
                )
            ).all()
            active = [row for row in all_requirements if row.status != "Annulé"]
            cancelled = [row for row in all_requirements if row.status == "Annulé"]
            self.assertEqual(len(active), 1)
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(active[0].assigned_resource_id, "R1")
            self.assertEqual(active[0].planned_hours, Decimal("10.00"))
            self.assertEqual(active[0].end_date, date(2026, 8, 26))

            history_count = session.scalar(
                select(func.count()).select_from(WorkforceRequestHistory)
            )
            self.assertEqual(int(history_count or 0), 2)

    def test_application_services_execute_on_real_sql_adapters(self) -> None:
        with transactional_session(self.factory) as session:
            planning_adapter = SqlPlanningCommandAdapter(session)
            allocation_adapter = SqlAllocationCommandAdapter(
                session,
                planning=planning_adapter,
            )
            demand_service = DemandService(
                SqlDemandRepository(session, actor_name="Jean"),
                planning_adapter,
                SqlApprovedDemandSyncAdapter(session),
                current_user="Jean",
            )
            planning_service = PlanningService(planning_adapter)
            allocation_service = AllocationService(allocation_adapter)

            number = demand_service.create(
                {
                    "NumeroProjet": "P-1",
                    "DateDebutSouhaitee": D1,
                    "DateFinSouhaitee": D1,
                    "Description": "Besoin SQL complet",
                    "NombreRessources": 1,
                    "TempsEstimeHeures": 8,
                    "TechnicienPropose": "Alice",
                },
                submit=True,
            )
            summary = demand_service.approve(number, "OK")
            self.assertEqual(summary["planning_engine"], "pure")
            self.assertEqual(summary["allocated_hours"], 8.0)

            requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.workforce_request_id.is_not(None),
                    ResourceRequirement.status != "Annulé",
                )
            )
            self.assertIsNotNone(requirement)
            assert requirement is not None
            segment_id = requirement.legacy_segment_id or requirement.id

            rebuilt = planning_service.rebuild()
            self.assertEqual(rebuilt["allocated_hours"], 8.0)

            allocation_id = allocation_service.create_manual(
                segment_id,
                "Alice",
                D1,
                2,
                False,
                "Priorité manuelle",
            )
            locked = session.scalar(
                select(Shift).where(Shift.legacy_allocation_id == allocation_id)
            )
            self.assertIsNotNone(locked)
            assert locked is not None
            self.assertTrue(locked.locked)
            self.assertEqual(locked.hours, Decimal("2.00"))

            assigned = allocation_service.assign_segment(segment_id, "Alice")
            self.assertEqual(assigned["planning_engine"], "pure")

    def test_failed_command_rolls_back_its_flushed_mutations(self) -> None:
        with transactional_session(self.factory) as session:
            self._add_requirement(
                session,
                identifier="SEG-FAIL",
                hours=Decimal("8"),
                resource_id="R1",
            )

        with self.assertRaises(RuntimeError):
            with transactional_session(self.factory) as session:
                adapter = SqlAllocationCommandAdapter(
                    session,
                    planning=_FailingPlanning(),
                )
                adapter.create_manual("SEG-FAIL", "Alice", D1, 2, False)

        with self.factory() as session:
            requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.legacy_segment_id == "SEG-FAIL"
                )
            )
            self.assertIsNotNone(requirement)
            assert requirement is not None
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count())
                        .select_from(Shift)
                        .where(Shift.resource_requirement_id == requirement.id)
                    )
                    or 0
                ),
                0,
            )

    def test_transaction_rolls_back_complete_quick_shift(self) -> None:
        with self.assertRaises(RuntimeError):
            with transactional_session(self.factory) as session:
                service = QuickShiftService(
                    SqlSegmentRepository(session),
                    SqlAllocationCommandAdapter(session),
                )
                service.create(
                    project_number="P-1",
                    technician="Alice",
                    day_value=D1,
                    hours_value=4,
                )
                raise RuntimeError("force rollback")

        with self.factory() as session:
            quick_requirements = session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.origin == ORIGIN_QUICK_SHIFT,
                    ResourceRequirement.legacy_segment_id != "SEG-FAIL",
                )
            ).all()
            self.assertEqual(quick_requirements, [])

    def test_sql_command_adapters_are_transaction_neutral_and_legacy_free(self) -> None:
        source = COMMAND_ADAPTERS.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")

        self.assertNotIn(".commit(", source)
        self.assertNotIn(".rollback(", source)
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
                f"SQL command adapter leaks legacy dependency: {forbidden}",
            )


if __name__ == "__main__":
    unittest.main()
