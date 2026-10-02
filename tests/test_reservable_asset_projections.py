from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import (
    Base,
    Project,
    RequestLine,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.approval_test_support import (
    map_asset_type_to_test_approval_scope,
    routed_demand_payload,
    seed_test_approval_routing,
)
from tests.http_test_auth import (
    TEST_ADMIN_AUTH_RESOLVER,
    TEST_PROJECT_MANAGER_AUTH_RESOLVER,
)


class ReservableAssetProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.url = (
            "sqlite+pysqlite:///"
            + Path(self.directory.name, "asset-projections.db").as_posix()
        )
        engine = create_sql_engine(self.url)
        Base.metadata.create_all(engine)
        with create_session_factory(engine).begin() as session:
            session.add(
                Project(
                    id="PROJECT",
                    number="P-1",
                    name="Projet projections actifs",
                )
            )
            seed_test_approval_routing(session, map_existing_tasks=True)
        engine.dispose()

        self.client = TestClient(
            create_api_app(
                self.url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            ),
            raise_server_exceptions=False,
        )
        self.client.__enter__()
        created_type = self.client.post(
            "/api/v1/assets/types",
            json={
                "code": "NACELLE",
                "label": "Nacelle",
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
        for code in ("N1", "N2"):
            created = self.client.post(
                "/api/v1/assets",
                json={
                    "code": code,
                    "label": code,
                    "asset_type_id": self.type_id,
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            self.asset_ids.append(created.json()["id"])

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def _approve_asset_only(self) -> str:
        created = self.client.post(
            "/api/v1/demands",
            json=routed_demand_payload({
                "project_number": "P-1",
                "submit": True,
                "lines": [
                    {
                        "kind": "ASSET",
                        "asset_type_id": self.type_id,
                        "desired_start": "2026-09-24",
                        "desired_end": "2026-09-26",
                    }
                ],
            }),
        )
        self.assertEqual(created.status_code, 201, created.text)
        number = created.json()["demand_number"]
        version = self.client.get(f"/api/v1/demands/{number}").json()["version"]
        approved = self.client.post(
            f"/api/v1/demands/{number}/approve",
            json={"expected_version": version},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        return number

    def test_asset_only_detail_and_snapshot_keep_units_separate_from_workforce_hours(self) -> None:
        number = self._approve_asset_only()

        detail = self.client.get(f"/api/v1/demands/{number}/detail")
        snapshot = self.client.get(
            "/api/v1/planning/snapshot"
            "?start=2026-09-24&end=2026-09-26"
        )

        self.assertEqual(detail.status_code, 200, detail.text)
        materialized = detail.json()["materialized_plan"]
        self.assertEqual(materialized["requirement_count"], 0)
        self.assertEqual(materialized["planned_hours"], 0.0)
        self.assertEqual(materialized["asset_requirement_count"], 1)
        self.assertEqual(materialized["asset_assigned_count"], 0)
        self.assertEqual(materialized["asset_usage_hours"], 0.0)
        self.assertEqual(materialized["asset_unbudgeted_requirement_count"], 1)
        self.assertEqual(
            materialized["asset_requirements"][0]["asset_type_id"],
            self.type_id,
        )

        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        body = snapshot.json()
        self.assertEqual(body["firm_hours"], 0.0)
        self.assertEqual(body["potential_hours"], 0.0)
        self.assertEqual(len(body["asset_requirements"]), 1)
        self.assertEqual(len(body["assets"]), 2)
        self.assertEqual(len(body["asset_capacity"]), 6)
        self.assertIn(
            "ASSET_REQUIREMENT_UNASSIGNED",
            {row["code"] for row in body["asset_diagnostics"]},
        )
        self.assertTrue(
            all(row["capacity_units"] == 1 for row in body["asset_capacity"])
        )

    def test_asset_only_delta_predicts_preserved_reservation_on_reapproval(self) -> None:
        number = self._approve_asset_only()
        state = self.client.get("/api/v1/assets/requirements").json()
        requirement = state["requirements"][0]
        reserved = self.client.put(
            f"/api/v1/assets/requirements/{requirement['id']}/reservation",
            json={
                "asset_id": self.asset_ids[0],
                "start_date": "2026-09-24",
                "end_date": "2026-09-24",
                "expected_planning_version": state["planning_version"],
            },
            headers={"Idempotency-Key": "projection-preserve"},
        )
        self.assertEqual(reserved.status_code, 200, reserved.text)

        candidate = self.client.get(f"/api/v1/demands/{number}").json()
        line = candidate["lines"][0]
        pm_client = TestClient(
            create_api_app(
                self.url,
                auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            ),
            raise_server_exceptions=False,
        )
        with pm_client:
            changed = pm_client.patch(
                f"/api/v1/demands/{number}",
                json={
                    "expected_version": candidate["version"],
                    "lines": [
                        {
                            "id": line["line_id"],
                            "kind": "ASSET",
                            "asset_type_id": self.type_id,
                            "desired_start": line["desired_start"],
                            "desired_end": "2026-09-27",
                            "task_code": "APPROVAL",
                        }
                    ],
                },
            )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertTrue(changed.json()["reapproval_required"])
        self.assertEqual(changed.json()["status"], "Soumise")

        delta = self.client.get(f"/api/v1/demands/{number}/plan-delta")
        self.assertEqual(delta.status_code, 200, delta.text)
        body = delta.json()
        self.assertTrue(body["available"])
        self.assertTrue(body["has_changes"])
        self.assertEqual(body["add_count"], 0)
        self.assertEqual(body["modify_count"], 0)
        self.assertEqual(body["move_count"], 0)
        self.assertEqual(body["cancel_count"], 0)
        self.assertEqual(body["current_hours"], 0.0)
        self.assertEqual(body["proposed_hours"], 0.0)
        self.assertEqual(body["asset_modify_count"], 1)
        self.assertEqual(body["asset_add_count"], 0)
        self.assertEqual(body["asset_cancel_count"], 0)
        self.assertEqual(len(body["asset_items"]), 1)
        self.assertTrue(body["asset_items"][0]["allocation_preserved"])
        self.assertEqual(
            body["asset_items"][0]["proposed_asset_id"],
            self.asset_ids[0],
        )

        approved = self.client.post(
            f"/api/v1/demands/{number}/approve",
            json={},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        after = self.client.get("/api/v1/assets/requirements").json()
        self.assertEqual(len(after["allocations"]), 1)
        self.assertEqual(after["allocations"][0]["asset_id"], self.asset_ids[0])
        self.assertEqual(after["requirements"][0]["end_date"], "2026-09-27")

    def test_locked_asset_incompatible_reapproval_is_explicit_in_delta(self) -> None:
        number = self._approve_asset_only()
        state = self.client.get("/api/v1/assets/requirements").json()
        requirement = state["requirements"][0]
        reserved = self.client.put(
            f"/api/v1/assets/requirements/{requirement['id']}/reservation",
            json={
                "asset_id": self.asset_ids[0],
                "start_date": "2026-09-24",
                "end_date": "2026-09-24",
                "expected_planning_version": state["planning_version"],
            },
            headers={"Idempotency-Key": "projection-block"},
        )
        self.assertEqual(reserved.status_code, 200, reserved.text)

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            request = session.scalar(
                select(WorkforceRequest).where(
                    WorkforceRequest.legacy_demand_number == number
                )
            )
            assert request is not None
            line = session.scalar(
                select(RequestLine).where(
                    RequestLine.workforce_request_id == request.id,
                    RequestLine.active.is_(True),
                )
            )
            assert line is not None
            line.desired_end = date(2026, 9, 25)
            request.status = "Soumise"
        engine.dispose()

        delta = self.client.get(f"/api/v1/demands/{number}/plan-delta")
        self.assertEqual(delta.status_code, 200, delta.text)
        body = delta.json()
        self.assertFalse(body["available"])
        self.assertEqual(
            body["reason"],
            "LOCKED_ASSET_ALLOCATION_INCOMPATIBLE",
        )
        self.assertEqual(body["asset_modify_count"], 1)
        self.assertIn(
            "LOCKED_ASSET_ALLOCATION_INCOMPATIBLE",
            {row["code"] for row in body["diagnostics"]},
        )


if __name__ == "__main__":
    unittest.main()
