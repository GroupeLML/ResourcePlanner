"""Exercise approval and exclusive reservation through the actual HTTP boundary."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.infrastructure.sql import (
    AssetTypeApprovalScopeMapping,
    Base,
    Project,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.approval_test_support import (
    TEST_APPROVAL_SCOPE_ID,
    routed_demand_payload,
    seed_test_approval_routing,
)
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


class ReservableAssetApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.url = f"sqlite+pysqlite:///{Path(self.directory.name, 'assets.db').as_posix()}"
        engine = create_sql_engine(self.url)
        Base.metadata.create_all(engine)
        with create_session_factory(engine).begin() as session:
            session.add(Project(id="PROJECT", number="P-1", name="Projet avec nacelle"))
            seed_test_approval_routing(session, map_existing_tasks=True)
        engine.dispose()
        self.client = TestClient(create_api_app(self.url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER), raise_server_exceptions=False)
        self.client.__enter__()
        result = self.client.post("/api/v1/assets/types", json={"code": "NACELLE", "label": "Nacelle", "category": "EQUIPMENT"})
        self.assertEqual(result.status_code, 201, result.text)
        self.type_id = result.json()["id"]
        factory = self.client.app.state.session_factory
        with factory() as session, session.begin():
            session.add(
                AssetTypeApprovalScopeMapping(
                    asset_type_id=self.type_id,
                    approval_scope_id=TEST_APPROVAL_SCOPE_ID,
                )
            )
        self.asset_ids = []
        for code in ("N1", "N2"):
            result = self.client.post("/api/v1/assets", json={"code": code, "label": code, "asset_type_id": self.type_id})
            self.assertEqual(result.status_code, 201, result.text)
            self.asset_ids.append(result.json()["id"])

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def approve(self, *, mixed: bool = False) -> str:
        lines = [{"kind": "ASSET", "asset_type_id": self.type_id,
                  "desired_start": "2026-09-24", "desired_end": "2026-09-26"}]
        if mixed:
            lines.append({"kind": "WORKFORCE", "desired_start": "2026-09-24", "estimated_hours": 8})
        result = self.client.post("/api/v1/demands", json=routed_demand_payload({"project_number": "P-1", "submit": True, "lines": lines}))
        self.assertEqual(result.status_code, 201, result.text)
        number = result.json()["demand_number"]
        version = self.client.get(f"/api/v1/demands/{number}").json()["version"]
        approved = self.client.post(f"/api/v1/demands/{number}/approve", json={"expected_version": version})
        self.assertEqual(approved.status_code, 200, approved.text)
        return number

    def reserve(self, requirement_id: str, *, asset_id: str | None, version: int, key: str,
                start_date: str | None = None, end_date: str | None = None,
                include_dates: bool = True):
        if include_dates and asset_id is not None and start_date is None and end_date is None:
            start_date = "2026-09-24"
            end_date = "2026-09-24"
        return self.client.put(f"/api/v1/assets/requirements/{requirement_id}/reservation",
                               json={"asset_id": asset_id, "start_date": start_date, "end_date": end_date,
                                     "expected_planning_version": version},
                               headers={"Idempotency-Key": key})

    def test_two_requests_cannot_reserve_same_asset_on_same_day(self) -> None:
        self.approve()
        self.approve()
        snapshot = self.client.get("/api/v1/assets/requirements").json()
        first, second = [row["id"] for row in snapshot["requirements"]]
        version = snapshot["planning_version"]
        reserved = self.reserve(first, asset_id=self.asset_ids[0], version=version, key="first")
        self.assertEqual(reserved.status_code, 200, reserved.text)
        replay = self.reserve(first, asset_id=self.asset_ids[0], version=version, key="first")
        self.assertEqual(replay.json(), reserved.json())
        collision = self.reserve(second, asset_id=self.asset_ids[0], version=reserved.json()["planning_version"], key="second")
        self.assertEqual(collision.status_code, 409)
        self.assertEqual(collision.json()["error"]["code"], "asset_double_booking")
        separate = self.reserve(second, asset_id=self.asset_ids[1], version=reserved.json()["planning_version"], key="other")
        self.assertEqual(separate.status_code, 200, separate.text)
        state = self.client.get("/api/v1/assets/requirements").json()
        self.assertEqual(len(state["allocations"]), 2)
        self.assertEqual(state["planning_version"], version + 2)

    def test_request_dates_are_explicit_and_existing_mutation_preserves_them(self) -> None:
        self.approve()
        state = self.client.get("/api/v1/assets/requirements").json()
        requirement_id = state["requirements"][0]["id"]
        version = state["planning_version"]

        missing = self.reserve(
            requirement_id,
            asset_id=self.asset_ids[0],
            version=version,
            key="dates-missing",
            include_dates=False,
        )
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertEqual(
            missing.json()["error"]["code"],
            "asset_reservation_dates_required",
        )

        partial = self.reserve(
            requirement_id,
            asset_id=self.asset_ids[0],
            version=version,
            key="dates-partial",
            start_date="2026-09-24",
            end_date=None,
            include_dates=False,
        )
        self.assertEqual(partial.status_code, 422, partial.text)
        self.assertEqual(
            partial.json()["error"]["code"],
            "asset_reservation_dates_required",
        )

        outside = self.reserve(
            requirement_id,
            asset_id=self.asset_ids[0],
            version=version,
            key="dates-outside",
            start_date="2026-09-23",
            end_date="2026-09-23",
        )
        self.assertEqual(outside.status_code, 422, outside.text)
        self.assertEqual(
            outside.json()["error"]["code"],
            "asset_outside_approved_window",
        )

        reserved = self.reserve(
            requirement_id,
            asset_id=self.asset_ids[0],
            version=version,
            key="dates-create",
            start_date="2026-09-25",
            end_date="2026-09-25",
        )
        self.assertEqual(reserved.status_code, 200, reserved.text)
        allocation_id = reserved.json()["allocation_id"]
        created_state = self.client.get("/api/v1/assets/requirements").json()
        allocation = created_state["allocations"][0]
        self.assertEqual(allocation["start_date"], "2026-09-25")
        self.assertEqual(allocation["end_date"], "2026-09-25")

        different_intent = self.reserve(
            requirement_id,
            asset_id=self.asset_ids[0],
            version=version,
            key="dates-create",
            start_date="2026-09-26",
            end_date="2026-09-26",
        )
        self.assertEqual(different_intent.status_code, 409, different_intent.text)

        changed_asset = self.reserve(
            requirement_id,
            asset_id=self.asset_ids[1],
            version=reserved.json()["planning_version"],
            key="dates-preserve",
            include_dates=False,
        )
        self.assertEqual(changed_asset.status_code, 200, changed_asset.text)
        self.assertEqual(changed_asset.json()["allocation_id"], allocation_id)
        changed_state = self.client.get("/api/v1/assets/requirements").json()
        allocation = changed_state["allocations"][0]
        self.assertEqual(allocation["id"], allocation_id)
        self.assertEqual(allocation["asset_id"], self.asset_ids[1])
        self.assertEqual(allocation["start_date"], "2026-09-25")
        self.assertEqual(allocation["end_date"], "2026-09-25")

        moved_dates = self.reserve(
            requirement_id,
            asset_id=self.asset_ids[1],
            version=changed_asset.json()["planning_version"],
            key="dates-move",
            start_date="2026-09-24",
            end_date="2026-09-26",
        )
        self.assertEqual(moved_dates.status_code, 200, moved_dates.text)
        moved_state = self.client.get("/api/v1/assets/requirements").json()
        allocation = moved_state["allocations"][0]
        self.assertEqual(allocation["id"], allocation_id)
        self.assertEqual(allocation["start_date"], "2026-09-24")
        self.assertEqual(allocation["end_date"], "2026-09-26")

    def test_draft_asset_request_can_be_submitted_through_workflow(self) -> None:
        created = self.client.post("/api/v1/demands", json=routed_demand_payload({
            "project_number": "P-1",
            "lines": [
                {
                    "kind": "ASSET",
                    "asset_type_id": self.type_id,
                    "proposed_asset_id": self.asset_ids[0],
                    "desired_start": "2026-09-24",
                    "desired_end": "2026-09-26",
                    "estimated_hours": 4,
                },
                {
                    "kind": "WORKFORCE",
                    "desired_start": "2026-09-24",
                    "estimated_hours": 8,
                },
            ],
        }))
        self.assertEqual(created.status_code, 201, created.text)
        number = created.json()["demand_number"]
        draft = self.client.get(f"/api/v1/demands/{number}").json()

        submitted = self.client.post(
            f"/api/v1/demands/{number}/submit",
            json={"expected_version": draft["version"]},
        )

        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(
            self.client.get(f"/api/v1/demands/{number}").json()["status"],
            "Soumise",
        )

    def test_mixed_request_unbudgeted_asset_and_human_hours(self) -> None:
        number = self.approve(mixed=True)
        state = self.client.get("/api/v1/assets/requirements").json()
        self.assertEqual(len(state["requirements"]), 1)
        self.assertEqual(self.client.get(f"/api/v1/demands/{number}").json()["lines"][0]["kind"], "ASSET")
        self.assertIsNone(state["requirements"][0]["usage_hours"])
        self.assertEqual(len(state["allocations"]), 0)
        self.assertEqual(len(state["requirements"]), 1)

    def test_stale_version_unavailability_and_release(self) -> None:
        self.approve()
        state = self.client.get("/api/v1/assets/requirements").json()
        identifier = state["requirements"][0]["id"]
        version = state["planning_version"]
        unavailable = self.client.post(f"/api/v1/assets/{self.asset_ids[0]}/unavailability", json={
            "start_date": "2026-09-25", "end_date": "2026-09-25", "expected_planning_version": version})
        self.assertEqual(unavailable.status_code, 201, unavailable.text)
        stale = self.reserve(identifier, asset_id=self.asset_ids[0], version=version, key="stale")
        self.assertEqual(stale.json()["error"]["code"], "planning_version_conflict")
        blocked = self.reserve(
            identifier,
            asset_id=self.asset_ids[0],
            version=version + 1,
            key="blocked",
            start_date="2026-09-25",
            end_date="2026-09-25",
        )
        self.assertEqual(blocked.json()["error"]["code"], "asset_unavailable")
        reserved = self.reserve(identifier, asset_id=self.asset_ids[1], version=version + 1, key="okay")
        self.assertEqual(reserved.status_code, 200, reserved.text)
        released = self.reserve(identifier, asset_id=None, version=version + 2, key="release")
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(self.client.get("/api/v1/assets/requirements").json()["allocations"], [])
        cleared = self.client.delete(
            f"/api/v1/assets/{self.asset_ids[0]}/unavailability/{unavailable.json()['id']}",
            params={"expected_planning_version": released.json()["planning_version"]},
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)

    def test_unbudgeted_alternatives_materialize_only_selected_period(self) -> None:
        created = self.client.post("/api/v1/demands", json=routed_demand_payload({
            "project_number": "P-1", "submit": True,
            "lines": [{"kind": "ASSET", "asset_type_id": self.type_id,
                       "desired_start": "2026-09-24", "desired_end": "2026-09-26"}],
        }))
        self.assertEqual(created.status_code, 201, created.text)
        number = created.json()["demand_number"]
        demand = self.client.get(f"/api/v1/demands/{number}").json()
        line_id = demand["lines"][0]["line_id"]
        replaced = self.client.put(f"/api/v1/demands/{number}/lines/{line_id}/periods", json={
            "expected_request_version": demand["version"],
            "periods": [
                {"period_id": "A", "start_date": "2026-09-24", "end_date": "2026-09-24",
                 "kind": "ALTERNATIVE", "alternative_group": "CHOIX"},
                {"period_id": "B", "start_date": "2026-09-25", "end_date": "2026-09-25",
                 "kind": "ALTERNATIVE", "alternative_group": "CHOIX"},
            ],
        })
        self.assertEqual(replaced.status_code, 200, replaced.text)
        selected = self.client.put(
            f"/api/v1/demands/{number}/lines/{line_id}/alternative-groups/CHOIX/selection",
            json={"period_id": "A"},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        approved = self.client.post(f"/api/v1/demands/{number}/approve", json={})
        self.assertEqual(approved.status_code, 200, approved.text)
        state = self.client.get("/api/v1/assets/requirements").json()
        self.assertEqual(len(state["requirements"]), 1)
        self.assertEqual(state["requirements"][0]["start_date"], "2026-09-24")
        approval_state = self.client.get(f"/api/v1/demands/{number}/approval-state")
        self.assertEqual(approval_state.status_code, 200, approval_state.text)
        operational_version = approval_state.json()["operational_version"]
        changed = self.client.put(
            f"/api/v1/demands/{number}/lines/{line_id}/operational-alternative-groups/CHOIX/selection",
            json={"period_id": "B", "expected_operational_version": operational_version},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        new_state = self.client.get("/api/v1/assets/requirements").json()
        self.assertEqual(len(new_state["requirements"]), 1)
        self.assertEqual(new_state["requirements"][0]["start_date"], "2026-09-25")

    def test_reapproval_retains_compatible_manual_allocation(self) -> None:
        number = self.approve()
        state = self.client.get("/api/v1/assets/requirements").json()
        row = state["requirements"][0]
        reserved = self.reserve(row["id"], asset_id=self.asset_ids[0],
                                version=state["planning_version"], key="before-approval")
        self.assertEqual(reserved.status_code, 200, reserved.text)
        allocation_id = reserved.json()["allocation_id"]
        demand = self.client.get(f"/api/v1/demands/{number}").json()
        changed = self.client.patch(f"/api/v1/demands/{number}", json={
            "expected_version": demand["version"],
            "lines": [{"id": demand["lines"][0]["line_id"], "kind": "ASSET",
                       "asset_type_id": self.type_id, "desired_start": "2026-09-24",
                       "desired_end": "2026-09-26", "estimated_hours": 4,
                       "task_code": "APPROVAL"}],
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        # An administrator may directly approve the changed authorization in this transaction.
        self.assertFalse(changed.json()["reapproval_required"])
        current = self.client.get("/api/v1/assets/requirements").json()
        self.assertEqual(current["allocations"][0]["id"], allocation_id)
        final = self.client.get("/api/v1/assets/requirements").json()
        self.assertEqual(final["requirements"][0]["id"], row["id"])
        self.assertEqual(float(final["requirements"][0]["usage_hours"]), 4.0)
        self.assertEqual(final["allocations"][0]["id"], allocation_id)


if __name__ == "__main__":
    unittest.main()
