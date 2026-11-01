from __future__ import annotations

from functools import partial

from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import (
    Base,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.server import create_api_app as _create_api_app


D1 = date(2026, 9, 7)
D2 = date(2026, 9, 8)


from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import (
    TEST_ADMIN_AUTH_RESOLVER,
    TEST_PROJECT_MANAGER_AUTH_RESOLVER,
)
from tests.sqlite_test_template import SqliteDatabaseTemplate

create_api_app = partial(_create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class ServerDemandPeriodRouteTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet périodes"))
        session.add_all(
            [
                Resource(id="R1", name="Alice", active=True),
                Resource(id="R2", name="Bob", active=True),
            ]
        )
        session.flush()
        for resource_id, rule_id in (("R1", "STD-1"), ("R2", "STD-2")):
            session.add(
                ResourceAvailabilityRule(
                    id=rule_id,
                    resource_id=resource_id,
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                )
            )
        seed_test_approval_routing(session, map_existing_tasks=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="period-api.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _with_request_version(
        client: TestClient,
        number: str,
        payload: dict,
    ) -> dict:
        demand = client.get(f"/api/v1/demands/{number}")
        assert demand.status_code == 200, demand.text
        return {
            **payload,
            "expected_request_version": demand.json()["version"],
        }

    @staticmethod
    def _alternatives(*, second_hours: float = 8) -> dict:
        return {
            "periods": [
                {
                    "period_id": "OPT-A",
                    "start_date": D1.isoformat(),
                    "end_date": D1.isoformat(),
                    "hours": 8,
                    "kind": "ALTERNATIVE",
                    "alternative_group": "VISITE",
                    "proposed_resource": "Alice",
                },
                {
                    "period_id": "OPT-B",
                    "start_date": D2.isoformat(),
                    "end_date": D2.isoformat(),
                    "hours": second_hours,
                    "kind": "ALTERNATIVE",
                    "alternative_group": "VISITE",
                    "proposed_resource": "Bob",
                },
            ]
        }

    def test_selection_inside_approved_envelope_switches_plan_without_reapproval(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, actor_name="coord-test-user")
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": D1.isoformat(),
                        "desired_end": D2.isoformat(),
                        "estimated_hours": 8,
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]

                replaced = client.put(
                    f"/api/v1/demands/{number}/periods",
                    json=self._with_request_version(
                        client,
                        number,
                        self._alternatives(),
                    ),
                )
                self.assertEqual(replaced.status_code, 200, replaced.text)
                self.assertEqual(replaced.json()["period_count"], 2)
                self.assertFalse(replaced.json()["reapproval_required"])

                periods = client.get(f"/api/v1/demands/{number}/periods")
                self.assertEqual(periods.status_code, 200, periods.text)
                self.assertEqual([row["period_id"] for row in periods.json()], ["OPT-A", "OPT-B"])
                self.assertFalse(any(row["selected"] for row in periods.json()))

                selected = client.put(
                    f"/api/v1/demands/{number}/alternative-groups/VISITE/selection",
                    json={"period_id": "OPT-A"},
                )
                self.assertEqual(selected.status_code, 200, selected.text)
                self.assertIsNone(selected.json()["planning"])

                self.assertEqual(client.post(f"/api/v1/demands/{number}/submit").status_code, 200)
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "enveloppe approuvée"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

                switched = client.put(
                    f"/api/v1/demands/{number}/alternative-groups/VISITE/selection",
                    json={"period_id": "OPT-B"},
                )
                self.assertEqual(switched.status_code, 200, switched.text)
                self.assertIsNotNone(switched.json()["planning"])

                demand = client.get(f"/api/v1/demands/{number}")
                self.assertEqual(demand.json()["status"], "En planification")
                current_periods = client.get(f"/api/v1/demands/{number}/periods").json()
                self.assertEqual(
                    [row["period_id"] for row in current_periods if row["selected"]],
                    ["OPT-B"],
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    active = session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                    ).all()
                    self.assertEqual(len(active), 1)
                    self.assertEqual(active[0].start_date, D2)
                    self.assertIsNone(active[0].assigned_resource_id)
            finally:
                engine.dispose()

    def test_editing_approved_period_envelope_requires_reapproval_and_preserves_old_plan(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, actor_name="coord-test-user")
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": D1.isoformat(),
                        "desired_end": D2.isoformat(),
                        "estimated_hours": 8,
                    }),
                )
                number = created.json()["demand_number"]
                self.assertEqual(
                    client.put(
                        f"/api/v1/demands/{number}/periods",
                        json=self._with_request_version(
                            client,
                            number,
                            self._alternatives(),
                        ),
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    client.put(
                        f"/api/v1/demands/{number}/alternative-groups/VISITE/selection",
                        json={"period_id": "OPT-B"},
                    ).status_code,
                    200,
                )
                client.post(f"/api/v1/demands/{number}/submit")
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "ok"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

                pm_app = _create_api_app(
                    database_url,
                    actor_name="pm-period-test",
                    auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
                )
                with TestClient(pm_app, raise_server_exceptions=False) as pm_client:
                    changed = pm_client.put(
                        f"/api/v1/demands/{number}/periods",
                        json=self._with_request_version(
                            pm_client,
                            number,
                            self._alternatives(second_hours=10),
                        ),
                    )
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertTrue(changed.json()["reapproval_required"])
                self.assertEqual(changed.json()["status"], "Soumise")
                self.assertEqual(
                    client.get(f"/api/v1/demands/{number}").json()["status"],
                    "Soumise",
                )
                self.assertFalse(
                    any(
                        row["selected"]
                        for row in client.get(
                            f"/api/v1/demands/{number}/periods"
                        ).json()
                    )
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    active = session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                    ).all()
                    self.assertEqual(len(active), 1)
                    self.assertEqual(active[0].start_date, D2)
                    self.assertEqual(float(active[0].planned_hours), 8.0)
            finally:
                engine.dispose()

    def test_period_replace_rejects_stale_request_version(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, actor_name="coord-version-test")
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload({
                        "project_number": "P-1",
                        "desired_start": D1.isoformat(),
                        "desired_end": D2.isoformat(),
                        "estimated_hours": 8,
                    }),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                initial = client.get(f"/api/v1/demands/{number}")
                self.assertEqual(initial.status_code, 200, initial.text)
                version = initial.json()["version"]

                first = client.put(
                    f"/api/v1/demands/{number}/periods",
                    json={
                        **self._alternatives(),
                        "expected_request_version": version,
                    },
                )
                self.assertEqual(first.status_code, 200, first.text)

                stale = client.put(
                    f"/api/v1/demands/{number}/periods",
                    json={
                        **self._alternatives(second_hours=10),
                        "expected_request_version": version,
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["error"]["code"],
                    "demand_version_conflict",
                )
                rows = client.get(f"/api/v1/demands/{number}/periods")
                self.assertEqual(rows.status_code, 200, rows.text)
                self.assertEqual(
                    next(
                        row["hours"]
                        for row in rows.json()
                        if row["period_id"] == "OPT-B"
                    ),
                    8,
                )

    def test_unknown_demand_period_read_uses_structured_not_found(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/v1/demands/UNKNOWN/periods")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "demand_not_found")


if __name__ == "__main__":
    unittest.main()
