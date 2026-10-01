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


DAY = date(2026, 9, 10)


from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER, test_admin_auth_resolver

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class QuickShiftAuthorHttpTests(unittest.TestCase):
    def test_quick_shift_actor_is_taken_from_server_context_not_request_body(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite:///{(Path(directory) / 'author.db').as_posix()}"
            engine = create_sql_engine(database_url)
            Base.metadata.create_all(engine)
            factory = create_session_factory(engine)
            with transactional_session(factory) as session:
                session.add(
                    Project(
                        id="P1",
                        number="P-1",
                        name="Projet A",
                        project_manager_name="Responsable A",
                        status="Actif",
                    )
                )
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
            engine.dispose()

            app = create_api_app(
                database_url,
                actor_name="Coordonnateur",
                auth_resolver=test_admin_auth_resolver("Coordonnateur"),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/quick-shifts",
                    json={
                        "project_number": "P-1",
                        "technician": "Alice",
                        "day": DAY.isoformat(),
                        "hours": 2,
                        "outside_standard_hours": True,
                        "description": "Intervention urgente",
                    },
                    headers={"Idempotency-Key": "quick-author-1"},
                )
                shifts = client.get(
                    f"/api/v1/shifts?start={DAY.isoformat()}&end={DAY.isoformat()}"
                )
                schema = client.get("/openapi.json").json()

            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(shifts.status_code, 200, shifts.text)
            self.assertEqual(shifts.json()[0]["requester"], "Coordonnateur")
            self.assertEqual(shifts.json()[0]["project_manager"], "Responsable A")
            self.assertIsNone(shifts.json()[0]["demand_number"])

            quick_schema = schema["components"]["schemas"]["QuickShiftRequest"]
            self.assertNotIn("requester", quick_schema.get("properties", {}))
            self.assertNotIn("creator", quick_schema.get("properties", {}))


if __name__ == "__main__":
    unittest.main()
