from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.domain.reservable_assets import AssetRequirementOrigin
from app.infrastructure.sql import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    AssetTypeCompetency,
    Base,
    Competency,
    PlanningChangeHistory,
    Project,
    Resource,
    ResourceCompetency,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.approval_test_support import (
    map_asset_type_to_test_approval_scope,
    seed_test_approval_routing,
)
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


DAY = date(2026, 10, 12)


class DirectAssetReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.url = (
            "sqlite+pysqlite:///"
            + Path(self.directory.name, "direct-assets.db").as_posix()
        )
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        Base.metadata.create_all(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    Project(id="PROJECT-575C", number="P-575C", name="Projet 575C"),
                    Resource(
                        id="RESOURCE-SKILLED",
                        name="Technicien qualifié",
                        active=True,
                    ),
                    Resource(
                        id="RESOURCE-UNSKILLED",
                        name="Technicien non qualifié",
                        active=True,
                    ),
                    Resource(
                        id="RESOURCE-INACTIVE",
                        name="Technicien inactif",
                        active=False,
                    ),
                    Competency(
                        id="COMP-575C",
                        name="Permis 575C",
                        active=True,
                    ),
                    AssetType(
                        id="TYPE-575C",
                        code="VEH-575C",
                        label="Véhicule 575C",
                        category="VEHICLE",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ResourceCompetency(
                        resource_id="RESOURCE-SKILLED",
                        competency_id="COMP-575C",
                    ),
                    ResourceCompetency(
                        resource_id="RESOURCE-INACTIVE",
                        competency_id="COMP-575C",
                    ),
                    AssetTypeCompetency(
                        asset_type_id="TYPE-575C",
                        competency_id="COMP-575C",
                    ),
                    Asset(
                        id="ASSET-A",
                        code="A-575C",
                        label="Actif A",
                        asset_type_id="TYPE-575C",
                    ),
                    Asset(
                        id="ASSET-B",
                        code="B-575C",
                        label="Actif B",
                        asset_type_id="TYPE-575C",
                    ),
                ]
            )
            seed_test_approval_routing(session, map_existing_tasks=True)
            map_asset_type_to_test_approval_scope(session, "TYPE-575C")
        engine.dispose()

        self.client = TestClient(
            create_api_app(
                self.url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            ),
            raise_server_exceptions=False,
        )
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def _version(self) -> int:
        response = self.client.get("/api/v1/assets/requirements")
        self.assertEqual(response.status_code, 200, response.text)
        return int(response.json()["planning_version"])

    def _project_create(
        self,
        *,
        asset_id: str = "ASSET-A",
        operator_resource_id: str | None = None,
        version: int | None = None,
        key: str = "project-create-575c",
    ):
        return self.client.post(
            "/api/v1/assets/project-reservations",
            headers={"Idempotency-Key": key},
            json={
                "project_id": "PROJECT-575C",
                "asset_type_id": "TYPE-575C",
                "asset_id": asset_id,
                "start_date": DAY.isoformat(),
                "end_date": DAY.isoformat(),
                "operator_resource_id": operator_resource_id,
                "expected_planning_version": version or self._version(),
            },
        )

    def _resource_create(
        self,
        *,
        resource_id: str = "RESOURCE-SKILLED",
        asset_id: str = "ASSET-A",
        project_id: str | None = None,
        version: int | None = None,
        key: str = "resource-create-575c",
    ):
        return self.client.post(
            "/api/v1/assets/resource-period-reservations",
            headers={"Idempotency-Key": key},
            json={
                "resource_id": resource_id,
                "project_id": project_id,
                "asset_type_id": "TYPE-575C",
                "asset_id": asset_id,
                "start_date": DAY.isoformat(),
                "end_date": DAY.isoformat(),
                "expected_planning_version": version or self._version(),
            },
        )

    def test_project_direct_can_start_without_operator_then_receive_one(self) -> None:
        version = self._version()
        created = self._project_create(version=version)
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        self.assertEqual(payload["operation"], "CREATE")
        self.assertEqual(payload["requirement_origin"], "PROJECT_DIRECT")
        self.assertEqual(payload["project_id"], "PROJECT-575C")
        self.assertIsNone(payload["context_resource_id"])
        self.assertIsNone(payload["operator_resource_id"])
        self.assertEqual(payload["qualification_state"], "MISSING_OPERATOR")
        self.assertEqual(payload["planning_version"], version + 1)

        replay = self._project_create(version=version)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), payload)
        self.assertEqual(self._version(), payload["planning_version"])

        candidates = self.client.get(
            f"/api/v1/assets/requirements/{payload['requirement_id']}/operator-candidates"
        )
        self.assertEqual(candidates.status_code, 200, candidates.text)
        self.assertEqual(
            [row["resource_id"] for row in candidates.json()["candidates"]],
            ["RESOURCE-SKILLED"],
        )

        updated = self.client.put(
            f"/api/v1/assets/project-reservations/{payload['requirement_id']}",
            headers={"Idempotency-Key": "project-operator-575c"},
            json={
                "asset_id": "ASSET-A",
                "start_date": DAY.isoformat(),
                "end_date": DAY.isoformat(),
                "operator_resource_id": "RESOURCE-SKILLED",
                "expected_planning_version": payload["planning_version"],
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        changed = updated.json()
        self.assertEqual(changed["operation"], "UPDATE")
        self.assertEqual(changed["requirement_id"], payload["requirement_id"])
        self.assertEqual(changed["allocation_id"], payload["allocation_id"])
        self.assertEqual(changed["operator_resource_id"], "RESOURCE-SKILLED")
        self.assertEqual(changed["qualification_state"], "SATISFIED")

        state = self.client.get("/api/v1/assets/requirements").json()
        requirement = next(
            row
            for row in state["requirements"]
            if row["id"] == payload["requirement_id"]
        )
        allocation = next(
            row
            for row in state["allocations"]
            if row["requirement_id"] == payload["requirement_id"]
        )
        self.assertEqual(requirement["origin"], "PROJECT_DIRECT")
        self.assertIsNone(requirement["request_id"])
        self.assertIsNone(requirement["resource_requirement_id"])
        self.assertIsNone(requirement["shift_id"])
        self.assertEqual(allocation["operator_resource_id"], "RESOURCE-SKILLED")

    def test_project_direct_invalid_operator_rolls_back_without_version_change(self) -> None:
        version = self._version()
        rejected = self._project_create(
            operator_resource_id="RESOURCE-UNSKILLED",
            version=version,
            key="project-unskilled-575c",
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(
            rejected.json()["error"]["code"],
            "asset_operator_skill_mismatch",
        )
        self.assertEqual(self._version(), version)
        state = self.client.get("/api/v1/assets/requirements").json()
        self.assertEqual(state["requirements"], [])
        self.assertEqual(state["allocations"], [])

    def test_resource_period_requires_qualified_beneficiary_and_keeps_operator_equal(self) -> None:
        version = self._version()
        created = self._resource_create(version=version)
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        self.assertEqual(payload["requirement_origin"], "RESOURCE_PERIOD")
        self.assertIsNone(payload["project_id"])
        self.assertEqual(payload["context_resource_id"], "RESOURCE-SKILLED")
        self.assertEqual(payload["operator_resource_id"], "RESOURCE-SKILLED")
        self.assertEqual(payload["qualification_state"], "SATISFIED")

        updated = self.client.put(
            f"/api/v1/assets/resource-period-reservations/{payload['requirement_id']}",
            headers={"Idempotency-Key": "resource-update-575c"},
            json={
                "project_id": "PROJECT-575C",
                "asset_id": "ASSET-B",
                "start_date": DAY.isoformat(),
                "end_date": DAY.isoformat(),
                "expected_planning_version": payload["planning_version"],
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        changed = updated.json()
        self.assertEqual(changed["project_id"], "PROJECT-575C")
        self.assertEqual(changed["asset_id"], "ASSET-B")
        self.assertEqual(changed["context_resource_id"], "RESOURCE-SKILLED")
        self.assertEqual(changed["operator_resource_id"], "RESOURCE-SKILLED")
        self.assertEqual(changed["qualification_state"], "SATISFIED")

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                requirement = session.get(
                    AssetRequirement,
                    payload["requirement_id"],
                )
                self.assertIsNotNone(requirement)
                assert requirement is not None
                allocation = session.scalar(
                    select(AssetAllocation).where(
                        AssetAllocation.asset_requirement_id == requirement.id
                    )
                )
                self.assertIsNotNone(allocation)
                assert allocation is not None
                self.assertEqual(
                    requirement.origin,
                    AssetRequirementOrigin.RESOURCE_PERIOD.value,
                )
                self.assertEqual(
                    requirement.context_resource_id,
                    allocation.operator_resource_id,
                )
        finally:
            engine.dispose()

    def test_resource_period_rejects_unskilled_and_inactive_resources(self) -> None:
        version = self._version()
        unskilled = self._resource_create(
            resource_id="RESOURCE-UNSKILLED",
            version=version,
            key="resource-unskilled-575c",
        )
        self.assertEqual(unskilled.status_code, 422, unskilled.text)
        self.assertEqual(
            unskilled.json()["error"]["code"],
            "asset_operator_skill_mismatch",
        )
        self.assertEqual(self._version(), version)

        inactive = self._resource_create(
            resource_id="RESOURCE-INACTIVE",
            version=version,
            key="resource-inactive-575c",
        )
        self.assertEqual(inactive.status_code, 422, inactive.text)
        self.assertEqual(
            inactive.json()["error"]["code"],
            "asset_context_resource_unavailable",
        )
        self.assertEqual(self._version(), version)

    def test_direct_reservations_share_global_double_booking(self) -> None:
        first = self._project_create()
        self.assertEqual(first.status_code, 201, first.text)
        blocked = self._resource_create(
            version=first.json()["planning_version"],
            key="resource-double-booked-575c",
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["error"]["code"], "asset_double_booking")
        self.assertEqual(
            self._version(),
            first.json()["planning_version"],
        )

    def test_release_deletes_support_requirement_and_keeps_audit(self) -> None:
        created = self._project_create()
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        released = self.client.delete(
            f"/api/v1/assets/project-reservations/{payload['requirement_id']}",
            params={"expected_planning_version": payload["planning_version"]},
            headers={"Idempotency-Key": "project-release-575c"},
        )
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(released.json()["operation"], "RELEASE")

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                self.assertIsNone(
                    session.get(AssetRequirement, payload["requirement_id"])
                )
                allocations = session.scalars(
                    select(AssetAllocation).where(
                        AssetAllocation.asset_requirement_id
                        == payload["requirement_id"]
                    )
                ).all()
                self.assertEqual(allocations, [])
                audits = int(
                    session.scalar(
                        select(func.count())
                        .select_from(PlanningChangeHistory)
                        .where(
                            PlanningChangeHistory.entity_reference
                            == payload["requirement_id"]
                        )
                    )
                    or 0
                )
                self.assertEqual(audits, 2)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
