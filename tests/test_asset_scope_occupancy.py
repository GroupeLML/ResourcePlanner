from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.security import AuthPrincipal, ROLE_PROJECT_MANAGER
from app.infrastructure.sql import (
    Base,
    Project,
    Resource,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.asset_models import AssetAllocation, AssetRequirement
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.approval_test_support import (
    map_asset_type_to_test_approval_scope,
    routed_demand_payload,
    seed_test_approval_routing,
)
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


DAY = "2026-09-24"


class AssetScopeOccupancyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.url = (
            "sqlite+pysqlite:///"
            + Path(self.directory.name, "asset-scope-occupancy.db").as_posix()
        )
        engine = create_sql_engine(self.url)
        Base.metadata.create_all(engine)
        with create_session_factory(engine).begin() as session:
            session.add_all(
                [
                    Project(
                        id="PROJECT-MINE-561",
                        number="P-561-MINE",
                        name="Projet visible 561",
                        project_manager_external_id="EMP-PM-561",
                    ),
                    Project(
                        id="PROJECT-HIDDEN-561",
                        number="P-561-HIDDEN",
                        name="Projet hors périmètre 561",
                    ),
                    Resource(
                        id="RESOURCE-VISIBLE-561",
                        name="Jean Tremblay",
                        active=True,
                        sort_order=10,
                    ),
                    Resource(
                        id="RESOURCE-HIDDEN-561",
                        name="Jean Tremblay hors périmètre",
                        active=True,
                        sort_order=20,
                    ),
                ]
            )
            seed_test_approval_routing(session, map_existing_tasks=True)
        engine.dispose()

        self.admin = TestClient(
            create_api_app(self.url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER),
            raise_server_exceptions=False,
        )
        self.admin.__enter__()

        created_type = self.admin.post(
            "/api/v1/assets/types",
            json={
                "code": "NACELLE-561",
                "label": "Nacelle 561",
                "category": "EQUIPMENT",
            },
        )
        self.assertEqual(created_type.status_code, 201, created_type.text)
        self.type_id = created_type.json()["id"]

        engine = create_sql_engine(self.url)
        with create_session_factory(engine).begin() as session:
            map_asset_type_to_test_approval_scope(session, self.type_id)
        engine.dispose()

        self.asset_ids: list[str] = []
        for code in ("N561-A", "N561-B"):
            created = self.admin.post(
                "/api/v1/assets",
                json={
                    "code": code,
                    "label": code,
                    "asset_type_id": self.type_id,
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            self.asset_ids.append(created.json()["id"])

        principal = AuthPrincipal.from_roles(
            local_user_id="PM-561",
            issuer="urn:resourceplanner:test",
            subject="pm-561",
            display_name="Chargé de projet 561",
            email=None,
            employee_external_id="EMP-PM-561",
            roles=(ROLE_PROJECT_MANAGER,),
            auth_mode="test",
        )
        self.pm = TestClient(
            create_api_app(self.url, auth_resolver=static_auth_resolver(principal)),
            raise_server_exceptions=False,
        )
        self.pm.__enter__()

    def tearDown(self) -> None:
        self.pm.__exit__(None, None, None)
        self.admin.__exit__(None, None, None)
        self.directory.cleanup()

    def _approve(self, project_number: str) -> tuple[str, str]:
        created = self.admin.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": project_number,
                    "submit": True,
                    "lines": [
                        {
                            "kind": "ASSET",
                            "asset_type_id": self.type_id,
                            "desired_start": DAY,
                            "desired_end": "2026-09-26",
                        }
                    ],
                }
            ),
        )
        self.assertEqual(created.status_code, 201, created.text)
        number = created.json()["demand_number"]
        current = self.admin.get(f"/api/v1/demands/{number}").json()
        approved = self.admin.post(
            f"/api/v1/demands/{number}/approve",
            json={"expected_version": current["version"]},
        )
        self.assertEqual(approved.status_code, 200, approved.text)

        engine = create_sql_engine(self.url)
        with create_session_factory(engine)() as session:
            requirement_id = session.scalar(
                select(AssetRequirement.id)
                .join(
                    WorkforceRequest,
                    AssetRequirement.workforce_request_id == WorkforceRequest.id,
                )
                .where(WorkforceRequest.legacy_demand_number == number)
            )
        engine.dispose()
        self.assertIsNotNone(requirement_id)
        return number, str(requirement_id)

    def _reserve(self, requirement_id: str, asset_id: str):
        state = self.admin.get("/api/v1/assets/requirements").json()
        response = self.admin.put(
            f"/api/v1/assets/requirements/{requirement_id}/reservation",
            json={
                "asset_id": asset_id,
                "start_date": DAY,
                "end_date": DAY,
                "expected_planning_version": state["planning_version"],
            },
            headers={"Idempotency-Key": f"reserve-561-{requirement_id}-{asset_id}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _set_operator(self, requirement_id: str, resource_id: str) -> None:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            allocation = session.scalar(
                select(AssetAllocation).where(
                    AssetAllocation.asset_requirement_id == requirement_id
                )
            )
            self.assertIsNotNone(allocation)
            assert allocation is not None
            allocation.operator_resource_id = resource_id
        engine.dispose()

    def _force_asset(self, requirement_id: str, asset_id: str) -> None:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            allocation = session.scalar(
                select(AssetAllocation).where(
                    AssetAllocation.asset_requirement_id == requirement_id
                )
            )
            self.assertIsNotNone(allocation)
            assert allocation is not None
            allocation.asset_id = asset_id
        engine.dispose()

    @staticmethod
    def _cell(payload: dict, asset_id: str, day: str = DAY) -> dict:
        return next(
            row
            for row in payload["asset_capacity"]
            if row["asset_id"] == asset_id and row["day"] == day
        )

    def _snapshot(self, client: TestClient, scope: str) -> tuple[dict, str]:
        response = client.get(
            "/api/v1/planning/snapshot"
            f"?start={DAY}&end=2026-09-26&scope={scope}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), response.text

    def test_hidden_reservation_keeps_asset_unavailable_without_detail_leak(self) -> None:
        hidden_number, hidden_requirement_id = self._approve("P-561-HIDDEN")
        self._reserve(hidden_requirement_id, self.asset_ids[0])
        self._set_operator(hidden_requirement_id, "RESOURCE-HIDDEN-561")
        _visible_number, visible_requirement_id = self._approve("P-561-MINE")

        state = self.admin.get("/api/v1/assets/requirements").json()
        blocked = self.admin.put(
            f"/api/v1/assets/requirements/{visible_requirement_id}/reservation",
            json={
                "asset_id": self.asset_ids[0],
                "start_date": DAY,
                "end_date": DAY,
                "expected_planning_version": state["planning_version"],
            },
            headers={"Idempotency-Key": "reserve-561-hidden-block"},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["error"]["code"], "asset_double_booking")

        mine, raw = self._snapshot(self.pm, "mine")
        hidden_cell = self._cell(mine, self.asset_ids[0])
        unused_cell = self._cell(mine, self.asset_ids[1])

        self.assertEqual(hidden_cell["occupied_units"], 1)
        self.assertFalse(hidden_cell["available"])
        self.assertEqual(hidden_cell["visible_occupations"], [])
        self.assertTrue(hidden_cell["has_hidden_occupancy"])
        self.assertTrue(unused_cell["available"])
        self.assertFalse(unused_cell["has_hidden_occupancy"])
        self.assertEqual(mine["asset_allocations"], [])
        self.assertEqual(
            {row["id"] for row in mine["assets"]},
            set(self.asset_ids),
        )
        self.assertNotIn("P-561-HIDDEN", raw)
        self.assertNotIn("Jean Tremblay hors périmètre", raw)
        self.assertNotIn(hidden_number, raw)

    def test_visible_and_hidden_occupations_keep_global_conflict_and_scoped_details(self) -> None:
        visible_number, visible_requirement_id = self._approve("P-561-MINE")
        hidden_number, hidden_requirement_id = self._approve("P-561-HIDDEN")
        self._reserve(visible_requirement_id, self.asset_ids[0])
        self._reserve(hidden_requirement_id, self.asset_ids[1])
        self._set_operator(visible_requirement_id, "RESOURCE-VISIBLE-561")
        self._set_operator(hidden_requirement_id, "RESOURCE-HIDDEN-561")
        self._force_asset(hidden_requirement_id, self.asset_ids[0])

        global_payload, _ = self._snapshot(self.admin, "global")
        global_cell = self._cell(global_payload, self.asset_ids[0])
        global_occupations = {
            row["project_number"]: row for row in global_cell["visible_occupations"]
        }

        self.assertEqual(global_cell["occupied_units"], 2)
        self.assertFalse(global_cell["available"])
        self.assertFalse(global_cell["has_hidden_occupancy"])
        self.assertEqual(
            set(global_occupations),
            {"P-561-MINE", "P-561-HIDDEN"},
        )
        self.assertEqual(
            global_occupations["P-561-MINE"]["operator_resource_name"],
            "Jean Tremblay",
        )
        self.assertEqual(
            global_occupations["P-561-HIDDEN"]["operator_resource_name"],
            "Jean Tremblay hors périmètre",
        )

        mine, raw = self._snapshot(self.pm, "mine")
        mine_cell = self._cell(mine, self.asset_ids[0])
        self.assertEqual(mine_cell["occupied_units"], 2)
        self.assertFalse(mine_cell["available"])
        self.assertTrue(mine_cell["has_hidden_occupancy"])
        self.assertEqual(len(mine_cell["visible_occupations"]), 1)
        self.assertEqual(
            mine_cell["visible_occupations"][0]["project_number"],
            "P-561-MINE",
        )
        self.assertEqual(
            mine_cell["visible_occupations"][0]["operator_resource_name"],
            "Jean Tremblay",
        )
        self.assertEqual(
            [row["operator_resource_name"] for row in mine_cell["visible_occupations"]],
            ["Jean Tremblay"],
        )
        self.assertNotIn("P-561-HIDDEN", raw)
        self.assertNotIn("Jean Tremblay hors périmètre", raw)
        self.assertNotIn(hidden_number, raw)
        self.assertIn(visible_number, raw)

    def test_visible_reservation_without_operator_does_not_invent_resource(self) -> None:
        _number, requirement_id = self._approve("P-561-MINE")
        self._reserve(requirement_id, self.asset_ids[1])

        mine, _raw = self._snapshot(self.pm, "mine")
        cell = self._cell(mine, self.asset_ids[1])

        self.assertEqual(cell["occupied_units"], 1)
        self.assertFalse(cell["has_hidden_occupancy"])
        self.assertEqual(len(cell["visible_occupations"]), 1)
        occupation = cell["visible_occupations"][0]
        self.assertEqual(occupation["project_number"], "P-561-MINE")
        self.assertIsNone(occupation["operator_resource_id"])
        self.assertIsNone(occupation["operator_resource_name"])

    def test_candidate_availability_is_global_without_hidden_scope_details(self) -> None:
        hidden_number, hidden_requirement_id = self._approve("P-561-HIDDEN")
        self._reserve(hidden_requirement_id, self.asset_ids[0])
        self._set_operator(hidden_requirement_id, "RESOURCE-HIDDEN-561")

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                ResourceRequirement(
                    id="REQ-CANDIDATE-561",
                    project_id="PROJECT-MINE-561",
                    workforce_request_id=None,
                    origin="AD_HOC",
                    start_date=date.fromisoformat(DAY),
                    end_date=date.fromisoformat(DAY),
                    planned_hours=Decimal("8"),
                    status="Planifié",
                )
            )
            session.flush()
            session.add(
                Shift(
                    id="SHIFT-CANDIDATE-561",
                    resource_requirement_id="REQ-CANDIDATE-561",
                    resource_id="RESOURCE-VISIBLE-561",
                    work_date=date.fromisoformat(DAY),
                    hours=Decimal("8"),
                    source="MANUAL",
                    locked=True,
                )
            )
        engine.dispose()

        response = self.pm.get(
            "/api/v1/assets/shifts/SHIFT-CANDIDATE-561/assignment/candidates"
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        by_id = {row["id"]: row for row in payload["candidates"]}
        self.assertFalse(by_id[self.asset_ids[0]]["available"])
        self.assertEqual(
            by_id[self.asset_ids[0]]["reason"],
            "asset_unavailable",
        )
        self.assertTrue(by_id[self.asset_ids[1]]["available"])
        self.assertNotIn("P-561-HIDDEN", response.text)
        self.assertNotIn("Jean Tremblay hors périmètre", response.text)
        self.assertNotIn(hidden_number, response.text)



if __name__ == "__main__":
    unittest.main()
