from __future__ import annotations

from datetime import date, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.infrastructure.sql import (
    Base,
    ORIGIN_QUICK_SHIFT,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


MONDAY = date(2026, 10, 5)
FRIDAY = MONDAY + timedelta(days=4)


class QuickShiftWeeklyExtensionTests(unittest.TestCase):
    @staticmethod
    def _database(directory: str, *, confirmation: str) -> str:
        path = Path(directory) / f"issue-576-{confirmation}.db"
        url = f"sqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(Project(id="P1", number="P-576", name="Projet Quick Shift"))
            session.add(Resource(id="R1", name="Alice", active=True))
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
            session.add(
                ResourceRequirement(
                    id="REQ-QS",
                    legacy_segment_id="SEG-QS",
                    project_id="P1",
                    workforce_request_id=None,
                    assigned_resource_id="R1",
                    start_date=MONDAY,
                    end_date=MONDAY,
                    planned_hours=8,
                    status="Planifié",
                    planning_type="Flexible",
                    confirmation=confirmation,
                    confirmation_overridden=False,
                    origin=ORIGIN_QUICK_SHIFT,
                )
            )
            session.flush()
            session.add(
                Shift(
                    id="SHIFT-QS",
                    legacy_allocation_id="ALLOC-QS",
                    resource_requirement_id="REQ-QS",
                    resource_id="R1",
                    work_date=MONDAY,
                    hours=8,
                    allocation_type="Flexible",
                    source="MANUAL",
                    locked=True,
                    outside_standard_hours=False,
                    confirmation=confirmation,
                )
            )
        engine.dispose()
        return url

    @staticmethod
    def _read_state(url: str) -> tuple[ResourceRequirement, list[Shift], int]:
        engine = create_sql_engine(url)
        factory = create_session_factory(engine)
        with factory() as session:
            requirement = session.get(ResourceRequirement, "REQ-QS")
            assert requirement is not None
            session.expunge(requirement)
            shifts = list(
                session.scalars(
                    select(Shift)
                    .where(Shift.resource_requirement_id == "REQ-QS")
                    .order_by(Shift.work_date, Shift.id)
                ).all()
            )
            for shift in shifts:
                session.expunge(shift)
            request_count = int(
                session.scalar(select(func.count()).select_from(WorkforceRequest)) or 0
            )
        engine.dispose()
        return requirement, shifts, request_count

    def test_extend_week_with_eight_hours_preserves_locked_shift_and_zero_residual(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory, confirmation="Tentative")
            app = create_api_app(url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.patch(
                    "/api/v1/segments/SEG-QS",
                    json={"end_date": FRIDAY.isoformat()},
                )
            self.assertEqual(response.status_code, 200, response.text)

            requirement, shifts, request_count = self._read_state(url)
            self.assertEqual(requirement.start_date, MONDAY)
            self.assertEqual(requirement.end_date, FRIDAY)
            self.assertEqual(float(requirement.planned_hours), 8.0)
            self.assertEqual(requirement.confirmation, "Tentative")
            self.assertEqual(request_count, 0)
            self.assertEqual(len(shifts), 1)
            locked = shifts[0]
            self.assertEqual(locked.id, "SHIFT-QS")
            self.assertTrue(locked.locked)
            self.assertEqual(locked.source, "MANUAL")
            self.assertEqual(locked.work_date, MONDAY)
            self.assertEqual(float(locked.hours), 8.0)

    def test_extend_week_to_forty_hours_preserves_locked_shift_and_rebuilds_only_residual(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory, confirmation="Confirmée")
            app = create_api_app(url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.patch(
                    "/api/v1/segments/SEG-QS",
                    json={
                        "end_date": FRIDAY.isoformat(),
                        "planned_hours": 40,
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)

            requirement, shifts, request_count = self._read_state(url)
            self.assertEqual(requirement.end_date, FRIDAY)
            self.assertEqual(float(requirement.planned_hours), 40.0)
            self.assertEqual(request_count, 0)

            locked = [shift for shift in shifts if shift.locked]
            automatic = [
                shift
                for shift in shifts
                if not shift.locked and shift.allocation_type != "Hors horaire requis"
            ]
            self.assertEqual(len(locked), 1)
            self.assertEqual(locked[0].id, "SHIFT-QS")
            self.assertEqual(locked[0].work_date, MONDAY)
            self.assertEqual(float(locked[0].hours), 8.0)
            self.assertAlmostEqual(sum(float(shift.hours) for shift in automatic), 32.0)
            self.assertTrue(all(MONDAY <= shift.work_date <= FRIDAY for shift in automatic))

    def test_date_only_patch_preserves_confirmed_value_without_request(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory, confirmation="Confirmée")
            app = create_api_app(url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.patch(
                    "/api/v1/segments/SEG-QS",
                    json={"end_date": FRIDAY.isoformat()},
                )
            self.assertEqual(response.status_code, 200, response.text)

            requirement, shifts, request_count = self._read_state(url)
            self.assertEqual(requirement.confirmation, "Confirmée")
            self.assertEqual(request_count, 0)
            self.assertEqual(
                [(shift.id, shift.confirmation, shift.locked) for shift in shifts],
                [("SHIFT-QS", "Confirmée", True)],
            )


if __name__ == "__main__":
    unittest.main()
