from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.security import ROLE_PROJECT_MANAGER, AuthPrincipal
from app.infrastructure.sql import (
    Base,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.sqlite_test_template import SqliteDatabaseTemplate


DAY = date(2026, 9, 21)


class ContextualPlanningScopeTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add_all(
            [
                Project(
                    id="P-MINE",
                    number="P-100",
                    name="Projet du chargé",
                    project_manager_external_id="EMP-PM",
                    project_manager_name="Chargé scope",
                    status="Actif",
                ),
                Project(
                    id="P-OUT",
                    number="P-900",
                    name="Projet hors périmètre",
                    status="Actif",
                ),
                Resource(
                    id="R-ALICE",
                    external_id="EMP-ALICE",
                    name="Alice",
                    resource_class="Programmation",
                    active=True,
                    sort_order=10,
                ),
            ]
        )
        session.flush()
        session.add(
            ResourceAvailabilityRule(
                id="SCH-ALICE",
                resource_id="R-ALICE",
                availability_type="Horaire standard",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
                weekdays="Lun,Mar,Mer,Jeu,Ven",
                start_time=time(7, 0),
                end_time=time(15, 0),
                active=True,
            )
        )
        session.add_all(
            [
                ResourceRequirement(
                    id="REQ-MINE",
                    legacy_segment_id="SEG-MINE",
                    project_id="P-MINE",
                    assigned_resource_id="R-ALICE",
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("8"),
                    status="Planifié",
                    confirmation="Confirmée",
                    origin="AD_HOC",
                ),
                ResourceRequirement(
                    id="REQ-MINE-OPEN",
                    legacy_segment_id="SEG-MINE-OPEN",
                    project_id="P-MINE",
                    assigned_resource_id=None,
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("4"),
                    status="À assigner",
                    confirmation="Confirmée",
                    origin="AD_HOC",
                ),
                ResourceRequirement(
                    id="REQ-OUT",
                    legacy_segment_id="SEG-OUT",
                    project_id="P-OUT",
                    assigned_resource_id="R-ALICE",
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("8"),
                    status="Planifié",
                    confirmation="Confirmée",
                    origin="AD_HOC",
                ),
                ResourceRequirement(
                    id="REQ-OUT-OPEN",
                    legacy_segment_id="SEG-OUT-OPEN",
                    project_id="P-OUT",
                    assigned_resource_id=None,
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("4"),
                    status="À assigner",
                    confirmation="Confirmée",
                    origin="AD_HOC",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Shift(
                    id="SHIFT-MINE",
                    resource_requirement_id="REQ-MINE",
                    resource_id="R-ALICE",
                    work_date=DAY,
                    hours=Decimal("2"),
                    source="AUTO",
                    locked=False,
                    outside_standard_hours=False,
                ),
                Shift(
                    id="SHIFT-OUT",
                    resource_requirement_id="REQ-OUT",
                    resource_id="R-ALICE",
                    work_date=DAY,
                    hours=Decimal("3"),
                    source="AUTO",
                    locked=False,
                    outside_standard_hours=False,
                ),
            ]
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="planning-scope.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_url = self._database_template.copy_to(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def principal(employee_external_id: str) -> AuthPrincipal:
        return AuthPrincipal.from_roles(
            local_user_id="U-PM",
            issuer="urn:test",
            subject=f"pm-{employee_external_id}",
            display_name="Chargé scope",
            email=None,
            employee_external_id=employee_external_id,
            roles=(ROLE_PROJECT_MANAGER,),
            auth_mode="local",
        )

    def client_for(self, employee_external_id: str) -> TestClient:
        return TestClient(
            create_api_app(
                self.database_url,
                auth_resolver=static_auth_resolver(
                    self.principal(employee_external_id)
                ),
            )
        )

    def test_mine_filters_planning_snapshot_actions_and_medium_term_rows(self) -> None:
        params = {
            "start": DAY.isoformat(),
            "end": DAY.isoformat(),
            "scope": "mine",
        }
        with self.client_for("EMP-PM") as client:
            snapshot = client.get("/api/v1/planning/snapshot", params=params)
            actions = client.get("/api/v1/planning/actions", params=params)
            unlinked = client.get(
                "/api/v1/medium-term/unlinked-segments",
                params=params,
            )
            segments = client.get("/api/v1/segments", params=params)
            shifts = client.get("/api/v1/shifts", params=params)

        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        payload = snapshot.json()
        self.assertEqual(
            {row["project_number"] for row in payload["segments"]},
            {"P-100"},
        )
        self.assertEqual(
            {row["project_number"] for row in payload["shifts"]},
            {"P-100"},
        )
        self.assertEqual(payload["firm_hours"], 2.0)
        self.assertEqual([row["name"] for row in payload["resources"]], ["Alice"])

        self.assertEqual(actions.status_code, 200, actions.text)
        self.assertEqual(
            {row["project_number"] for row in actions.json()},
            {"P-100"},
        )
        self.assertEqual(
            {row["project_number"] for row in unlinked.json()},
            {"P-100"},
        )
        self.assertEqual(
            {row["project_number"] for row in segments.json()},
            {"P-100"},
        )
        self.assertEqual(
            {row["project_number"] for row in shifts.json()},
            {"P-100"},
        )

    def test_project_manager_defaults_to_mine_and_global_is_forbidden(self) -> None:
        window = {
            "start": DAY.isoformat(),
            "end": DAY.isoformat(),
        }
        with self.client_for("EMP-PM") as client:
            default_scope = client.get(
                "/api/v1/planning/snapshot",
                params=window,
            )
            forbidden_global = client.get(
                "/api/v1/planning/snapshot",
                params={**window, "scope": "global"},
            )

        self.assertEqual(default_scope.status_code, 200, default_scope.text)
        self.assertEqual(
            {row["project_number"] for row in default_scope.json()["shifts"]},
            {"P-100"},
        )
        self.assertEqual(forbidden_global.status_code, 403, forbidden_global.text)
        self.assertEqual(
            forbidden_global.json()["error"]["code"],
            "planning_scope_not_authorized",
        )

    def test_hidden_segment_detail_does_not_leak_outside_project_scope(self) -> None:
        with self.client_for("EMP-PM") as client:
            visible = client.get(
                "/api/v1/segments/SEG-MINE",
                params={"scope": "mine"},
            )
            hidden = client.get(
                "/api/v1/segments/SEG-OUT",
                params={"scope": "mine"},
            )

        self.assertEqual(visible.status_code, 200, visible.text)
        self.assertEqual(visible.json()["project_number"], "P-100")
        self.assertEqual(hidden.status_code, 404, hidden.text)
        self.assertEqual(hidden.json()["error"]["code"], "segment_not_found")

    def test_contextual_capacity_keeps_outside_project_occupation(self) -> None:
        params = {
            "start": DAY.isoformat(),
            "end": DAY.isoformat(),
            "scope": "mine",
        }
        with self.client_for("EMP-PM") as client:
            response = client.get("/api/v1/planning/capacity-grid", params=params)

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(len(payload["resources"]), 1)
        alice = payload["resources"][0]
        self.assertEqual(alice["resource_name"], "Alice")
        self.assertEqual(alice["capacity_hours"], 8.0)
        # 2 h in P-100 + 3 h in P-900. The second shift is hidden from the
        # contextual board but still consumes real capacity.
        self.assertEqual(alice["confirmed_hours"], 5.0)
        self.assertEqual(alice["prudent_free"], 3.0)

        diagnostics = {
            row["segment_id"]: row for row in payload["segment_diagnostics"]
        }
        self.assertEqual(set(diagnostics), {"SEG-MINE", "SEG-MINE-OPEN"})
        self.assertEqual(diagnostics["SEG-MINE"]["allocated_hours"], 2.0)
        self.assertEqual(diagnostics["SEG-MINE"]["unplaced_hours"], 6.0)
        self.assertEqual(diagnostics["SEG-MINE"]["automatic_target_resource_id"], "R-ALICE")

        unassigned = diagnostics["SEG-MINE-OPEN"]
        self.assertIsNone(unassigned["automatic_target_resource_id"])
        self.assertEqual(unassigned["allocated_hours"], 0.0)
        self.assertEqual(unassigned["unplaced_hours"], 4.0)

    def test_runtime_composition_supplies_global_medium_term_capacity_reference(self) -> None:
        params = {
            "start": DAY.isoformat(),
            "end": DAY.isoformat(),
            "scope": "mine",
        }
        with self.client_for("EMP-PM") as client:
            response = client.get("/api/v1/planning/snapshot", params=params)

        self.assertEqual(response.status_code, 200, response.text)
        buckets = response.json()["capacity_buckets"]
        total = next(row for row in buckets if row["resource_class"] is None)
        self.assertEqual(total["capacity_hours"], 8.0)
        self.assertEqual(total["firm_hours"], 5.0)
        self.assertEqual(total["exposure_hours"], 5.0)

    def test_empty_mine_never_falls_back_to_global_planning(self) -> None:
        params = {
            "start": DAY.isoformat(),
            "end": DAY.isoformat(),
            "scope": "mine",
        }
        with self.client_for("EMP-UNKNOWN") as client:
            snapshot = client.get("/api/v1/planning/snapshot", params=params)
            capacity = client.get("/api/v1/planning/capacity-grid", params=params)
            actions = client.get("/api/v1/planning/actions", params=params)

        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        payload = snapshot.json()
        self.assertEqual(payload["demands"], [])
        self.assertEqual(payload["segments"], [])
        self.assertEqual(payload["shifts"], [])
        self.assertEqual(payload["resources"], [])
        self.assertEqual(payload["firm_hours"], 0.0)
        self.assertEqual(payload["potential_hours"], 0.0)
        # The medium-term capacity reference is intentionally global and explicitly
        # labelled as such in React, even when the contextual project result is empty.
        self.assertTrue(payload["capacity_buckets"])

        self.assertEqual(capacity.status_code, 200, capacity.text)
        self.assertEqual(capacity.json()["resources"], [])
        self.assertEqual(capacity.json()["segment_diagnostics"], [])
        self.assertEqual(actions.json(), [])


if __name__ == "__main__":
    unittest.main()
