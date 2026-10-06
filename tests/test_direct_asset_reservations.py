from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

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
from app.infrastructure.sql.approval_scope_models import ApprovalScopeApprover
from app.server import create_api_app
from tests.approval_test_support import (
    TEST_ADMIN_USER_ID,
    TEST_APPROVAL_SCOPE_ID,
    TEST_COORDINATOR_USER_ID,
    map_asset_type_to_test_approval_scope,
    seed_test_approval_routing,
)
from tests.http_test_auth import (
    TEST_ADMIN_AUTH_RESOLVER,
    TEST_COORDINATOR_AUTH_RESOLVER,
)


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
                    AssetType(
                        id="TYPE-615A-OTHER",
                        code="VEH-615A-OTHER",
                        label="Autre type 615A",
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

    def test_admin_role_does_not_bypass_asset_authority(self) -> None:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.execute(
                    delete(ApprovalScopeApprover).where(
                        ApprovalScopeApprover.approval_scope_id
                        == TEST_APPROVAL_SCOPE_ID,
                        ApprovalScopeApprover.app_user_id
                        == TEST_ADMIN_USER_ID,
                    )
                )
        finally:
            engine.dispose()

        denied = self._project_create(key="admin-bypass-denied-615a")
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(
            denied.json()["error"]["code"],
            "asset_assignment_authority_required",
        )

    def test_coordinator_requires_current_unit_authority_for_create_replace_and_release(self) -> None:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.execute(
                    delete(ApprovalScopeApprover).where(
                        ApprovalScopeApprover.approval_scope_id
                        == TEST_APPROVAL_SCOPE_ID,
                        ApprovalScopeApprover.app_user_id
                        == TEST_COORDINATOR_USER_ID,
                    )
                )
        finally:
            engine.dispose()

        with TestClient(
            create_api_app(
                self.url,
                auth_resolver=TEST_COORDINATOR_AUTH_RESOLVER,
            ),
            raise_server_exceptions=False,
        ) as coordinator:
            denied = coordinator.post(
                "/api/v1/assets/project-reservations",
                headers={"Idempotency-Key": "coord-denied-615a"},
                json={
                    "project_id": "PROJECT-575C",
                    "asset_type_id": "TYPE-575C",
                    "asset_id": "ASSET-A",
                    "start_date": DAY.isoformat(),
                    "end_date": DAY.isoformat(),
                    "operator_resource_id": None,
                    "expected_planning_version": coordinator.get(
                        "/api/v1/assets/requirements"
                    ).json()["planning_version"],
                },
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(
                denied.json()["error"]["code"],
                "asset_assignment_authority_required",
            )

            self_add = coordinator.put(
                f"/api/v1/assets/ASSET-A/approvers/{TEST_COORDINATOR_USER_ID}"
            )
            self.assertEqual(self_add.status_code, 403, self_add.text)

            type_change = coordinator.patch(
                "/api/v1/assets/ASSET-A",
                json={
                    "asset_type_id": "TYPE-615A-OTHER",
                    "expected_planning_version": coordinator.get(
                        "/api/v1/assets/requirements"
                    ).json()["planning_version"],
                },
            )
            self.assertEqual(type_change.status_code, 403, type_change.text)
            self.assertEqual(
                type_change.json()["error"]["code"],
                "asset_authority_admin_required",
            )

            granted = self.client.put(
                f"/api/v1/assets/ASSET-A/approvers/{TEST_COORDINATOR_USER_ID}"
            )
            self.assertEqual(granted.status_code, 200, granted.text)

            created = coordinator.post(
                "/api/v1/assets/project-reservations",
                headers={"Idempotency-Key": "coord-create-615a"},
                json={
                    "project_id": "PROJECT-575C",
                    "asset_type_id": "TYPE-575C",
                    "asset_id": "ASSET-A",
                    "start_date": DAY.isoformat(),
                    "end_date": DAY.isoformat(),
                    "operator_resource_id": None,
                    "expected_planning_version": coordinator.get(
                        "/api/v1/assets/requirements"
                    ).json()["planning_version"],
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            requirement_id = created.json()["requirement_id"]

            replace_denied = coordinator.put(
                f"/api/v1/assets/project-reservations/{requirement_id}",
                headers={"Idempotency-Key": "coord-replace-denied-615a"},
                json={
                    "asset_id": "ASSET-B",
                    "start_date": DAY.isoformat(),
                    "end_date": DAY.isoformat(),
                    "operator_resource_id": None,
                    "expected_planning_version": created.json()["planning_version"],
                },
            )
            self.assertEqual(replace_denied.status_code, 403, replace_denied.text)
            self.assertEqual(
                replace_denied.json()["error"]["code"],
                "asset_assignment_authority_required",
            )

            granted_b = self.client.put(
                f"/api/v1/assets/ASSET-B/approvers/{TEST_COORDINATOR_USER_ID}"
            )
            self.assertEqual(granted_b.status_code, 200, granted_b.text)
            current_version = coordinator.get(
                "/api/v1/assets/requirements"
            ).json()["planning_version"]
            replaced = coordinator.put(
                f"/api/v1/assets/project-reservations/{requirement_id}",
                headers={"Idempotency-Key": "coord-replace-615a"},
                json={
                    "asset_id": "ASSET-B",
                    "start_date": DAY.isoformat(),
                    "end_date": DAY.isoformat(),
                    "operator_resource_id": None,
                    "expected_planning_version": current_version,
                },
            )
            self.assertEqual(replaced.status_code, 200, replaced.text)

            revoked_b = self.client.delete(
                f"/api/v1/assets/ASSET-B/approvers/{TEST_COORDINATOR_USER_ID}"
            )
            self.assertEqual(revoked_b.status_code, 200, revoked_b.text)
            release_denied = coordinator.delete(
                f"/api/v1/assets/project-reservations/{requirement_id}",
                headers={"Idempotency-Key": "coord-release-denied-615a"},
                params={
                    "expected_planning_version": coordinator.get(
                        "/api/v1/assets/requirements"
                    ).json()["planning_version"],
                },
            )
            self.assertEqual(release_denied.status_code, 403, release_denied.text)
            self.assertEqual(
                release_denied.json()["error"]["code"],
                "asset_assignment_authority_required",
            )

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
            headers={"Idempotency-Key": "resource-update-615c"},
            json={
                "asset_id": "ASSET-B",
                "start_date": DAY.isoformat(),
                "end_date": DAY.isoformat(),
                "expected_planning_version": payload["planning_version"],
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        changed = updated.json()
        self.assertIsNone(changed["project_id"])
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
                self.assertIsNone(requirement.project_id)
                self.assertEqual(
                    requirement.context_resource_id,
                    allocation.operator_resource_id,
                )
        finally:
            engine.dispose()

    def test_resource_period_rejects_new_project_and_preserves_historical_project(self) -> None:
        version = self._version()
        rejected = self._resource_create(
            project_id="PROJECT-575C",
            version=version,
            key="resource-project-forbidden-615c",
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(self._version(), version)

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                requirement = AssetRequirement(
                    id="AR-HIST-RP-615C",
                    project_id="PROJECT-575C",
                    origin=AssetRequirementOrigin.RESOURCE_PERIOD.value,
                    context_resource_id="RESOURCE-SKILLED",
                    asset_type_id="TYPE-575C",
                    start_date=DAY,
                    end_date=DAY,
                    status="Planifié",
                )
                session.add(requirement)
                session.flush()
                session.add(
                    AssetAllocation(
                        id="ALLOC-HIST-RP-615C",
                        asset_requirement_id=requirement.id,
                        asset_id="ASSET-A",
                        operator_resource_id="RESOURCE-SKILLED",
                        start_date=DAY,
                        end_date=DAY,
                        locked=True,
                        source="MANUAL",
                    )
                )

            current_version = self._version()
            updated = self.client.put(
                "/api/v1/assets/resource-period-reservations/AR-HIST-RP-615C",
                headers={"Idempotency-Key": "resource-historical-update-615c"},
                json={
                    "asset_id": "ASSET-B",
                    "start_date": DAY.isoformat(),
                    "end_date": DAY.isoformat(),
                    "expected_planning_version": current_version,
                },
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["project_id"], "PROJECT-575C")

            with factory() as session:
                historical = session.get(AssetRequirement, "AR-HIST-RP-615C")
                self.assertIsNotNone(historical)
                assert historical is not None
                self.assertEqual(historical.project_id, "PROJECT-575C")
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
