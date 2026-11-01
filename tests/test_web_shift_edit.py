from __future__ import annotations

from functools import partial

from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.infrastructure.sql import (
    Base,
    Project,
    Resource,
    ResourceAvailabilityRule,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.server import create_api_app


DAY = date(2026, 8, 24)


from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class WebShiftEditApiTests(unittest.TestCase):
    def _database(self, directory: str) -> str:
        path = Path(directory) / "web_shift_edit.db"
        url = f"sqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with transactional_session(factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet Web"))
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
            seed_test_approval_routing(session, map_existing_tasks=True)
        engine.dispose()
        return url

    @staticmethod
    def _approved_tentative_segment(client: TestClient) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload({
                "project_number": "P-1",
                "desired_start": DAY.isoformat(),
                "desired_end": DAY.isoformat(),
                "estimated_hours": 4,
                "proposed_technician": "Alice",
                "confirmation": "Tentative",
            }),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        assert client.post(f"/api/v1/demands/{number}/submit").status_code == 200
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "ok"},
        )
        assert approved.status_code == 200, approved.text
        actions = client.get(
            "/api/v1/planning/actions",
            params={"start": DAY.isoformat(), "end": DAY.isoformat(), "scope": "global"},
        )
        assert actions.status_code == 200, actions.text
        assignment = next(
            row for row in actions.json()
            if row["kind"] == "ASSIGNMENT" and row["demand_number"] == number
        )
        assigned = client.post(
            f"/api/v1/segments/{assignment['segment_id']}/assign",
            json={"resource_id": "R1"},
        )
        assert assigned.status_code == 200, assigned.text
        return str(assignment["segment_id"])

    def test_updating_auto_shift_turns_it_into_locked_manual_decision(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="web")
            with TestClient(app, raise_server_exceptions=False) as client:
                segment_id = self._approved_tentative_segment(client)
                auto_shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["segment_id"] == segment_id
                )
                self.assertEqual(auto_shift["source"], "AUTO")
                self.assertFalse(auto_shift["locked"])
                self.assertEqual(auto_shift["confirmation"], "Tentative")

                updated = client.put(
                    f"/api/v1/allocations/{auto_shift['allocation_id']}",
                    json={
                        "resource_id": "R1",
                        "day": DAY.isoformat(),
                        "hours": 4,
                        "outside_standard_hours": False,
                        "note": "Décision Web",
                        "confirmation": "Confirmée",
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)

                shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["allocation_id"] == auto_shift["allocation_id"]
                )
                self.assertEqual(shift["source"], "MANUAL")
                self.assertTrue(shift["locked"])
                self.assertEqual(shift["confirmation"], "Confirmée")
                self.assertEqual(shift["confirmation_override"], "Confirmée")
                self.assertEqual(shift["note"], "Décision Web")

    def test_put_null_confirmation_clears_shift_override_and_restores_inheritance(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="web")
            with TestClient(app, raise_server_exceptions=False) as client:
                segment_id = self._approved_tentative_segment(client)
                auto_shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["segment_id"] == segment_id
                )

                explicit = client.put(
                    f"/api/v1/allocations/{auto_shift['allocation_id']}",
                    json={
                        "technician": "Alice",
                        "day": DAY.isoformat(),
                        "hours": 4,
                        "outside_standard_hours": False,
                        "note": "",
                        "confirmation": "Confirmée",
                    },
                )
                self.assertEqual(explicit.status_code, 200, explicit.text)

                inherited = client.put(
                    f"/api/v1/allocations/{auto_shift['allocation_id']}",
                    json={
                        "technician": "Alice",
                        "day": DAY.isoformat(),
                        "hours": 4,
                        "outside_standard_hours": False,
                        "note": "",
                        "confirmation": None,
                    },
                )
                self.assertEqual(inherited.status_code, 200, inherited.text)

                shift = next(
                    row
                    for row in client.get("/api/v1/shifts").json()
                    if row["allocation_id"] == auto_shift["allocation_id"]
                )
                self.assertTrue(shift["locked"])
                self.assertIsNone(shift["confirmation_override"])
                self.assertEqual(shift["confirmation"], "Tentative")


if __name__ == "__main__":
    unittest.main()
