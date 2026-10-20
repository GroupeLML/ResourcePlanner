from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal
import unittest

from sqlalchemy import select

from app.infrastructure.sql import (
    Base,
    ORIGIN_AD_HOC,
    ORIGIN_QUICK_SHIFT,
    ORIGIN_REQUEST,
    Project,
    QuickShiftPlanningExclusion,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    SqlAllocationCommandAdapter,
    SqlPlanningCommandAdapter,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


MONDAY = date(2026, 10, 19)
TUESDAY = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)
THURSDAY = MONDAY + timedelta(days=3)


class _FailingPlanning:
    def rebuild(self, *, preserve_shift_ids=()):
        raise RuntimeError("rebuild failure")


class QuickShiftDelete755Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-755", name="Projet 755"))
            session.add(Resource(id="R1", name="Alice", active=True, erp_active=True))
            session.flush()
            session.add(
                ResourceAvailabilityRule(
                    id="STD-R1",
                    resource_id="R1",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def _requirement(
        self,
        session,
        *,
        identifier: str,
        hours: str,
        origin: str = ORIGIN_QUICK_SHIFT,
        workforce_request_id: str | None = None,
    ) -> ResourceRequirement:
        row = ResourceRequirement(
            id=f"REQ-{identifier}",
            legacy_segment_id=identifier,
            project_id="P1",
            workforce_request_id=workforce_request_id,
            assigned_resource_id="R1",
            start_date=MONDAY,
            end_date=THURSDAY,
            planned_hours=Decimal(hours),
            status="Planifié",
            planning_type="Flexible",
            origin=origin,
        )
        session.add(row)
        session.flush()
        return row

    def _shift(
        self,
        session,
        requirement: ResourceRequirement,
        *,
        identifier: str,
        day: date,
        hours: str,
        locked: bool,
    ) -> Shift:
        row = Shift(
            id=f"S-{identifier}",
            legacy_allocation_id=identifier,
            resource_requirement_id=requirement.id,
            resource_id="R1",
            work_date=day,
            hours=Decimal(hours),
            allocation_type="Flexible",
            source="MANUAL" if locked else "AUTO",
            locked=locked,
            outside_standard_hours=False,
        )
        session.add(row)
        session.flush()
        return row

    def test_delete_middle_quick_shift_preserves_mixed_survivors_and_persists_exclusion(self) -> None:
        with transactional_session(self.factory) as session:
            requirement = self._requirement(session, identifier="SEG-QS-32", hours="32")
            monday = self._shift(session, requirement, identifier="MON", day=MONDAY, hours="8", locked=True)
            tuesday = self._shift(session, requirement, identifier="TUE", day=TUESDAY, hours="8", locked=False)
            self._shift(session, requirement, identifier="WED", day=WEDNESDAY, hours="8", locked=True)
            thursday = self._shift(session, requirement, identifier="THU", day=THURSDAY, hours="8", locked=False)

            SqlAllocationCommandAdapter(session).delete_manual("WED")

            self.assertEqual(requirement.planned_hours, Decimal("24.00"))
            self.assertEqual(requirement.start_date, MONDAY)
            self.assertEqual(requirement.end_date, THURSDAY)
            rows = list(
                session.scalars(
                    select(Shift)
                    .where(Shift.resource_requirement_id == requirement.id)
                    .order_by(Shift.work_date)
                ).all()
            )
            self.assertEqual(
                [(row.id, row.work_date, row.hours, row.locked, row.source) for row in rows],
                [
                    (monday.id, MONDAY, Decimal("8.00"), True, "MANUAL"),
                    (tuesday.id, TUESDAY, Decimal("8.00"), False, "AUTO"),
                    (thursday.id, THURSDAY, Decimal("8.00"), False, "AUTO"),
                ],
            )
            exclusion = session.get(
                QuickShiftPlanningExclusion,
                {"resource_requirement_id": requirement.id, "excluded_date": WEDNESDAY},
            )
            self.assertIsNotNone(exclusion)

            # A later unrelated/global rebuild may replace automatic rows, but the
            # persisted Quick Shift exclusion must keep Wednesday empty.
            SqlPlanningCommandAdapter(session).rebuild()
            rebuilt = list(
                session.scalars(
                    select(Shift)
                    .where(Shift.resource_requirement_id == requirement.id)
                    .order_by(Shift.work_date)
                ).all()
            )
            self.assertEqual([row.work_date for row in rebuilt], [MONDAY, TUESDAY, THURSDAY])
            self.assertEqual(sum((row.hours for row in rebuilt), Decimal("0")), Decimal("24.00"))
            self.assertEqual(
                [(row.work_date, row.locked, row.source) for row in rebuilt],
                [
                    (MONDAY, True, "MANUAL"),
                    (TUESDAY, False, "AUTO"),
                    (THURSDAY, False, "AUTO"),
                ],
            )

    def test_delete_uses_exact_six_hour_shift_value(self) -> None:
        with transactional_session(self.factory) as session:
            requirement = self._requirement(session, identifier="SEG-QS-30", hours="30")
            self._shift(session, requirement, identifier="MON6", day=MONDAY, hours="8", locked=True)
            self._shift(session, requirement, identifier="TUE6", day=TUESDAY, hours="8", locked=False)
            self._shift(session, requirement, identifier="WED6", day=WEDNESDAY, hours="6", locked=True)
            self._shift(session, requirement, identifier="THU6", day=THURSDAY, hours="8", locked=False)

            SqlAllocationCommandAdapter(session).delete_manual("WED6")

            self.assertEqual(requirement.planned_hours, Decimal("24.00"))
            self.assertEqual(
                sum(
                    session.scalars(
                        select(Shift.hours).where(Shift.resource_requirement_id == requirement.id)
                    ).all(),
                    Decimal("0"),
                ),
                Decimal("24.00"),
            )

    def test_final_quick_shift_cancels_parent_without_zero_hour_state(self) -> None:
        with transactional_session(self.factory) as session:
            requirement = self._requirement(session, identifier="SEG-QS-FINAL", hours="8")
            requirement.end_date = MONDAY
            self._shift(session, requirement, identifier="FINAL", day=MONDAY, hours="8", locked=True)

            SqlAllocationCommandAdapter(session).delete_manual("FINAL")

            self.assertEqual(requirement.status, "Annulé")
            self.assertEqual(requirement.planned_hours, Decimal("8.00"))
            self.assertEqual(
                list(
                    session.scalars(
                        select(Shift).where(Shift.resource_requirement_id == requirement.id)
                    ).all()
                ),
                [],
            )

    def test_request_and_generic_ad_hoc_keep_existing_delete_semantics(self) -> None:
        with transactional_session(self.factory) as session:
            request = WorkforceRequest(
                id="WR-755",
                legacy_demand_number="DMO-755",
                project_id="P1",
                desired_start=MONDAY,
                desired_end=MONDAY,
                status="En planification",
            )
            session.add(request)
            session.flush()
            request_requirement = self._requirement(
                session,
                identifier="SEG-REQ",
                hours="8",
                origin=ORIGIN_REQUEST,
                workforce_request_id=request.id,
            )
            request_requirement.end_date = MONDAY
            adhoc_requirement = self._requirement(
                session,
                identifier="SEG-ADHOC",
                hours="8",
                origin=ORIGIN_AD_HOC,
            )
            adhoc_requirement.end_date = MONDAY
            self._shift(session, request_requirement, identifier="REQ-S", day=MONDAY, hours="8", locked=True)
            self._shift(session, adhoc_requirement, identifier="ADHOC-S", day=MONDAY, hours="8", locked=True)

            adapter = SqlAllocationCommandAdapter(session)
            adapter.delete_manual("REQ-S")
            adapter.delete_manual("ADHOC-S")

            self.assertEqual(request_requirement.planned_hours, Decimal("8.00"))
            self.assertEqual(adhoc_requirement.planned_hours, Decimal("8.00"))
            self.assertEqual(request_requirement.status, "Planifié")
            self.assertEqual(adhoc_requirement.status, "Planifié")
            self.assertIsNone(
                session.get(
                    QuickShiftPlanningExclusion,
                    {"resource_requirement_id": request_requirement.id, "excluded_date": MONDAY},
                )
            )
            self.assertIsNone(
                session.get(
                    QuickShiftPlanningExclusion,
                    {"resource_requirement_id": adhoc_requirement.id, "excluded_date": MONDAY},
                )
            )

    def test_failed_rebuild_rolls_back_hours_shift_and_exclusion_together(self) -> None:
        with transactional_session(self.factory) as session:
            requirement = self._requirement(session, identifier="SEG-QS-ROLLBACK", hours="16")
            self._shift(session, requirement, identifier="KEEP", day=MONDAY, hours="8", locked=True)
            self._shift(session, requirement, identifier="DELETE", day=TUESDAY, hours="8", locked=True)

        with self.assertRaises(RuntimeError):
            with transactional_session(self.factory) as session:
                SqlAllocationCommandAdapter(
                    session,
                    planning=_FailingPlanning(),
                ).delete_manual("DELETE")

        with self.factory() as session:
            requirement = session.get(ResourceRequirement, "REQ-SEG-QS-ROLLBACK")
            assert requirement is not None
            self.assertEqual(requirement.planned_hours, Decimal("16.00"))
            self.assertIsNotNone(session.get(Shift, "S-DELETE"))
            self.assertIsNone(
                session.get(
                    QuickShiftPlanningExclusion,
                    {"resource_requirement_id": requirement.id, "excluded_date": TUESDAY},
                )
            )


if __name__ == "__main__":
    unittest.main()
