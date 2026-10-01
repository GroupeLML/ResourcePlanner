from __future__ import annotations

from functools import partial
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.infrastructure.sql import (
    Base,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    SqlPlanningMutationVersionRepository,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class PlanningOutsideStandardHoursMoveTests(unittest.TestCase):
    @staticmethod
    def _database(directory: str) -> str:
        path = Path(directory) / "planning-outside-standard-hours-536.db"
        url = f"sqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(Project(id="P-536", number="P-536", name="Projet #536"))
            resources = [
                Resource(id="R-A-536", name="Alice 536", active=True, sort_order=10),
                Resource(id="R-B-536", name="Bob 536", active=True, sort_order=20),
                Resource(id="R-C-536", name="Carol 536", active=True, sort_order=30),
            ]
            session.add_all(resources)
            session.flush()
            for resource in resources:
                session.add(
                    ResourceAvailabilityRule(
                        id=f"STD-{resource.id}",
                        resource_id=resource.id,
                        availability_type="Horaire standard",
                        start_date=date(2026, 1, 1),
                        end_date=date(2026, 12, 31),
                        weekdays="Lun,Mar,Mer,Jeu,Ven",
                        start_time=time(7, 0),
                        end_time=time(15, 0),
                        active=True,
                    )
                )
            session.add(
                ResourceAvailabilityRule(
                    id="VAC-B-536",
                    resource_id="R-B-536",
                    availability_type="Vacances",
                    start_date=date(2026, 9, 23),
                    end_date=date(2026, 9, 23),
                    active=True,
                )
            )
            session.add(
                ResourceAvailabilityRule(
                    id="HOL-B-536",
                    resource_id="R-B-536",
                    availability_type="Jour férié",
                    start_date=date(2026, 9, 24),
                    end_date=date(2026, 9, 24),
                    active=True,
                )
            )
            requirement = ResourceRequirement(
                id="REQ-536",
                legacy_segment_id="SEG-536",
                project_id="P-536",
                assigned_resource_id="R-A-536",
                start_date=date(2026, 9, 21),
                end_date=date(2026, 9, 27),
                planned_hours=Decimal("16"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            )
            session.add(requirement)
            session.flush()
            session.add(
                Shift(
                    id="SHIFT-536",
                    resource_requirement_id=requirement.id,
                    resource_id="R-A-536",
                    work_date=date(2026, 9, 21),
                    hours=Decimal("8"),
                    allocation_type="Flexible",
                    source="AUTO",
                    locked=False,
                    outside_standard_hours=False,
                    note="Régression #536",
                )
            )
        engine.dispose()
        return url

    def test_weekend_preview_requires_consent_then_move_persists_override(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Coordonnateur #536")
            with TestClient(app) as client:
                preview = client.post(
                    "/api/v1/allocations/SHIFT-536/evaluate-drop",
                    json={
                        "resource_id": "R-C-536",
                        "day": "2026-09-26",
                        "outside_standard_hours": False,
                    },
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertIn(
                    "OUTSIDE_STANDARD_HOURS_REQUIRED",
                    {row["code"] for row in preview.json()["warnings"]},
                )
                blocked = client.post(
                    "/api/v1/allocations/SHIFT-536/move",
                    json={
                        "resource_id": "R-C-536",
                        "day": "2026-09-26",
                        "outside_standard_hours": False,
                        "expected_planning_version": preview.json()["planning_version"],
                    },
                )
                self.assertEqual(blocked.status_code, 422, blocked.text)
                self.assertIn("horaire standard", blocked.json()["error"]["message"])

                reevaluated = client.post(
                    "/api/v1/allocations/SHIFT-536/evaluate-drop",
                    json={
                        "resource_id": "R-C-536",
                        "day": "2026-09-26",
                        "outside_standard_hours": True,
                    },
                )
                self.assertEqual(reevaluated.status_code, 200, reevaluated.text)
                self.assertNotIn(
                    "OUTSIDE_STANDARD_HOURS_REQUIRED",
                    {row["code"] for row in reevaluated.json()["warnings"]},
                )
                moved = client.post(
                    "/api/v1/allocations/SHIFT-536/move",
                    json={
                        "resource_id": "R-C-536",
                        "day": "2026-09-26",
                        "outside_standard_hours": True,
                        "expected_planning_version": reevaluated.json()["planning_version"],
                    },
                )
                self.assertEqual(moved.status_code, 200, moved.text)
                shifts = client.get(
                    "/api/v1/shifts",
                    params={"start": "2026-09-21", "end": "2026-09-27"},
                ).json()
                history = client.get("/api/v1/shifts/SHIFT-536/history").json()

            persisted = next(row for row in shifts if row["allocation_id"] == "SHIFT-536")
            self.assertEqual(persisted["resource_id"], "R-C-536")
            self.assertEqual(persisted["work_date"], "2026-09-26")
            self.assertTrue(persisted["outside_standard_hours"])
            self.assertTrue(persisted["locked"])
            self.assertTrue(any(row["action"] == "Déplacement quart" for row in history))

    def test_holiday_is_overridable_but_vacation_is_not(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                holiday = client.post(
                    "/api/v1/allocations/SHIFT-536/evaluate-drop",
                    json={
                        "resource_id": "R-B-536",
                        "day": "2026-09-24",
                        "outside_standard_hours": True,
                    },
                )
                self.assertEqual(holiday.status_code, 200, holiday.text)
                holiday_move = client.post(
                    "/api/v1/allocations/SHIFT-536/move",
                    json={
                        "resource_id": "R-B-536",
                        "day": "2026-09-24",
                        "outside_standard_hours": True,
                        "expected_planning_version": holiday.json()["planning_version"],
                    },
                )
                self.assertEqual(holiday_move.status_code, 200, holiday_move.text)

        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                vacation = client.post(
                    "/api/v1/allocations/SHIFT-536/evaluate-drop",
                    json={
                        "resource_id": "R-B-536",
                        "day": "2026-09-23",
                        "outside_standard_hours": True,
                    },
                )
                self.assertEqual(vacation.status_code, 422, vacation.text)
                self.assertIn("Vacances", vacation.json()["error"]["message"])
                blocked_move = client.post(
                    "/api/v1/allocations/SHIFT-536/move",
                    json={
                        "resource_id": "R-B-536",
                        "day": "2026-09-23",
                        "outside_standard_hours": True,
                    },
                )
                self.assertEqual(blocked_move.status_code, 422, blocked_move.text)
                self.assertIn("Vacances", blocked_move.json()["error"]["message"])

    def test_normal_day_does_not_persist_unused_override(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/allocations/SHIFT-536/move",
                    json={
                        "resource_id": "R-B-536",
                        "day": "2026-09-22",
                        "outside_standard_hours": True,
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                shifts = client.get(
                    "/api/v1/shifts",
                    params={"start": "2026-09-21", "end": "2026-09-27"},
                ).json()

            persisted = next(row for row in shifts if row["allocation_id"] == "SHIFT-536")
            self.assertFalse(persisted["outside_standard_hours"])

    def test_stale_preview_version_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app) as client:
                preview = client.post(
                    "/api/v1/allocations/SHIFT-536/evaluate-drop",
                    json={"resource_id": "R-B-536", "day": "2026-09-22"},
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                stale_version = preview.json()["planning_version"]

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                with factory.begin() as session:
                    SqlPlanningMutationVersionRepository(session).acquire(
                        expected_version=stale_version
                    )
                engine.dispose()

                response = client.post(
                    "/api/v1/allocations/SHIFT-536/move",
                    json={
                        "resource_id": "R-B-536",
                        "day": "2026-09-22",
                        "outside_standard_hours": False,
                        "expected_planning_version": stale_version,
                    },
                )

            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "planning_version_conflict")


if __name__ == "__main__":
    unittest.main()
