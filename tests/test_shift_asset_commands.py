from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.domain.reservable_assets import AssetRequirementOrigin
from app.infrastructure.sql import (
    ORIGIN_AD_HOC,
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    AssetTypeCompetency,
    AssetUnavailability,
    Base,
    Competency,
    PlanningChangeHistory,
    Project,
    Resource,
    ResourceCompetency,
    ResourceRequirement,
    Shift,
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


DAY = date(2026, 10, 5)


class ShiftAssetCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.url = (
            "sqlite+pysqlite:///"
            + Path(self.directory.name, "shift-assets.db").as_posix()
        )
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        Base.metadata.create_all(engine)
        with factory.begin() as session:
            session.add(
                Project(id="PROJECT-560B", number="P-560B", name="Projet 560B")
            )
            session.add_all(
                [
                    Resource(id="RESOURCE-SKILLED", name="Technicien qualifié", active=True),
                    Resource(id="RESOURCE-UNSKILLED", name="Technicien non qualifié", active=True),
                    Competency(id="COMP-560B", name="Permis 560B", active=True),
                    AssetType(
                        id="TYPE-560B",
                        code="VEH-560B",
                        label="Véhicule 560B",
                        category="VEHICLE",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ResourceCompetency(
                        resource_id="RESOURCE-SKILLED",
                        competency_id="COMP-560B",
                    ),
                    AssetTypeCompetency(
                        asset_type_id="TYPE-560B",
                        competency_id="COMP-560B",
                    ),
                    Asset(
                        id="ASSET-A",
                        code="A-560B",
                        label="Actif A",
                        asset_type_id="TYPE-560B",
                    ),
                    Asset(
                        id="ASSET-B",
                        code="B-560B",
                        label="Actif B",
                        asset_type_id="TYPE-560B",
                    ),
                    Asset(
                        id="ASSET-C",
                        code="C-560B",
                        label="Actif C",
                        asset_type_id="TYPE-560B",
                    ),
                    ResourceRequirement(
                        id="HUMAN-SKILLED",
                        project_id="PROJECT-560B",
                        workforce_request_id=None,
                        origin=ORIGIN_AD_HOC,
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("8"),
                        status="Planifié",
                    ),
                    ResourceRequirement(
                        id="HUMAN-UNSKILLED",
                        project_id="PROJECT-560B",
                        workforce_request_id=None,
                        origin=ORIGIN_AD_HOC,
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("8"),
                        status="Planifié",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    Shift(
                        id="SHIFT-SKILLED",
                        legacy_allocation_id="ALLOC-SKILLED",
                        resource_requirement_id="HUMAN-SKILLED",
                        resource_id="RESOURCE-SKILLED",
                        work_date=DAY,
                        hours=Decimal("8"),
                        source="AUTO",
                        locked=False,
                    ),
                    Shift(
                        id="SHIFT-UNSKILLED",
                        legacy_allocation_id="ALLOC-UNSKILLED",
                        resource_requirement_id="HUMAN-UNSKILLED",
                        resource_id="RESOURCE-UNSKILLED",
                        work_date=DAY,
                        hours=Decimal("8"),
                        source="AUTO",
                        locked=False,
                    ),
                ]
            )
            seed_test_approval_routing(session, map_existing_tasks=True)
            map_asset_type_to_test_approval_scope(session, "TYPE-560B")
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

    def _set_asset(
        self,
        *,
        shift_id: str = "SHIFT-SKILLED",
        asset_id: str | None,
        version: int,
        key: str,
        requirement_id: str | None = None,
        client: TestClient | None = None,
    ):
        payload: dict[str, object] = {
            "asset_id": asset_id,
            "expected_planning_version": version,
        }
        if requirement_id is not None:
            payload["asset_requirement_id"] = requirement_id
        return (client or self.client).put(
            f"/api/v1/assets/shifts/{shift_id}/assignment",
            headers={"Idempotency-Key": key},
            json=payload,
        )

    def _ad_hoc_state(self, shift_id: str = "SHIFT-SKILLED"):
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                requirement = session.scalar(
                    select(AssetRequirement).where(
                        AssetRequirement.origin
                        == AssetRequirementOrigin.SHIFT_AD_HOC.value,
                        AssetRequirement.shift_id == shift_id,
                    )
                )
                allocation = (
                    session.scalar(
                        select(AssetAllocation).where(
                            AssetAllocation.asset_requirement_id
                            == requirement.id
                        )
                    )
                    if requirement is not None
                    else None
                )
                shift = session.get(Shift, shift_id)
                self.assertIsNotNone(shift)
                return requirement, allocation, shift
        finally:
            engine.dispose()

    def test_assign_is_atomic_idempotent_and_stale_concurrent_mutation_loses(self) -> None:
        version = self._version()
        first = self._set_asset(
            asset_id="ASSET-A",
            version=version,
            key="assign-560b",
        )
        self.assertEqual(first.status_code, 200, first.text)
        payload = first.json()
        self.assertEqual(payload["operation"], "ASSIGN")
        self.assertEqual(payload["requirement_origin"], "SHIFT_AD_HOC")
        self.assertEqual(payload["operator_resource_id"], "RESOURCE-SKILLED")
        self.assertEqual(payload["qualification_state"], "SATISFIED")
        self.assertEqual(payload["planning_version"], version + 1)

        replay = self._set_asset(
            asset_id="ASSET-A",
            version=version,
            key="assign-560b",
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), payload)

        stale = self._set_asset(
            asset_id="ASSET-B",
            version=version,
            key="assign-stale-560b",
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(
            stale.json()["error"]["code"],
            "planning_version_conflict",
        )

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                requirements = session.scalars(
                    select(AssetRequirement).where(
                        AssetRequirement.origin
                        == AssetRequirementOrigin.SHIFT_AD_HOC.value,
                        AssetRequirement.shift_id == "SHIFT-SKILLED",
                    )
                ).all()
                self.assertEqual(len(requirements), 1)
                allocations = session.scalars(
                    select(AssetAllocation).where(
                        AssetAllocation.asset_requirement_id
                        == requirements[0].id
                    )
                ).all()
                self.assertEqual(len(allocations), 1)
                self.assertEqual(allocations[0].asset_id, "ASSET-A")
                shift = session.get(Shift, "SHIFT-SKILLED")
                self.assertEqual(shift.source, "MANUAL")
                self.assertTrue(shift.locked)
                assignment_audits = int(
                    session.scalar(
                        select(func.count())
                        .select_from(PlanningChangeHistory)
                        .where(
                            PlanningChangeHistory.action
                            == "Affectation actif au quart"
                        )
                    )
                    or 0
                )
                protection_audits = int(
                    session.scalar(
                        select(func.count())
                        .select_from(PlanningChangeHistory)
                        .where(
                            PlanningChangeHistory.action
                            == "Protection quart pour actif ad hoc"
                        )
                    )
                    or 0
                )
                self.assertEqual(assignment_audits, 1)
                self.assertEqual(protection_audits, 1)
        finally:
            engine.dispose()

    def test_incompatible_operator_rolls_back_requirement_allocation_and_shift(self) -> None:
        version = self._version()
        response = self._set_asset(
            shift_id="SHIFT-UNSKILLED",
            asset_id="ASSET-A",
            version=version,
            key="unskilled-560b",
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["error"]["code"],
            "asset_operator_skill_mismatch",
        )
        requirement, allocation, shift = self._ad_hoc_state("SHIFT-UNSKILLED")
        self.assertIsNone(requirement)
        self.assertIsNone(allocation)
        self.assertEqual(shift.source, "AUTO")
        self.assertFalse(shift.locked)
        self.assertEqual(self._version(), version)

    def test_unavailable_asset_fails_without_mutation(self) -> None:
        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                AssetUnavailability(
                    id="UNAVAILABLE-560B",
                    asset_id="ASSET-A",
                    start_date=DAY,
                    end_date=DAY,
                    reason="Inspection",
                )
            )
        engine.dispose()

        version = self._version()
        response = self._set_asset(
            asset_id="ASSET-A",
            version=version,
            key="unavailable-560b",
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "asset_unavailable")
        requirement, allocation, shift = self._ad_hoc_state()
        self.assertIsNone(requirement)
        self.assertIsNone(allocation)
        self.assertEqual(shift.source, "AUTO")
        self.assertFalse(shift.locked)

    def test_failure_after_shift_protection_rolls_back_everything(self) -> None:
        version = self._version()
        with patch(
            "app.infrastructure.sql.asset_service.SqlPlanningAuditJournal.append",
            side_effect=RuntimeError("audit failure after mutation"),
        ):
            response = self._set_asset(
                asset_id="ASSET-A",
                version=version,
                key="rollback-560b",
            )
        self.assertEqual(response.status_code, 500, response.text)
        requirement, allocation, shift = self._ad_hoc_state()
        self.assertIsNone(requirement)
        self.assertIsNone(allocation)
        self.assertEqual(shift.source, "AUTO")
        self.assertFalse(shift.locked)
        self.assertEqual(self._version(), version)

    def test_change_preserves_identities_and_invalid_change_keeps_previous_asset(self) -> None:
        first = self._set_asset(
            asset_id="ASSET-A",
            version=self._version(),
            key="change-first-560b",
        )
        self.assertEqual(first.status_code, 200, first.text)
        first_payload = first.json()

        changed = self._set_asset(
            asset_id="ASSET-B",
            version=first_payload["planning_version"],
            key="change-second-560b",
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        changed_payload = changed.json()
        self.assertEqual(changed_payload["operation"], "CHANGE")
        self.assertEqual(
            changed_payload["requirement_id"],
            first_payload["requirement_id"],
        )
        self.assertEqual(
            changed_payload["allocation_id"],
            first_payload["allocation_id"],
        )
        self.assertEqual(changed_payload["asset_id"], "ASSET-B")

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                AssetUnavailability(
                    id="UNAVAILABLE-C-560B",
                    asset_id="ASSET-C",
                    start_date=DAY,
                    end_date=DAY,
                    reason="Réservé",
                )
            )
        engine.dispose()

        blocked = self._set_asset(
            asset_id="ASSET-C",
            version=changed_payload["planning_version"],
            key="change-invalid-560b",
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["error"]["code"], "asset_unavailable")

        requirement, allocation, shift = self._ad_hoc_state()
        self.assertEqual(requirement.id, first_payload["requirement_id"])
        self.assertEqual(allocation.id, first_payload["allocation_id"])
        self.assertEqual(allocation.asset_id, "ASSET-B")
        self.assertEqual(shift.source, "MANUAL")
        self.assertTrue(shift.locked)
        self.assertEqual(
            self._version(),
            changed_payload["planning_version"],
        )

    def test_release_deletes_ad_hoc_requirement_but_keeps_shift_protected(self) -> None:
        assigned = self._set_asset(
            asset_id="ASSET-A",
            version=self._version(),
            key="release-first-560b",
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        released = self._set_asset(
            asset_id=None,
            version=assigned.json()["planning_version"],
            key="release-final-560b",
        )
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(released.json()["operation"], "RELEASE")
        self.assertEqual(released.json()["requirement_origin"], "SHIFT_AD_HOC")

        requirement, allocation, shift = self._ad_hoc_state()
        self.assertIsNone(requirement)
        self.assertIsNone(allocation)
        self.assertEqual(shift.source, "MANUAL")
        self.assertTrue(shift.locked)

    def test_request_requirement_remains_authoritative_and_keeps_provenance(self) -> None:
        created = self.client.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": "P-560B",
                    "submit": True,
                    "lines": [
                        {
                            "kind": "ASSET",
                            "asset_type_id": "TYPE-560B",
                            "desired_start": DAY.isoformat(),
                            "desired_end": DAY.isoformat(),
                        },
                        {
                            "kind": "WORKFORCE",
                            "desired_start": DAY.isoformat(),
                            "desired_end": DAY.isoformat(),
                            "estimated_hours": 8,
                        },
                    ],
                }
            ),
        )
        self.assertEqual(created.status_code, 201, created.text)
        number = created.json()["demand_number"]
        demand = self.client.get(f"/api/v1/demands/{number}").json()
        approved = self.client.post(
            f"/api/v1/demands/{number}/approve",
            json={"expected_version": demand["version"]},
        )
        self.assertEqual(approved.status_code, 200, approved.text)

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            request = session.scalar(
                select(WorkforceRequest).where(
                    WorkforceRequest.legacy_demand_number == number
                )
            )
            self.assertIsNotNone(request)
            human_requirement = session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.workforce_request_id == request.id
                )
            )
            self.assertIsNotNone(human_requirement)
            request_asset_requirement = session.scalar(
                select(AssetRequirement).where(
                    AssetRequirement.workforce_request_id == request.id,
                    AssetRequirement.origin
                    == AssetRequirementOrigin.REQUEST.value,
                )
            )
            self.assertIsNotNone(request_asset_requirement)
            session.add(
                Shift(
                    id="SHIFT-REQUEST-560B",
                    resource_requirement_id=human_requirement.id,
                    resource_id="RESOURCE-SKILLED",
                    work_date=DAY,
                    hours=Decimal("8"),
                    source="MANUAL",
                    locked=True,
                )
            )
            request_id = request.id
            asset_requirement_id = request_asset_requirement.id
            source_line_id = request_asset_requirement.source_request_line_id
            approval_revision_id = request_asset_requirement.approval_revision_id
            approved_entry_key = request_asset_requirement.approved_entry_key
        engine.dispose()

        assigned = self._set_asset(
            shift_id="SHIFT-REQUEST-560B",
            asset_id="ASSET-A",
            version=self._version(),
            key="request-assign-560b",
            requirement_id=asset_requirement_id,
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        payload = assigned.json()
        self.assertEqual(payload["requirement_origin"], "REQUEST")
        self.assertEqual(payload["requirement_id"], asset_requirement_id)
        self.assertEqual(payload["operator_resource_id"], "RESOURCE-SKILLED")

        changed = self._set_asset(
            shift_id="SHIFT-REQUEST-560B",
            asset_id="ASSET-B",
            version=payload["planning_version"],
            key="request-change-560b",
            requirement_id=asset_requirement_id,
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["requirement_id"], asset_requirement_id)
        self.assertEqual(changed.json()["allocation_id"], payload["allocation_id"])

        released = self._set_asset(
            shift_id="SHIFT-REQUEST-560B",
            asset_id=None,
            version=changed.json()["planning_version"],
            key="request-release-560b",
            requirement_id=asset_requirement_id,
        )
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(released.json()["requirement_origin"], "REQUEST")

        engine = create_sql_engine(self.url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                requirement = session.get(AssetRequirement, asset_requirement_id)
                self.assertIsNotNone(requirement)
                self.assertEqual(requirement.origin, "REQUEST")
                self.assertIsNone(requirement.shift_id)
                self.assertEqual(requirement.workforce_request_id, request_id)
                self.assertEqual(requirement.source_request_line_id, source_line_id)
                self.assertEqual(requirement.approval_revision_id, approval_revision_id)
                self.assertEqual(requirement.approved_entry_key, approved_entry_key)
                self.assertEqual(requirement.status, "À affecter")
                allocation = session.scalar(
                    select(AssetAllocation).where(
                        AssetAllocation.asset_requirement_id
                        == asset_requirement_id
                    )
                )
                self.assertIsNone(allocation)
                ad_hoc = session.scalar(
                    select(AssetRequirement).where(
                        AssetRequirement.origin
                        == AssetRequirementOrigin.SHIFT_AD_HOC.value,
                        AssetRequirement.shift_id == "SHIFT-REQUEST-560B",
                    )
                )
                self.assertIsNone(ad_hoc)
        finally:
            engine.dispose()

    def test_manage_planning_permission_is_required(self) -> None:
        with TestClient(
            create_api_app(
                self.url,
                auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            ),
            raise_server_exceptions=False,
        ) as client:
            response = self._set_asset(
                asset_id="ASSET-A",
                version=self._version(),
                key="permission-560b",
                client=client,
            )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")
        self.assertEqual(
            response.json()["error"]["context"]["required_permission"],
            "manage_planning",
        )
        requirement, allocation, shift = self._ad_hoc_state()
        self.assertIsNone(requirement)
        self.assertIsNone(allocation)
        self.assertEqual(shift.source, "AUTO")
        self.assertFalse(shift.locked)


if __name__ == "__main__":
    unittest.main()
