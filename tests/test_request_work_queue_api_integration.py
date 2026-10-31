from __future__ import annotations

from datetime import date, timedelta, time
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.infrastructure.sql import (
    Asset,
    AssetAllocation,
    AssetType,
    PlanningWorkQueueRelease,
    PlanningWorkQueueRestoration,
    RequestCompletionReduction,
    RequestOperationalState,
    RequestRequirementPlanningIntent,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    RequestApprovalRevision,
    Shift,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    SqlPlanningWindowOverrideRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.planning_audit import PlanningChangeHistory, SqlPlanningAuditJournal
from app.server import create_api_app
from tests.approval_test_support import (
    TEST_ADMIN_USER_ID,
    map_asset_type_to_test_approval_scope,
    routed_demand_payload,
    seed_test_approval_routing,
)
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


START = "2026-11-02"
END = "2026-11-06"
THURSDAY = "2026-11-05"


class RequestWorkQueueApiIntegrationTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P-778-API", number="P-778-API", name="Projet 778 API"))
        session.add_all(
            [
                Resource(
                    id="R-778-A",
                    name="Alice 778 API",
                    active=True,
                    erp_active=True,
                    sort_order=1,
                ),
                Resource(
                    id="R-778-B",
                    name="Bob 778 API",
                    active=True,
                    erp_active=True,
                    sort_order=2,
                ),
            ]
        )
        session.flush()
        for resource_id in ("R-778-A", "R-778-B"):
            session.add(
                ResourceAvailabilityRule(
                    id=f"SCH-{resource_id}",
                    resource_id=resource_id,
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                )
            )
        session.add(
            AssetType(
                id="TYPE-778-API",
                code="VEH-778-API",
                label="Véhicule 778 API",
                category="VEHICLE",
            )
        )
        session.flush()
        session.add_all(
            [
                Asset(
                    id="ASSET-778-A",
                    code="A-778",
                    label="Actif 778 A",
                    asset_type_id="TYPE-778-API",
                ),
                Asset(
                    id="ASSET-778-B",
                    code="B-778",
                    label="Actif 778 B",
                    asset_type_id="TYPE-778-API",
                ),
                Asset(
                    id="ASSET-778-C",
                    code="C-778",
                    label="Actif 778 C",
                    asset_type_id="TYPE-778-API",
                ),
            ]
        )
        seed_test_approval_routing(session, map_existing_tasks=True)
        map_asset_type_to_test_approval_scope(session, "TYPE-778-API")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="request-work-queue-api-integration.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _approve_40(client: TestClient) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": "P-778-API",
                    "submit": True,
                    "lines": [
                        {
                            "desired_start": START,
                            "desired_end": END,
                            "estimated_hours": 40,
                            "confirmation": "Confirmée",
                            "proposed_resource_id": "R-778-A",
                            "description": "Description approuvée source #778",
                        }
                    ],
                }
            ),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "Approbation API #778"},
        )
        assert approved.status_code == 200, approved.text
        return number

    @staticmethod
    def _snapshot(client: TestClient, number: str) -> dict:
        response = client.get(
            "/api/v1/planning/snapshot",
            params={"start": START, "end": END},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        shifts = [row for row in payload["shifts"] if row["demand_number"] == number]
        segments = [row for row in payload["segments"] if row["demand_number"] == number]
        return {**payload, "demand_shifts": shifts, "demand_segments": segments}

    @staticmethod
    def _actions(client: TestClient, number: str) -> list[dict]:
        response = client.get(
            "/api/v1/planning/actions",
            params={"start": START, "end": END},
        )
        assert response.status_code == 200, response.text
        return [
            row
            for row in response.json()
            if row.get("demand_number") == number and row.get("kind") == "ASSIGNMENT"
        ]

    def test_cutoff_rejects_new_same_day_manual_placement_but_allows_survivor_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    snapshot = self._snapshot(client, number)
                    survivor = min(
                        (
                            row
                            for row in snapshot["demand_shifts"]
                            if row["work_date"] < THURSDAY
                        ),
                        key=lambda row: row["work_date"],
                    )
                    requirement_id = snapshot["demand_segments"][0]["requirement_id"]
                    with factory.begin() as session:
                        intent = session.scalar(
                            select(RequestRequirementPlanningIntent).where(
                                RequestRequirementPlanningIntent.current_resource_requirement_id
                                == requirement_id
                            )
                        )
                        self.assertIsNotNone(intent)
                        intent.placement_not_before_date = date.fromisoformat(THURSDAY)

                    base_payload = {
                        "technician": survivor["resource_id"],
                        "day": survivor["work_date"],
                        "outside_standard_hours": False,
                        "confirmation": survivor.get("confirmation"),
                    }
                    increased = client.put(
                        f"/api/v1/allocations/{survivor['allocation_id']}",
                        json={**base_payload, "hours": 12, "note": "augmentation interdite"},
                    )
                    self.assertEqual(increased.status_code, 409, increased.text)
                    self.assertEqual(
                        increased.json()["error"]["code"],
                        "segment_placement_before_cutoff",
                    )

                    changed_resource = client.put(
                        f"/api/v1/allocations/{survivor['allocation_id']}",
                        json={
                            **base_payload,
                            "technician": "R-778-B",
                            "hours": survivor["hours"],
                            "note": "changement ressource interdit",
                        },
                    )
                    self.assertEqual(changed_resource.status_code, 409, changed_resource.text)
                    self.assertEqual(
                        changed_resource.json()["error"]["code"],
                        "segment_placement_before_cutoff",
                    )

                    descriptive = client.put(
                        f"/api/v1/allocations/{survivor['allocation_id']}",
                        json={
                            **base_payload,
                            "hours": survivor["hours"],
                            "note": "survivant inchangé avant cutoff",
                        },
                    )
                    self.assertEqual(descriptive.status_code, 200, descriptive.text)
                    persisted = next(
                        row
                        for row in self._snapshot(client, number)["demand_shifts"]
                        if row["allocation_id"] == survivor["allocation_id"]
                    )
                    self.assertEqual(persisted["resource_id"], survivor["resource_id"])
                    self.assertEqual(persisted["work_date"], survivor["work_date"])
                    self.assertEqual(float(persisted["hours"]), float(survivor["hours"]))
                    self.assertEqual(persisted["note"], "survivant inchangé avant cutoff")

                    at_cutoff = next(
                        row
                        for row in self._snapshot(client, number)["demand_shifts"]
                        if row["work_date"] >= THURSDAY
                    )
                    allowed = client.put(
                        f"/api/v1/allocations/{at_cutoff['allocation_id']}",
                        json={
                            "technician": "R-778-B",
                            "day": at_cutoff["work_date"],
                            "hours": at_cutoff["hours"],
                            "outside_standard_hours": False,
                            "note": "placement au cutoff",
                            "confirmation": at_cutoff.get("confirmation"),
                        },
                    )
                    self.assertEqual(allowed.status_code, 200, allowed.text)
            finally:
                engine.dispose()

    def test_dated_drop_places_only_residual_after_cutoff_and_preserves_release(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    before = self._snapshot(client, number)
                    self.assertEqual(len(before["demand_shifts"]), 5)
                    returned_shift = min(
                        before["demand_shifts"],
                        key=lambda row: row["work_date"],
                    )
                    preview = client.get(
                        f"/api/v1/allocations/{returned_shift['allocation_id']}/work-queue-preview"
                    ).json()
                    returned = client.post(
                        f"/api/v1/allocations/{returned_shift['allocation_id']}/return-to-work-queue",
                        headers={"Idempotency-Key": "777-return-one"},
                        json={
                            "expected_planning_version": preview["planning_version"],
                            "expected_approval_revision_id": preview["approval_revision_id"],
                            "expected_operational_version": preview["operational_version"],
                        },
                    )
                    self.assertEqual(returned.status_code, 200, returned.text)

                    action = self._actions(client, number)[0]
                    segment_id = action["segment_id"]
                    placement_snapshot = self._snapshot(client, number)
                    survivors_before = {
                        row["allocation_id"]: (
                            row["resource_id"],
                            row["work_date"],
                            row["hours"],
                            row["locked"],
                            row["source"],
                        )
                        for row in self._snapshot(client, number)["demand_shifts"]
                        if row["allocation_type"] != "Hors horaire requis"
                    }
                    self.assertEqual(sum(row[2] for row in survivors_before.values()), 32)

                    dropped = client.post(
                        f"/api/v1/segments/{segment_id}/place-from-drop",
                        headers={"Idempotency-Key": "777-drop-bob-thursday"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": THURSDAY,
                            "expected_planning_version": placement_snapshot["planning_version"],
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(dropped.status_code, 200, dropped.text)
                    result = dropped.json()
                    self.assertEqual(result["placement_not_before_date"], THURSDAY)
                    self.assertEqual(result["covered_hours"], 40)
                    self.assertEqual(result["remaining_hours"], 0)
                    self.assertEqual(set(result["preserved_shift_ids"]), set(survivors_before))

                    after = self._snapshot(client, number)
                    counted = [
                        row for row in after["demand_shifts"]
                        if row["allocation_type"] != "Hors horaire requis"
                    ]
                    for allocation_id, survivor in survivors_before.items():
                        current = next(row for row in counted if row["allocation_id"] == allocation_id)
                        self.assertEqual(
                            (
                                current["resource_id"],
                                current["work_date"],
                                current["hours"],
                                current["locked"],
                                current["source"],
                            ),
                            survivor,
                        )
                    new_rows = [
                        row for row in counted
                        if row["allocation_id"] not in survivors_before
                    ]
                    self.assertTrue(new_rows)
                    self.assertTrue(all(row["resource_id"] == "R-778-B" for row in new_rows))
                    self.assertTrue(all(row["work_date"] >= THURSDAY for row in new_rows))
                    self.assertEqual(sum(row["hours"] for row in new_rows), 8)

                    # Same idempotency key replays the first success and adds nothing.
                    replay = client.post(
                        f"/api/v1/segments/{segment_id}/place-from-drop",
                        headers={"Idempotency-Key": "777-drop-bob-thursday"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": THURSDAY,
                            "expected_planning_version": before["planning_version"] + 1,
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(replay.status_code, 200, replay.text)
                    self.assertEqual(replay.json(), result)
                    self.assertEqual(
                        len(self._snapshot(client, number)["demand_shifts"]),
                        len(after["demand_shifts"]),
                    )

                    resource_only = client.post(
                        f"/api/v1/segments/{segment_id}/assign",
                        json={"resource_id": "R-778-B"},
                    )
                    self.assertEqual(resource_only.status_code, 200, resource_only.text)
                    with factory() as session:
                        persisted_intent = session.scalar(
                            select(RequestRequirementPlanningIntent).where(
                                RequestRequirementPlanningIntent.current_resource_requirement_id
                                == result["requirement_id"]
                            )
                        )
                        self.assertEqual(
                            persisted_intent.placement_not_before_date.isoformat(),
                            THURSDAY,
                        )

                    # Restart/resync/rebuild reuse the same durable intent.
                    with factory.begin() as session:
                        SqlPeriodAwareApprovedDemandSyncAdapter(
                            session
                        ).sync_operational_choices(number)
                    rebuilt = client.post("/api/v1/planning/rebuild")
                    self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                    after_resync = self._snapshot(client, number)
                    counted_after_resync = [
                        row
                        for row in after_resync["demand_shifts"]
                        if row["allocation_type"] != "Hors horaire requis"
                    ]
                    self.assertTrue(
                        all(
                            row["allocation_id"] in survivors_before
                            or row["work_date"] >= THURSDAY
                            for row in counted_after_resync
                        )
                    )

                    with factory() as session:
                        intent = session.scalar(
                            select(RequestRequirementPlanningIntent).where(
                                RequestRequirementPlanningIntent.current_resource_requirement_id
                                == result["requirement_id"]
                            )
                        )
                        self.assertIsNotNone(intent)
                        self.assertEqual(intent.placement_not_before_date.isoformat(), THURSDAY)
                        release = session.scalar(
                            select(PlanningWorkQueueRelease).where(
                                PlanningWorkQueueRelease.original_shift_id
                                == returned_shift["allocation_id"]
                            )
                        )
                        # legacy allocation id and Shift id differ; locate the single active release instead.
                        if release is None:
                            release = session.scalar(
                                select(PlanningWorkQueueRelease).where(
                                    PlanningWorkQueueRelease.planning_intent_id == intent.id
                                )
                            )
                        self.assertIsNotNone(release)
                        self.assertTrue(release.active)
            finally:
                engine.dispose()

    def test_dated_drop_rolls_back_target_cutoff_and_rebuild_when_audit_fails(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    target = min(
                        self._snapshot(client, number)["demand_shifts"],
                        key=lambda row: row["work_date"],
                    )
                    preview = client.get(
                        f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                    ).json()
                    returned = client.post(
                        f"/api/v1/allocations/{target['allocation_id']}/return-to-work-queue",
                        headers={"Idempotency-Key": "777-rollback-return"},
                        json={
                            "expected_planning_version": preview["planning_version"],
                            "expected_approval_revision_id": preview["approval_revision_id"],
                            "expected_operational_version": preview["operational_version"],
                        },
                    )
                    self.assertEqual(returned.status_code, 200, returned.text)
                    action = self._actions(client, number)[0]
                    before = self._snapshot(client, number)
                    with factory() as session:
                        intent = session.scalar(
                            select(RequestRequirementPlanningIntent).where(
                                RequestRequirementPlanningIntent.current_resource_requirement_id
                                == returned.json()["requirement_id"]
                            )
                        )
                        before_intent = (
                            intent.intent_mode,
                            intent.target_mode,
                            intent.target_resource_id,
                            intent.placement_not_before_date,
                        )

                    with patch.object(
                        SqlPlanningAuditJournal,
                        "append",
                        side_effect=RuntimeError("777 injected audit failure"),
                    ):
                        failed = client.post(
                            f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                            headers={"Idempotency-Key": "777-rollback-drop"},
                            json={
                                "resource_id": "R-778-B",
                                "dropped_date": THURSDAY,
                                "expected_planning_version": before["planning_version"],
                                "expected_approval_revision_id": action["approval_revision_id"],
                                "expected_operational_version": action["operational_version"],
                            },
                        )
                    self.assertEqual(failed.status_code, 500, failed.text)
                    self.assertEqual(self._snapshot(client, number), before)
                    with factory() as session:
                        intent = session.scalar(
                            select(RequestRequirementPlanningIntent).where(
                                RequestRequirementPlanningIntent.current_resource_requirement_id
                                == returned.json()["requirement_id"]
                            )
                        )
                        self.assertEqual(
                            (
                                intent.intent_mode,
                                intent.target_mode,
                                intent.target_resource_id,
                                intent.placement_not_before_date,
                            ),
                            before_intent,
                        )
            finally:
                engine.dispose()

    def test_dated_drop_preview_reports_authoritative_window_residual_and_placeable_hours(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            engine = create_sql_engine(database_url)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    target = min(
                        self._snapshot(client, number)["demand_shifts"],
                        key=lambda row: row["work_date"],
                    )
                    work_queue = client.get(
                        f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                    ).json()
                    returned = client.post(
                        f"/api/v1/allocations/{target['allocation_id']}/return-to-work-queue",
                        headers={"Idempotency-Key": "777-preview-return"},
                        json={
                            "expected_planning_version": work_queue["planning_version"],
                            "expected_approval_revision_id": work_queue["approval_revision_id"],
                            "expected_operational_version": work_queue["operational_version"],
                        },
                    )
                    self.assertEqual(returned.status_code, 200, returned.text)
                    action = self._actions(client, number)[0]

                    preview = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop/preview",
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": THURSDAY,
                        },
                    )
                    self.assertEqual(preview.status_code, 200, preview.text)
                    payload = preview.json()
                    self.assertTrue(payload["allowed"])
                    self.assertEqual(payload["authorized_start_date"], START)
                    self.assertEqual(payload["authorized_end_date"], END)
                    self.assertEqual(payload["generation_start_date"], THURSDAY)
                    self.assertEqual(payload["generation_end_date"], END)
                    self.assertEqual(payload["effective_need"], 40)
                    self.assertEqual(payload["covered_hours"], 32)
                    self.assertEqual(payload["remaining_hours"], 8)
                    self.assertEqual(payload["placeable_hours"], 8)
                    self.assertEqual(
                        payload["approval_revision_id"],
                        action["approval_revision_id"],
                    )
                    self.assertEqual(
                        payload["operational_version"],
                        action["operational_version"],
                    )

                    outside = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop/preview",
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": "2026-11-07",
                        },
                    )
                    self.assertEqual(outside.status_code, 200, outside.text)
                    refusal = outside.json()
                    self.assertFalse(refusal["allowed"])
                    self.assertEqual(
                        refusal["refusal_code"],
                        "segment_placement_window_extension_required",
                    )
                    self.assertIn(
                        "PROPOSE_WINDOW_EXTENSION",
                        refusal["available_actions"],
                    )
            finally:
                engine.dispose()

    def test_dated_drop_rejects_stale_planning_and_operational_versions_atomically(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            engine = create_sql_engine(database_url)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    target = min(
                        self._snapshot(client, number)["demand_shifts"],
                        key=lambda row: row["work_date"],
                    )
                    preview = client.get(
                        f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                    ).json()
                    returned = client.post(
                        f"/api/v1/allocations/{target['allocation_id']}/return-to-work-queue",
                        headers={"Idempotency-Key": "777-cas-return"},
                        json={
                            "expected_planning_version": preview["planning_version"],
                            "expected_approval_revision_id": preview["approval_revision_id"],
                            "expected_operational_version": preview["operational_version"],
                        },
                    )
                    self.assertEqual(returned.status_code, 200, returned.text)
                    action = self._actions(client, number)[0]
                    before = self._snapshot(client, number)

                    stale_planning = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                        headers={"Idempotency-Key": "777-stale-planning"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": THURSDAY,
                            "expected_planning_version": before["planning_version"] - 1,
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(stale_planning.status_code, 409, stale_planning.text)
                    self.assertEqual(
                        stale_planning.json()["error"]["code"],
                        "planning_version_conflict",
                    )
                    self.assertEqual(self._snapshot(client, number), before)

                    stale_operational = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                        headers={"Idempotency-Key": "777-stale-operational"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": THURSDAY,
                            "expected_planning_version": before["planning_version"],
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"] + 1,
                        },
                    )
                    self.assertEqual(stale_operational.status_code, 409, stale_operational.text)
                    self.assertEqual(
                        stale_operational.json()["error"]["code"],
                        "operational_choice_version_conflict",
                    )
                    self.assertEqual(self._snapshot(client, number), before)
            finally:
                engine.dispose()

    def test_dated_drop_accepts_date_inside_existing_adr022_widened_window(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    target = min(
                        self._snapshot(client, number)["demand_shifts"],
                        key=lambda row: row["work_date"],
                    )
                    preview = client.get(
                        f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                    ).json()
                    returned = client.post(
                        f"/api/v1/allocations/{target['allocation_id']}/return-to-work-queue",
                        headers={"Idempotency-Key": "777-widen-return"},
                        json={
                            "expected_planning_version": preview["planning_version"],
                            "expected_approval_revision_id": preview["approval_revision_id"],
                            "expected_operational_version": preview["operational_version"],
                        },
                    )
                    self.assertEqual(returned.status_code, 200, returned.text)

                    with factory.begin() as session:
                        requirement = session.get(
                            ResourceRequirement,
                            returned.json()["requirement_id"],
                        )
                        intent = session.scalar(
                            select(RequestRequirementPlanningIntent).where(
                                RequestRequirementPlanningIntent.current_resource_requirement_id
                                == requirement.id
                            )
                        )
                        widened_end = requirement.end_date + timedelta(days=1)
                        SqlPlanningWindowOverrideRepository(session).apply_widening(
                            requirement,
                            approval_revision_id=requirement.approval_revision_id,
                            approved_entry_key=intent.approved_entry_key,
                            approved_start_date=requirement.start_date,
                            approved_end_date=requirement.end_date,
                            effective_start_date=requirement.start_date,
                            effective_end_date=widened_end,
                            actor_user_id=TEST_ADMIN_USER_ID,
                            reason="Extension E2E #777",
                            correlation_id="777-existing-window-override",
                        )
                        requirement.end_date = widened_end

                    action = self._actions(client, number)[0]
                    snapshot = self._snapshot(client, number)
                    widened_date = (
                        date.fromisoformat(END) + timedelta(days=1)
                    ).isoformat()
                    placed = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                        headers={"Idempotency-Key": "777-drop-inside-widened"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": widened_date,
                            "expected_planning_version": snapshot["planning_version"],
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(placed.status_code, 200, placed.text)
                    self.assertEqual(
                        placed.json()["placement_not_before_date"],
                        widened_date,
                    )
                    self.assertEqual(
                        placed.json()["authorized_end_date"],
                        widened_date,
                    )
            finally:
                engine.dispose()

    def test_dated_drop_auto_partial_capacity_and_cutoff_replacement(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    approved = self._snapshot(client, number)
                    segment = approved["demand_segments"][0]
                    requirement_id = segment["requirement_id"]
                    with factory.begin() as session:
                        session.execute(
                            Shift.__table__.delete().where(
                                Shift.resource_requirement_id == requirement_id
                            )
                        )
                    action = self._actions(client, number)[0]
                    current = self._snapshot(client, number)
                    placed = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                        headers={"Idempotency-Key": "777-auto-partial"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": THURSDAY,
                            "expected_planning_version": current["planning_version"],
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(placed.status_code, 200, placed.text)
                    result = placed.json()
                    self.assertEqual(result["covered_hours"], 16)
                    self.assertEqual(result["remaining_hours"], 24)
                    counted = [
                        row
                        for row in self._snapshot(client, number)["demand_shifts"]
                        if row["allocation_type"] != "Hors horaire requis"
                    ]
                    self.assertEqual(sum(row["hours"] for row in counted), 16)
                    self.assertTrue(all(row["work_date"] >= THURSDAY for row in counted))
                    survivor_ids = {row["allocation_id"] for row in counted}
                    with factory() as session:
                        intent = session.scalar(
                            select(RequestRequirementPlanningIntent).where(
                                RequestRequirementPlanningIntent.current_resource_requirement_id
                                == requirement_id
                            )
                        )
                        self.assertEqual(intent.intent_mode, "AUTO")
                        self.assertEqual(intent.placement_not_before_date.isoformat(), THURSDAY)

                    # A later dated drop replaces, rather than max-merges, the cutoff.
                    action = self._actions(client, number)[0]
                    current = self._snapshot(client, number)
                    later = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                        headers={"Idempotency-Key": "777-cutoff-later"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": END,
                            "expected_planning_version": current["planning_version"],
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(later.status_code, 200, later.text)
                    self.assertEqual(later.json()["placement_not_before_date"], END)
                    after_later = [
                        row
                        for row in self._snapshot(client, number)["demand_shifts"]
                        if row["allocation_type"] != "Hors horaire requis"
                    ]
                    self.assertEqual(
                        {row["allocation_id"] for row in after_later},
                        survivor_ids,
                    )

                    action = self._actions(client, number)[0]
                    current = self._snapshot(client, number)
                    earlier = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                        headers={"Idempotency-Key": "777-cutoff-earlier"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": THURSDAY,
                            "expected_planning_version": current["planning_version"],
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(earlier.status_code, 200, earlier.text)
                    self.assertEqual(earlier.json()["placement_not_before_date"], THURSDAY)
                    self.assertEqual(
                        {
                            row["allocation_id"]
                            for row in self._snapshot(client, number)["demand_shifts"]
                            if row["allocation_type"] != "Hors horaire requis"
                        },
                        survivor_ids,
                    )
                    with factory() as session:
                        intent = session.scalar(
                            select(RequestRequirementPlanningIntent).where(
                                RequestRequirementPlanningIntent.current_resource_requirement_id
                                == requirement_id
                            )
                        )
                        self.assertEqual(intent.intent_mode, "PRESERVE_EXISTING")
                        self.assertEqual(intent.placement_not_before_date.isoformat(), THURSDAY)
            finally:
                engine.dispose()

    def test_dated_drop_rejects_before_window_and_explicit_reset_is_non_regenerating(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            engine = create_sql_engine(database_url)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    first = min(self._snapshot(client, number)["demand_shifts"], key=lambda row: row["work_date"])
                    preview = client.get(
                        f"/api/v1/allocations/{first['allocation_id']}/work-queue-preview"
                    ).json()
                    self.assertEqual(
                        client.post(
                            f"/api/v1/allocations/{first['allocation_id']}/return-to-work-queue",
                            headers={"Idempotency-Key": "777-return-reset"},
                            json={
                                "expected_planning_version": preview["planning_version"],
                                "expected_approval_revision_id": preview["approval_revision_id"],
                                "expected_operational_version": preview["operational_version"],
                            },
                        ).status_code,
                        200,
                    )
                    action = self._actions(client, number)[0]
                    snap = self._snapshot(client, number)
                    early = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                        headers={"Idempotency-Key": "777-too-early"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": "2026-11-01",
                            "expected_planning_version": snap["planning_version"],
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(early.status_code, 409, early.text)
                    self.assertEqual(
                        early.json()["error"]["code"],
                        "segment_placement_before_authorized_window",
                    )

                    late = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                        headers={"Idempotency-Key": "777-too-late"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": "2026-11-07",
                            "expected_planning_version": snap["planning_version"],
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(late.status_code, 409, late.text)
                    self.assertEqual(
                        late.json()["error"]["code"],
                        "segment_placement_window_extension_required",
                    )
                    self.assertIn(
                        "PROPOSE_WINDOW_EXTENSION",
                        late.json()["error"]["context"]["available_actions"],
                    )

                    placed = client.post(
                        f"/api/v1/segments/{action['segment_id']}/place-from-drop",
                        headers={"Idempotency-Key": "777-valid-cutoff"},
                        json={
                            "resource_id": "R-778-B",
                            "dropped_date": THURSDAY,
                            "expected_planning_version": snap["planning_version"],
                            "expected_approval_revision_id": action["approval_revision_id"],
                            "expected_operational_version": action["operational_version"],
                        },
                    )
                    self.assertEqual(placed.status_code, 200, placed.text)

                    # Return Segment retains the cutoff and enters QUEUED.
                    current_shift = self._snapshot(client, number)["demand_shifts"][0]
                    current_preview = client.get(
                        f"/api/v1/allocations/{current_shift['allocation_id']}/work-queue-preview"
                    ).json()
                    returned_segment = client.post(
                        f"/api/v1/segments/{action['segment_id']}/return-to-work-queue",
                        headers={"Idempotency-Key": "777-return-segment"},
                        json={
                            "expected_planning_version": current_preview["planning_version"],
                            "expected_approval_revision_id": current_preview["approval_revision_id"],
                            "expected_operational_version": current_preview["operational_version"],
                        },
                    )
                    self.assertEqual(returned_segment.status_code, 200, returned_segment.text)
                    queued_action = self._actions(client, number)[0]
                    self.assertEqual(
                        queued_action["placement_not_before_date"],
                        THURSDAY,
                    )
                    queued_snapshot = self._snapshot(client, number)
                    self.assertEqual(queued_snapshot["demand_shifts"], [])

                    reset = client.post(
                        f"/api/v1/segments/{action['segment_id']}/placement-cutoff/reset",
                        headers={"Idempotency-Key": "777-reset-cutoff"},
                        json={
                            "expected_planning_version": queued_snapshot["planning_version"],
                            "expected_approval_revision_id": queued_action["approval_revision_id"],
                            "expected_operational_version": queued_action["operational_version"],
                        },
                    )
                    self.assertEqual(reset.status_code, 200, reset.text)
                    self.assertIsNone(reset.json()["placement_not_before_date"])
                    self.assertEqual(self._snapshot(client, number)["demand_shifts"], [])
                    with create_session_factory(engine)() as session:
                        intent = session.scalar(
                            select(RequestRequirementPlanningIntent).where(
                                RequestRequirementPlanningIntent.current_resource_requirement_id
                                == reset.json()["requirement_id"]
                            )
                        )
                        self.assertEqual(intent.intent_mode, "QUEUED")
                        self.assertIsNone(intent.placement_not_before_date)
            finally:
                engine.dispose()

    def test_demand_update_validates_all_preserved_survivors_and_reuses_canonical_exception(self) -> None:
        for survivor_kind in ("unlocked", "manual", "asset"):
            with self.subTest(survivors=survivor_kind), TemporaryDirectory() as directory:
                database_url = self._database(directory)
                app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                try:
                    with TestClient(app, raise_server_exceptions=False) as client:
                        number = self._approve_40(client)
                        target = self._snapshot(client, number)["demand_shifts"][0]
                        segment_id = target["requirement_id"]
                        preview = client.get(f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview").json()
                        returned = client.post(
                            f"/api/v1/allocations/{target['allocation_id']}/return-to-work-queue",
                            headers={"Idempotency-Key": f"demand-return-{survivor_kind}"},
                            json={"expected_planning_version": preview["planning_version"],
                                  "expected_approval_revision_id": preview["approval_revision_id"],
                                  "expected_operational_version": preview["operational_version"]},
                        )
                        self.assertEqual(returned.status_code, 200, returned.text)
                        if survivor_kind == "manual":
                            with factory.begin() as session:
                                for shift in session.scalars(select(Shift).where(Shift.resource_requirement_id == segment_id)):
                                    shift.locked, shift.source = True, "MANUAL"
                        if survivor_kind == "asset":
                            shifts = self._snapshot(client, number)["demand_shifts"]
                            for index, shift in enumerate(shifts):
                                version = client.get("/api/v1/assets/requirements").json()["planning_version"]
                                assigned = client.put(
                                    f"/api/v1/assets/shifts/{shift['allocation_id']}/assignment",
                                    headers={"Idempotency-Key": f"demand-asset-{index}"},
                                    json={"asset_id": "ASSET-778-A", "expected_planning_version": version},
                                )
                                self.assertEqual(assigned.status_code, 200, assigned.text)
                        before = self._snapshot(client, number)
                        self.assertEqual(before["demand_segments"][0]["covered_hours"], 32)
                        survivor_state = [(row["allocation_id"], row["resource_id"], row["work_date"],
                                           row["hours"], row["source"], row["locked"])
                                          for row in before["demand_shifts"]]
                        demand = client.get(f"/api/v1/demands/{number}").json()
                        line_id = demand["lines"][0]["line_id"]

                        def update_hours(hours):
                            current = client.get(f"/api/v1/demands/{number}").json()
                            return client.patch(f"/api/v1/demands/{number}", json=routed_demand_payload({
                                "expected_version": current["version"],
                                "lines": [{"id": line_id, "desired_start": START, "desired_end": END,
                                           "estimated_hours": hours, "confirmation": "Confirmée",
                                           "proposed_resource_id": "R-778-A",
                                           "description": "Description approuvée source #778"}],
                            }))

                        denied = update_hours(16)
                        self.assertEqual(denied.status_code, 422, denied.text)
                        error = denied.json()["error"]
                        self.assertEqual(error["code"], "segment_overallocation_choice_required")
                        self.assertEqual(error["context"]["planned_hours"], 16)
                        self.assertEqual(error["context"]["current_excess_hours"], 0)
                        self.assertEqual(error["context"]["excess_hours"], 16)
                        self.assertEqual(client.get(f"/api/v1/demands/{number}").json(), demand)
                        self.assertEqual(self._snapshot(client, number), before)

                        # Acknowledge through the existing authorized/audited Segment workflow.
                        acknowledged = client.patch(f"/api/v1/segments/{segment_id}", json={
                            "planned_hours": 16, "allow_locked_overallocation": True,
                        })
                        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
                        for hours, excess in ((16, 16), (20, 12)):
                            saved = update_hours(hours)
                            self.assertEqual(saved.status_code, 200, saved.text)
                            current = self._snapshot(client, number)
                            self.assertEqual(current["demand_segments"][0]["planned_hours"], hours)
                            self.assertEqual(current["demand_segments"][0]["excess_hours"], excess)
                        increased = update_hours(12)
                        self.assertEqual(increased.status_code, 422, increased.text)
                        self.assertEqual(increased.json()["error"]["context"]["current_excess_hours"], 12)
                        self.assertEqual(increased.json()["error"]["context"]["excess_hours"], 20)
                        with factory.begin() as session:
                            SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_operational_choices(number)
                        rebuilt = client.post("/api/v1/planning/rebuild")
                        self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                        after = self._snapshot(client, number)
                        self.assertEqual([(row["allocation_id"], row["resource_id"], row["work_date"],
                                           row["hours"], row["source"], row["locked"])
                                          for row in after["demand_shifts"]], survivor_state)
                        with factory() as session:
                            events = session.scalars(select(PlanningChangeHistory).where(
                                PlanningChangeHistory.action == "Dérogation surallocation manuelle"
                            )).all()
                            self.assertEqual(len(events), 1)
                            if survivor_kind == "asset":
                                self.assertEqual(session.scalar(select(func.count()).select_from(AssetAllocation)), 4)
                finally:
                    engine.dispose()

    def test_return_segment_audits_each_assignment_atomically_and_replay_does_not_duplicate(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    initial = self._snapshot(client, number)
                    segment_id = initial["demand_shifts"][0]["requirement_id"]
                    with factory.begin() as session:
                        shifts = session.scalars(select(Shift).where(
                            Shift.resource_requirement_id == segment_id
                        ).order_by(Shift.work_date, Shift.id)).all()
                        shifts[0].hours, shifts[1].hours = Decimal("4"), Decimal("12")
                        shifts[0].resource_id, shifts[0].source, shifts[0].locked = "R-778-B", "MANUAL", True
                        shifts[0].confirmation, shifts[0].note = "Tentative", "Assignment removed by segment return"
                        expected = {row.id: {"allocation_id": row.legacy_allocation_id or row.id,
                                    "resource_id": row.resource_id, "work_date": row.work_date.isoformat(),
                                    "hours": float(row.hours), "source": row.source, "locked": row.locked,
                                    "confirmation": row.confirmation, "note": row.note,
                                    "outside_standard_hours": row.outside_standard_hours,
                                    "allocation_type": row.allocation_type}
                                    for row in shifts}
                        history_before = set(session.scalars(select(PlanningChangeHistory.id)).all())
                    before = self._snapshot(client, number)
                    target = before["demand_shifts"][0]["allocation_id"]
                    preview = client.get(f"/api/v1/allocations/{target}/work-queue-preview").json()
                    payload = {"expected_planning_version": preview["planning_version"],
                               "expected_approval_revision_id": preview["approval_revision_id"],
                               "expected_operational_version": preview["operational_version"]}
                    url = f"/api/v1/segments/{segment_id}/return-to-work-queue"
                    headers = {"Idempotency-Key": "segment-audit-atomic-replay"}
                    append = SqlPlanningAuditJournal.append
                    seen = []

                    def fail_after_second_shift(journal, **kwargs):
                        append(journal, **kwargs)
                        if kwargs["entity_type"] == "SHIFT":
                            seen.append(kwargs["entity_id"])
                            if len(seen) == 2:
                                raise RuntimeError("Injected audit failure")

                    with patch.object(SqlPlanningAuditJournal, "append", fail_after_second_shift):
                        failed = client.post(url, headers=headers, json=payload)
                    self.assertEqual(failed.status_code, 500, failed.text)
                    self.assertEqual(len(seen), 2)
                    self.assertEqual(self._snapshot(client, number), before)
                    with factory() as session:
                        self.assertEqual(set(session.scalars(select(PlanningChangeHistory.id)).all()), history_before)
                    returned = client.post(url, headers=headers, json=payload)
                    self.assertEqual(returned.status_code, 200, returned.text)
                    self.assertEqual(returned.json()["removed_shift_count"], 5)
                    replay = client.post(url, headers=headers, json=payload)
                    self.assertEqual(replay.status_code, 200, replay.text)
                    self.assertEqual(replay.json(), returned.json())
                    self.assertEqual(self._snapshot(client, number)["demand_shifts"], [])
                    with factory() as session:
                        history = session.scalars(select(PlanningChangeHistory).where(
                            PlanningChangeHistory.action == "Retour segment à la File de Travail",
                            PlanningChangeHistory.entity_type == "SHIFT",
                        )).all()
                        self.assertEqual(len(history), 5)
                        self.assertEqual({row.entity_id for row in history}, set(expected))
                        for row in history:
                            snapshot = json.loads(row.details)["before"]
                            for field, value in expected[row.entity_id].items():
                                self.assertEqual(snapshot[field], value, field)
                            self.assertEqual(row.parent_reference, snapshot["segment_id"])
                            self.assertTrue(snapshot["technician"])
            finally:
                engine.dispose()

    def _prepare_reduced_returned_24(
        self,
        client: TestClient,
        *,
        key_prefix: str,
    ) -> tuple[str, str, list[dict]]:
        number = self._approve_40(client)
        before = self._snapshot(client, number)
        reduced_target = next(
            row for row in before["demand_shifts"]
            if row["work_date"] == THURSDAY
        )
        segment_id = reduced_target["requirement_id"] or reduced_target["segment_id"]
        reduction_preview = client.get(
            f"/api/v1/allocations/{reduced_target['allocation_id']}/work-queue-preview"
        ).json()
        reduced = client.post(
            f"/api/v1/allocations/{reduced_target['allocation_id']}/permanent-reduction",
            headers={"Idempotency-Key": f"{key_prefix}-reduce"},
            json={
                "expected_planning_version": reduction_preview["planning_version"],
                "expected_request_version": reduction_preview["request_version"],
                "expected_approval_revision_id": reduction_preview["approval_revision_id"],
                "expected_operational_version": reduction_preview["operational_version"],
            },
        )
        self.assertEqual(reduced.status_code, 200, reduced.text)
        after_reduction = self._snapshot(client, number)
        returned_target = next(
            row for row in after_reduction["demand_shifts"]
            if row["work_date"] != THURSDAY
        )
        return_preview = client.get(
            f"/api/v1/allocations/{returned_target['allocation_id']}/work-queue-preview"
        ).json()
        returned = client.post(
            f"/api/v1/allocations/{returned_target['allocation_id']}/return-to-work-queue",
            headers={"Idempotency-Key": f"{key_prefix}-return"},
            json={
                "expected_planning_version": return_preview["planning_version"],
                "expected_approval_revision_id": return_preview["approval_revision_id"],
                "expected_operational_version": return_preview["operational_version"],
            },
        )
        self.assertEqual(returned.status_code, 200, returned.text)
        snapshot = self._snapshot(client, number)
        self.assertEqual(snapshot["demand_segments"][0]["planned_hours"], 32.0)
        self.assertEqual(snapshot["demand_segments"][0]["covered_hours"], 24.0)
        return number, segment_id, snapshot["demand_shifts"]

    def test_a_return_shift_preserves_40_exposes_8_and_allows_controlled_bob_placement(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(
                self._database(directory),
                actor_name="admin-778-api-a",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve_40(client)
                before = self._snapshot(client, number)
                self.assertEqual(len(before["demand_segments"]), 1)
                self.assertEqual(len(before["demand_shifts"]), 5)
                self.assertEqual(
                    sum(row["hours"] for row in before["demand_shifts"]),
                    40.0,
                )
                target = next(
                    row for row in before["demand_shifts"]
                    if row["work_date"] == THURSDAY
                )
                segment_id = target["requirement_id"] or target["segment_id"]

                preview = client.get(
                    f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                ).json()
                returned = client.post(
                    f"/api/v1/allocations/{target['allocation_id']}/return-to-work-queue",
                    headers={"Idempotency-Key": "api-a-return-shift-778"},
                    json={
                        "expected_planning_version": preview["planning_version"],
                        "expected_approval_revision_id": preview["approval_revision_id"],
                        "expected_operational_version": preview["operational_version"],
                    },
                )
                self.assertEqual(returned.status_code, 200, returned.text)

                after = self._snapshot(client, number)
                self.assertEqual(len(after["demand_shifts"]), 4)
                self.assertEqual(
                    sum(row["hours"] for row in after["demand_shifts"]),
                    32.0,
                )
                segment = after["demand_segments"][0]
                self.assertEqual(segment["planned_hours"], 40.0)
                self.assertEqual(segment["covered_hours"], 32.0)
                self.assertEqual(segment["remaining_hours"], 8.0)
                actions = self._actions(client, number)
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]["planned_hours"], 8.0)

                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                after_rebuild = self._snapshot(client, number)
                self.assertEqual(len(after_rebuild["demand_shifts"]), 4)
                self.assertFalse(
                    any(
                        row["resource_id"] == "R-778-A"
                        and row["work_date"] == THURSDAY
                        for row in after_rebuild["demand_shifts"]
                    )
                )

                placed = client.post(
                    f"/api/v1/segments/{segment_id}/assign",
                    json={"resource_id": "R-778-B"},
                )
                self.assertEqual(placed.status_code, 200, placed.text)
                final = self._snapshot(client, number)
                self.assertEqual(
                    sum(row["hours"] for row in final["demand_shifts"]),
                    40.0,
                )
                self.assertTrue(
                    any(row["resource_id"] == "R-778-B" for row in final["demand_shifts"])
                )
                self.assertEqual(self._actions(client, number), [])

    def test_b_return_segment_exposes_full_40_and_explicit_bob_placement_materializes(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(
                self._database(directory),
                actor_name="admin-778-api-b",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve_40(client)
                before = self._snapshot(client, number)
                target = before["demand_shifts"][0]
                segment_id = target["requirement_id"] or target["segment_id"]
                preview = client.get(
                    f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                ).json()

                returned = client.post(
                    f"/api/v1/segments/{segment_id}/return-to-work-queue",
                    headers={"Idempotency-Key": "api-b-return-segment-778"},
                    json={
                        "expected_planning_version": preview["planning_version"],
                        "expected_approval_revision_id": preview["approval_revision_id"],
                        "expected_operational_version": preview["operational_version"],
                    },
                )
                self.assertEqual(returned.status_code, 200, returned.text)
                after = self._snapshot(client, number)
                self.assertEqual(after["demand_shifts"], [])
                segment = after["demand_segments"][0]
                self.assertEqual(segment["planned_hours"], 40.0)
                self.assertEqual(segment["covered_hours"], 0.0)
                self.assertEqual(segment["remaining_hours"], 40.0)
                self.assertEqual(self._actions(client, number)[0]["planned_hours"], 40.0)

                placed = client.post(
                    f"/api/v1/segments/{segment_id}/assign",
                    json={"resource_id": "R-778-B"},
                )
                self.assertEqual(placed.status_code, 200, placed.text)
                final = self._snapshot(client, number)
                self.assertEqual(
                    sum(row["hours"] for row in final["demand_shifts"]),
                    40.0,
                )
                self.assertEqual(
                    {row["resource_id"] for row in final["demand_shifts"]},
                    {"R-778-B"},
                )
                self.assertEqual(self._actions(client, number), [])

    def test_resolved_release_still_bounds_fresh_placement_and_keep_exception_is_canonical(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-api-resolved",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve_40(client)
                before = self._snapshot(client, number)
                target = next(
                    row for row in before["demand_shifts"]
                    if row["work_date"] == THURSDAY
                )
                segment_id = target["requirement_id"] or target["segment_id"]
                preview = client.get(
                    f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                ).json()
                returned = client.post(
                    f"/api/v1/allocations/{target['allocation_id']}/return-to-work-queue",
                    headers={"Idempotency-Key": "api-resolved-return-778"},
                    json={
                        "expected_planning_version": preview["planning_version"],
                        "expected_approval_revision_id": preview["approval_revision_id"],
                        "expected_operational_version": preview["operational_version"],
                    },
                )
                self.assertEqual(returned.status_code, 200, returned.text)

                restored = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    headers={"Idempotency-Key": "api-resolved-restore-778"},
                    json={
                        "resource_id": "R-778-A",
                        "day": THURSDAY,
                        "hours": 8,
                    },
                )
                self.assertEqual(restored.status_code, 201, restored.text)

                blocked = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    headers={"Idempotency-Key": "api-resolved-blocked-778"},
                    json={
                        "resource_id": "R-778-A",
                        "day": THURSDAY,
                        "hours": 4,
                    },
                )
                self.assertEqual(blocked.status_code, 409, blocked.text)
                self.assertEqual(
                    blocked.json()["error"]["code"],
                    "request_work_queue_placement_exceeds_residual",
                )
                full = self._snapshot(client, number)
                self.assertEqual(
                    sum(row["hours"] for row in full["demand_shifts"]),
                    40.0,
                )

                kept = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    headers={"Idempotency-Key": "api-resolved-keep-778"},
                    json={
                        "resource_id": "R-778-A",
                        "day": THURSDAY,
                        "hours": 4,
                        "overallocation_policy": "KEEP_EXCEPTION",
                    },
                )
                self.assertEqual(kept.status_code, 201, kept.text)
                over = self._snapshot(client, number)
                self.assertEqual(
                    sum(row["hours"] for row in over["demand_shifts"]),
                    44.0,
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    releases = session.scalars(
                        select(PlanningWorkQueueRelease).order_by(
                            PlanningWorkQueueRelease.created_at,
                            PlanningWorkQueueRelease.id,
                        )
                    ).all()
                    self.assertEqual(len(releases), 1)
                    self.assertFalse(releases[0].active)
                    restored_hours = session.scalar(
                        select(
                            func.coalesce(
                                func.sum(PlanningWorkQueueRestoration.restored_hours),
                                0,
                            )
                        ).where(
                            PlanningWorkQueueRestoration.release_id == releases[0].id
                        )
                    )
                    self.assertEqual(Decimal(str(restored_hours)), Decimal("8.00"))
            finally:
                engine.dispose()

    def test_return_segment_full_assignment_rejects_fresh_ordinary_placement(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(
                self._database(directory),
                actor_name="admin-778-api-return-segment-bound",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve_40(client)
                before = self._snapshot(client, number)
                target = before["demand_shifts"][0]
                segment_id = target["requirement_id"] or target["segment_id"]
                preview = client.get(
                    f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                ).json()
                returned = client.post(
                    f"/api/v1/segments/{segment_id}/return-to-work-queue",
                    headers={"Idempotency-Key": "api-return-segment-bound-778"},
                    json={
                        "expected_planning_version": preview["planning_version"],
                        "expected_approval_revision_id": preview["approval_revision_id"],
                        "expected_operational_version": preview["operational_version"],
                    },
                )
                self.assertEqual(returned.status_code, 200, returned.text)
                assigned = client.post(
                    f"/api/v1/segments/{segment_id}/assign",
                    json={"resource_id": "R-778-B"},
                )
                self.assertEqual(assigned.status_code, 200, assigned.text)

                blocked = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    headers={"Idempotency-Key": "api-return-segment-fresh-778"},
                    json={
                        "resource_id": "R-778-B",
                        "day": START,
                        "hours": 4,
                    },
                )
                self.assertEqual(blocked.status_code, 409, blocked.text)
                self.assertEqual(
                    blocked.json()["error"]["code"],
                    "request_work_queue_placement_exceeds_residual",
                )
                final = self._snapshot(client, number)
                self.assertEqual(
                    sum(row["hours"] for row in final["demand_shifts"]),
                    40.0,
                )

    def test_partial_restorations_accumulate_on_one_release_and_replay_is_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-api-partial-release",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve_40(client)
                before = self._snapshot(client, number)
                target = next(
                    row for row in before["demand_shifts"]
                    if row["work_date"] == THURSDAY
                )
                segment_id = target["requirement_id"] or target["segment_id"]
                preview = client.get(
                    f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                ).json()
                returned = client.post(
                    f"/api/v1/allocations/{target['allocation_id']}/return-to-work-queue",
                    headers={"Idempotency-Key": "api-partial-return-778"},
                    json={
                        "expected_planning_version": preview["planning_version"],
                        "expected_approval_revision_id": preview["approval_revision_id"],
                        "expected_operational_version": preview["operational_version"],
                    },
                )
                self.assertEqual(returned.status_code, 200, returned.text)

                first_payload = {
                    "resource_id": "R-778-A",
                    "day": THURSDAY,
                    "hours": 4,
                }
                first = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    headers={"Idempotency-Key": "api-partial-first-778"},
                    json=first_payload,
                )
                self.assertEqual(first.status_code, 201, first.text)
                replay = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    headers={"Idempotency-Key": "api-partial-first-778"},
                    json=first_payload,
                )
                self.assertEqual(replay.status_code, 201, replay.text)
                self.assertEqual(replay.json(), first.json())

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    release_a = session.scalar(
                        select(PlanningWorkQueueRelease).order_by(
                            PlanningWorkQueueRelease.created_at,
                            PlanningWorkQueueRelease.id,
                        )
                    )
                    self.assertIsNotNone(release_a)
                    self.assertTrue(release_a.active)
                    restored = session.scalars(
                        select(PlanningWorkQueueRestoration).where(
                            PlanningWorkQueueRestoration.release_id == release_a.id
                        )
                    ).all()
                    self.assertEqual(len(restored), 1)
                    self.assertEqual(
                        sum((row.restored_hours for row in restored), Decimal("0")),
                        Decimal("4.00"),
                    )
                    sibling = PlanningWorkQueueRelease(
                        planning_intent_id=release_a.planning_intent_id,
                        resource_requirement_id=release_a.resource_requirement_id,
                        original_shift_id="SIBLING-RELEASE-SHIFT-778",
                        workforce_request_id=release_a.workforce_request_id,
                        approval_revision_id=release_a.approval_revision_id,
                        approved_entry_key=release_a.approved_entry_key,
                        resource_id=release_a.resource_id,
                        work_date=release_a.work_date,
                        hours=Decimal("4.00"),
                        shift_snapshot_text="{}",
                        actor_user_id=TEST_ADMIN_USER_ID,
                        command_id="api-partial-sibling-778",
                        active=True,
                    )
                    session.add(sibling)
                    session.flush()

                    restored_shift_id = restored[0].shift_id
                    sibling_source = session.scalar(
                        select(Shift)
                        .where(
                            Shift.resource_requirement_id
                            == release_a.resource_requirement_id,
                            Shift.id != restored_shift_id,
                            Shift.hours == Decimal("8.00"),
                        )
                        .order_by(Shift.work_date, Shift.id)
                    )
                    self.assertIsNotNone(sibling_source)
                    sibling_source.hours = Decimal("4.00")
                    session.flush()

                    sibling_id = sibling.id
                    release_a_id = release_a.id
                    shift_ids_before_rejection = set(
                        session.scalars(
                            select(Shift.id).where(
                                Shift.resource_requirement_id
                                == release_a.resource_requirement_id
                            )
                        ).all()
                    )
            finally:
                engine.dispose()

            blocked_payload = {
                "resource_id": "R-778-A",
                "day": THURSDAY,
                "hours": 8,
            }
            with TestClient(app, raise_server_exceptions=False) as client:
                blocked = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    headers={"Idempotency-Key": "api-partial-over-release-778"},
                    json=blocked_payload,
                )
                self.assertEqual(blocked.status_code, 409, blocked.text)
                self.assertEqual(
                    blocked.json()["error"]["code"],
                    "request_work_queue_restoration_exceeds_release_outstanding",
                )
                replay_blocked = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    headers={"Idempotency-Key": "api-partial-over-release-778"},
                    json=blocked_payload,
                )
                self.assertEqual(replay_blocked.status_code, 409, replay_blocked.text)
                self.assertEqual(
                    replay_blocked.json()["error"]["code"],
                    "request_work_queue_restoration_exceeds_release_outstanding",
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    release_a = session.get(PlanningWorkQueueRelease, release_a_id)
                    sibling = session.get(PlanningWorkQueueRelease, sibling_id)
                    self.assertTrue(release_a.active)
                    self.assertTrue(sibling.active)
                    restored = session.scalars(
                        select(PlanningWorkQueueRestoration).where(
                            PlanningWorkQueueRestoration.release_id == release_a_id
                        )
                    ).all()
                    self.assertEqual(
                        sum((row.restored_hours for row in restored), Decimal("0")),
                        Decimal("4.00"),
                    )
                    self.assertEqual(
                        set(
                            session.scalars(
                                select(Shift.id).where(
                                    Shift.resource_requirement_id
                                    == release_a.resource_requirement_id
                                )
                            ).all()
                        ),
                        shift_ids_before_rejection,
                    )
            finally:
                engine.dispose()

            with TestClient(app, raise_server_exceptions=False) as client:
                second = client.post(
                    f"/api/v1/segments/{segment_id}/allocations",
                    headers={"Idempotency-Key": "api-partial-second-778"},
                    json={
                        "resource_id": "R-778-A",
                        "day": THURSDAY,
                        "hours": 4,
                    },
                )
                self.assertEqual(second.status_code, 201, second.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    release_a = session.get(PlanningWorkQueueRelease, release_a_id)
                    sibling = session.get(PlanningWorkQueueRelease, sibling_id)
                    self.assertFalse(release_a.active)
                    self.assertTrue(sibling.active)
                    restored = session.scalars(
                        select(PlanningWorkQueueRestoration).where(
                            PlanningWorkQueueRestoration.release_id == release_a_id
                        )
                    ).all()
                    self.assertEqual(len(restored), 2)
                    self.assertEqual(
                        sum((row.restored_hours for row in restored), Decimal("0")),
                        Decimal("8.00"),
                    )
                    sibling_restorations = session.scalars(
                        select(PlanningWorkQueueRestoration).where(
                            PlanningWorkQueueRestoration.release_id == sibling_id
                        )
                    ).all()
                    self.assertEqual(sibling_restorations, [])
            finally:
                engine.dispose()

    def test_public_segment_patch_materializes_base_minus_active_reduction(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-api-patch-effective",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve_40(client)
                before = self._snapshot(client, number)
                target = next(
                    row for row in before["demand_shifts"]
                    if row["work_date"] == THURSDAY
                )
                segment_id = target["requirement_id"] or target["segment_id"]
                preview = client.get(
                    f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                ).json()
                reduced = client.post(
                    f"/api/v1/allocations/{target['allocation_id']}/permanent-reduction",
                    headers={"Idempotency-Key": "api-patch-reduce-778"},
                    json={
                        "expected_planning_version": preview["planning_version"],
                        "expected_request_version": preview["request_version"],
                        "expected_approval_revision_id": preview["approval_revision_id"],
                        "expected_operational_version": preview["operational_version"],
                    },
                )
                self.assertEqual(reduced.status_code, 200, reduced.text)

                current_before_edit = client.get(f"/api/v1/segments/{segment_id}")
                self.assertEqual(
                    current_before_edit.status_code,
                    200,
                    current_before_edit.text,
                )
                self.assertEqual(current_before_edit.json()["planned_hours"], 32.0)
                self.assertEqual(
                    current_before_edit.json()["operational_base_hours"],
                    40.0,
                )

                description_only = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={
                        "planned_hours": 40,
                        "description": "Description seulement après réduction",
                    },
                )
                self.assertEqual(
                    description_only.status_code,
                    200,
                    description_only.text,
                )
                after_description = client.get(f"/api/v1/segments/{segment_id}")
                self.assertEqual(after_description.status_code, 200, after_description.text)
                self.assertEqual(after_description.json()["planned_hours"], 32.0)
                self.assertEqual(
                    after_description.json()["operational_base_hours"],
                    40.0,
                )
                self.assertEqual(
                    after_description.json()["description"],
                    "Description seulement après réduction",
                )

                patched = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 36},
                )
                self.assertEqual(patched.status_code, 200, patched.text)

                current = client.get(f"/api/v1/segments/{segment_id}")
                self.assertEqual(current.status_code, 200, current.text)
                self.assertEqual(current.json()["planned_hours"], 28.0)
                self.assertEqual(current.json()["operational_base_hours"], 36.0)
                snapshot = self._snapshot(client, number)
                segment = snapshot["demand_segments"][0]
                self.assertEqual(segment["planned_hours"], 28.0)
                self.assertEqual(segment["covered_hours"], 28.0)
                self.assertEqual(segment["remaining_hours"], 0.0)
                self.assertEqual(self._actions(client, number), [])
                detail = client.get(f"/api/v1/demands/{number}/detail")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(
                    float(detail.json()["materialized_plan"]["planned_hours"]),
                    28.0,
                )
                self.assertEqual(
                    float(detail.json()["materialized_plan"]["covered_hours"]),
                    28.0,
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
                    self.assertIsNotNone(request)
                    revision = session.scalar(
                        select(RequestApprovalRevision)
                        .where(
                            RequestApprovalRevision.workforce_request_id == request.id
                        )
                        .order_by(RequestApprovalRevision.created_at)
                    )
                    payload = json.loads(revision.payload_text)
                    self.assertEqual(
                        sum(
                            Decimal(str(row["hours"]))
                            for row in payload["authorization"]["entries"]
                            if row.get("hours") is not None
                        ),
                        Decimal("40.00"),
                    )
                    reductions = session.scalars(
                        select(RequestCompletionReduction).where(
                            RequestCompletionReduction.workforce_request_id
                            == request.id
                        )
                    ).all()
                    self.assertEqual(len(reductions), 1)
                    self.assertEqual(reductions[0].reconciliation_state, "ACTIVE")
                    state = session.get(
                        RequestOperationalState,
                        request.id,
                    )
                    self.assertIsNotNone(state)
                    overrides = json.loads(state.budget_overrides_text)
                    self.assertEqual(
                        float(overrides[reductions[0].approved_entry_key]),
                        36.0,
                    )
            finally:
                engine.dispose()

    def test_public_patch_respects_post_reduction_preserved_survivor_boundary(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-api-patch-survivor-boundary",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve_40(client)
                before = self._snapshot(client, number)
                reduced_target = next(
                    row for row in before["demand_shifts"]
                    if row["work_date"] == THURSDAY
                )
                segment_id = (
                    reduced_target["requirement_id"]
                    or reduced_target["segment_id"]
                )
                reduction_preview = client.get(
                    f"/api/v1/allocations/{reduced_target['allocation_id']}/work-queue-preview"
                ).json()
                reduced = client.post(
                    f"/api/v1/allocations/{reduced_target['allocation_id']}/permanent-reduction",
                    headers={"Idempotency-Key": "api-patch-boundary-reduce-778"},
                    json={
                        "expected_planning_version": reduction_preview["planning_version"],
                        "expected_request_version": reduction_preview["request_version"],
                        "expected_approval_revision_id": reduction_preview["approval_revision_id"],
                        "expected_operational_version": reduction_preview["operational_version"],
                    },
                )
                self.assertEqual(reduced.status_code, 200, reduced.text)

                after_reduction = self._snapshot(client, number)
                returned_target = next(
                    row for row in after_reduction["demand_shifts"]
                    if row["work_date"] != THURSDAY
                )
                return_preview = client.get(
                    f"/api/v1/allocations/{returned_target['allocation_id']}/work-queue-preview"
                ).json()
                returned = client.post(
                    f"/api/v1/allocations/{returned_target['allocation_id']}/return-to-work-queue",
                    headers={"Idempotency-Key": "api-patch-boundary-return-778"},
                    json={
                        "expected_planning_version": return_preview["planning_version"],
                        "expected_approval_revision_id": return_preview["approval_revision_id"],
                        "expected_operational_version": return_preview["operational_version"],
                    },
                )
                self.assertEqual(returned.status_code, 200, returned.text)

                preserved_before = self._snapshot(client, number)
                segment_before = preserved_before["demand_segments"][0]
                self.assertEqual(segment_before["planned_hours"], 32.0)
                self.assertEqual(segment_before["covered_hours"], 24.0)
                self.assertEqual(segment_before["remaining_hours"], 8.0)
                survivor_state_before = sorted(
                    (
                        row["allocation_id"],
                        row.get("source"),
                        row.get("locked"),
                        row.get("asset_allocation_id"),
                        row["hours"],
                    )
                    for row in preserved_before["demand_shifts"]
                )

                rejected = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 28},
                )
                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertEqual(
                    rejected.json()["error"]["code"],
                    "segment_overallocation_choice_required",
                )
                rejected_context = rejected.json()["error"]["context"]
                self.assertEqual(rejected_context["planned_hours"], 20.0)
                self.assertEqual(rejected_context["current_planned_hours"], 32.0)
                self.assertEqual(rejected_context["current_excess_hours"], 0.0)
                self.assertEqual(rejected_context["excess_hours"], 4.0)
                self.assertEqual(rejected_context["locked_hours"], 24.0)
                self.assertEqual(
                    rejected_context["operational_base_hours"],
                    40.0,
                )
                self.assertEqual(
                    rejected_context["safe_operational_base_hours"],
                    32.0,
                )

                unchanged = self._snapshot(client, number)
                unchanged_segment = unchanged["demand_segments"][0]
                self.assertEqual(unchanged_segment["planned_hours"], 32.0)
                self.assertEqual(unchanged_segment["covered_hours"], 24.0)
                self.assertEqual(unchanged_segment["remaining_hours"], 8.0)
                self.assertEqual(
                    sorted(
                        (
                            row["allocation_id"],
                            row.get("source"),
                            row.get("locked"),
                            row.get("asset_allocation_id"),
                            row["hours"],
                        )
                        for row in unchanged["demand_shifts"]
                    ),
                    survivor_state_before,
                )

                acknowledged = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={
                        "planned_hours": 28,
                        "allow_locked_overallocation": True,
                    },
                )
                self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
                acknowledged_snapshot = self._snapshot(client, number)
                acknowledged_segment = acknowledged_snapshot["demand_segments"][0]
                self.assertEqual(acknowledged_segment["planned_hours"], 20.0)
                self.assertEqual(acknowledged_segment["covered_hours"], 24.0)
                self.assertEqual(acknowledged_segment["remaining_hours"], 0.0)
                acknowledged_read = client.get(f"/api/v1/segments/{segment_id}")
                self.assertEqual(acknowledged_read.status_code, 200, acknowledged_read.text)
                self.assertEqual(acknowledged_read.json()["planned_hours"], 20.0)
                self.assertEqual(
                    acknowledged_read.json()["operational_base_hours"],
                    28.0,
                )

                unchanged_exception = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 28},
                )
                self.assertEqual(
                    unchanged_exception.status_code,
                    200,
                    unchanged_exception.text,
                )
                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                after_rebuild = self._snapshot(client, number)
                self.assertEqual(after_rebuild["demand_segments"][0]["planned_hours"], 20.0)
                self.assertEqual(after_rebuild["demand_segments"][0]["covered_hours"], 24.0)

                increased = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 24},
                )
                self.assertEqual(increased.status_code, 422, increased.text)
                increased_context = increased.json()["error"]["context"]
                self.assertEqual(increased_context["current_excess_hours"], 4.0)
                self.assertEqual(increased_context["excess_hours"], 8.0)

                reduced_exception = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 30},
                )
                self.assertEqual(
                    reduced_exception.status_code,
                    200,
                    reduced_exception.text,
                )
                reduced_snapshot = self._snapshot(client, number)
                reduced_segment = reduced_snapshot["demand_segments"][0]
                self.assertEqual(reduced_segment["planned_hours"], 22.0)
                self.assertEqual(reduced_segment["covered_hours"], 24.0)

                exact = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 32},
                )
                self.assertEqual(exact.status_code, 200, exact.text)
                exact_snapshot = self._snapshot(client, number)
                exact_segment = exact_snapshot["demand_segments"][0]
                self.assertEqual(exact_segment["planned_hours"], 24.0)
                self.assertEqual(exact_segment["covered_hours"], 24.0)
                self.assertEqual(exact_segment["remaining_hours"], 0.0)

                above = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 36},
                )
                self.assertEqual(above.status_code, 200, above.text)
                above_snapshot = self._snapshot(client, number)
                above_segment = above_snapshot["demand_segments"][0]
                self.assertEqual(above_segment["planned_hours"], 28.0)
                self.assertEqual(above_segment["covered_hours"], 24.0)
                self.assertEqual(above_segment["remaining_hours"], 4.0)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    reductions = session.scalars(
                        select(RequestCompletionReduction).where(
                            RequestCompletionReduction.reconciliation_state == "ACTIVE"
                        )
                    ).all()
                    self.assertEqual(len(reductions), 1)
                    self.assertEqual(reductions[0].reduction_hours, Decimal("8.00"))
                    exception_events = session.scalars(
                        select(PlanningChangeHistory).where(
                            PlanningChangeHistory.action
                            == "Dérogation surallocation manuelle"
                        )
                    ).all()
                    self.assertGreaterEqual(len(exception_events), 1)
            finally:
                engine.dispose()

    def test_auto_locked_reduction_uses_effective_exception_budget_through_resync(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_40(client)
                    target = self._snapshot(client, number)["demand_shifts"][0]
                    segment_id = target["requirement_id"]
                    preview = client.get(
                        f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                    ).json()
                    reduced = client.post(
                        f"/api/v1/allocations/{target['allocation_id']}/permanent-reduction",
                        headers={"Idempotency-Key": "auto-locked-reduction"},
                        json={
                            "expected_planning_version": preview["planning_version"],
                            "expected_request_version": preview["request_version"],
                            "expected_approval_revision_id": preview["approval_revision_id"],
                            "expected_operational_version": preview["operational_version"],
                        },
                    )
                    self.assertEqual(reduced.status_code, 200, reduced.text)
                    with factory.begin() as session:
                        rows = session.scalars(select(Shift).where(
                            Shift.resource_requirement_id == segment_id
                        )).all()
                        self.assertEqual(len(rows), 4)
                        survivor_ids = sorted(row.id for row in rows)
                        for row in rows:
                            row.source = "MANUAL"
                            row.locked = True

                    rejected = client.patch(
                        f"/api/v1/segments/{segment_id}", json={"planned_hours": 36}
                    )
                    self.assertEqual(rejected.status_code, 422, rejected.text)
                    self.assertEqual(rejected.json()["error"]["code"], "segment_overallocation_choice_required")
                    context = rejected.json()["error"]["context"]
                    self.assertEqual(context["planned_hours"], 28)
                    self.assertEqual(context["excess_hours"], 4)
                    self.assertEqual(context["safe_operational_base_hours"], 40)
                    for payload in (
                        {"planned_hours": 36, "allow_locked_overallocation": True},
                        {"planned_hours": 36},
                    ):
                        saved = client.patch(f"/api/v1/segments/{segment_id}", json=payload)
                        self.assertEqual(saved.status_code, 200, saved.text)
                    increased = client.patch(
                        f"/api/v1/segments/{segment_id}", json={"planned_hours": 32}
                    )
                    self.assertEqual(increased.status_code, 422, increased.text)
                    self.assertEqual(increased.json()["error"]["context"]["current_excess_hours"], 4)
                    self.assertEqual(increased.json()["error"]["context"]["excess_hours"], 8)
                    with factory.begin() as session:
                        SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_operational_choices(number)
                    rebuilt = client.post("/api/v1/planning/rebuild")
                    self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                    segment = self._snapshot(client, number)["demand_segments"][0]
                    self.assertEqual(segment["planned_hours"], 28)
                    self.assertEqual(segment["covered_hours"], 32)
                    self.assertEqual(segment["remaining_hours"], 0)
                    with factory() as session:
                        rows = session.scalars(select(Shift).where(
                            Shift.resource_requirement_id == segment_id
                        )).all()
                        self.assertEqual(sorted(row.id for row in rows), survivor_ids)
                        self.assertTrue(all(row.locked and row.source == "MANUAL" for row in rows))
            finally:
                engine.dispose()

    def test_acknowledged_locked_survivors_survive_atomic_sync_and_later_resync(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-api-locked-sync",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number, segment_id, survivors = self._prepare_reduced_returned_24(
                    client,
                    key_prefix="api-locked-sync-778",
                )

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                try:
                    with factory.begin() as session:
                        survivor_rows = session.scalars(
                            select(Shift).where(
                                Shift.resource_requirement_id == segment_id
                            )
                        ).all()
                        self.assertEqual(len(survivor_rows), 3)
                        for shift in survivor_rows:
                            shift.locked = True
                            shift.source = "MANUAL"
                        survivor_ids = tuple(sorted(shift.id for shift in survivor_rows))
                finally:
                    engine.dispose()

                rejected = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 28},
                )
                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertEqual(
                    rejected.json()["error"]["code"],
                    "segment_overallocation_choice_required",
                )

                acknowledged = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={
                        "planned_hours": 28,
                        "allow_locked_overallocation": True,
                    },
                )
                self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
                current = client.get(f"/api/v1/segments/{segment_id}")
                self.assertEqual(current.status_code, 200, current.text)
                self.assertEqual(current.json()["operational_base_hours"], 28.0)
                self.assertEqual(current.json()["planned_hours"], 20.0)

                after = self._snapshot(client, number)
                segment = after["demand_segments"][0]
                self.assertEqual(segment["covered_hours"], 24.0)
                self.assertEqual(segment["planned_hours"], 20.0)
                self.assertEqual(segment["excess_hours"], 4.0)

                unchanged = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 28},
                )
                self.assertEqual(unchanged.status_code, 200, unchanged.text)
                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    survivor_rows = session.scalars(
                        select(Shift).where(
                            Shift.resource_requirement_id == segment_id
                        )
                    ).all()
                    self.assertEqual(
                        tuple(sorted(shift.id for shift in survivor_rows)),
                        survivor_ids,
                    )
                    self.assertTrue(all(shift.locked for shift in survivor_rows))
                    self.assertTrue(all(shift.source == "MANUAL" for shift in survivor_rows))
                    exception_events = session.scalars(
                        select(PlanningChangeHistory).where(
                            PlanningChangeHistory.action
                            == "Dérogation surallocation manuelle"
                        )
                    ).all()
                    self.assertGreaterEqual(len(exception_events), 1)
            finally:
                engine.dispose()

    def test_acknowledged_asset_promoted_survivors_keep_assets_through_sync_and_rebuild(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-api-asset-sync",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number, segment_id, _survivors = self._prepare_reduced_returned_24(
                    client,
                    key_prefix="api-asset-sync-778",
                )

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                try:
                    with factory() as session:
                        survivor_rows = session.scalars(
                            select(Shift)
                            .where(Shift.resource_requirement_id == segment_id)
                            .order_by(Shift.work_date, Shift.id)
                        ).all()
                        self.assertEqual(len(survivor_rows), 3)
                        survivor_ids = tuple(shift.id for shift in survivor_rows)
                finally:
                    engine.dispose()

                for index, (shift_id, asset_id) in enumerate(
                    zip(
                        survivor_ids,
                        ("ASSET-778-A", "ASSET-778-B", "ASSET-778-C"),
                        strict=True,
                    )
                ):
                    version_response = client.get("/api/v1/assets/requirements")
                    self.assertEqual(
                        version_response.status_code,
                        200,
                        version_response.text,
                    )
                    assigned = client.put(
                        f"/api/v1/assets/shifts/{shift_id}/assignment",
                        headers={
                            "Idempotency-Key": f"api-asset-promote-778-{index}"
                        },
                        json={
                            "asset_id": asset_id,
                            "expected_planning_version": version_response.json()[
                                "planning_version"
                            ],
                        },
                    )
                    self.assertEqual(assigned.status_code, 200, assigned.text)

                acknowledged = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={
                        "planned_hours": 28,
                        "allow_locked_overallocation": True,
                    },
                )
                self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
                current = client.get(f"/api/v1/segments/{segment_id}")
                self.assertEqual(current.status_code, 200, current.text)
                self.assertEqual(current.json()["operational_base_hours"], 28.0)
                self.assertEqual(current.json()["planned_hours"], 20.0)

                later_sync = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 28},
                )
                self.assertEqual(later_sync.status_code, 200, later_sync.text)
                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    survivor_rows = session.scalars(
                        select(Shift)
                        .where(Shift.resource_requirement_id == segment_id)
                        .order_by(Shift.work_date, Shift.id)
                    ).all()
                    self.assertEqual(
                        tuple(shift.id for shift in survivor_rows),
                        survivor_ids,
                    )
                    self.assertTrue(all(shift.locked for shift in survivor_rows))
                    allocations = session.scalars(
                        select(AssetAllocation).where(
                            AssetAllocation.asset_id.in_(
                                ("ASSET-778-A", "ASSET-778-B", "ASSET-778-C")
                            )
                        )
                    ).all()
                    self.assertEqual(len(allocations), 3)
            finally:
                engine.dispose()

    def test_c_permanent_reduction_40_to_32_keeps_historical_approval_and_no_queue_residual(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-api-c",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve_40(client)
                before = self._snapshot(client, number)
                target = next(
                    row for row in before["demand_shifts"]
                    if row["work_date"] == THURSDAY
                )
                preview = client.get(
                    f"/api/v1/allocations/{target['allocation_id']}/work-queue-preview"
                ).json()
                reduced = client.post(
                    f"/api/v1/allocations/{target['allocation_id']}/permanent-reduction",
                    headers={"Idempotency-Key": "api-c-reduce-778"},
                    json={
                        "expected_planning_version": preview["planning_version"],
                        "expected_request_version": preview["request_version"],
                        "expected_approval_revision_id": preview["approval_revision_id"],
                        "expected_operational_version": preview["operational_version"],
                    },
                )
                self.assertEqual(reduced.status_code, 200, reduced.text)
                self.assertEqual(reduced.json()["effective_need_after"], 32.0)
                self.assertEqual(reduced.json()["remaining_hours_after"], 0.0)

                after = self._snapshot(client, number)
                segment = after["demand_segments"][0]
                self.assertEqual(segment["planned_hours"], 32.0)
                self.assertEqual(segment["covered_hours"], 32.0)
                self.assertEqual(segment["remaining_hours"], 0.0)
                self.assertEqual(len(after["demand_shifts"]), 4)
                self.assertEqual(self._actions(client, number), [])

                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    revision = session.scalar(
                        select(RequestApprovalRevision)
                        .where(
                            RequestApprovalRevision.workforce_request_id == request.id
                        )
                        .order_by(RequestApprovalRevision.created_at)
                    )
                    payload = json.loads(revision.payload_text)
                    self.assertEqual(
                        sum(
                            Decimal(str(row["hours"]))
                            for row in payload["authorization"]["entries"]
                            if row.get("hours") is not None
                        ),
                        Decimal("40.00"),
                    )
                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    requirement = session.scalar(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                    )
                    self.assertEqual(requirement.planned_hours, Decimal("32.00"))
                    covered = sum(
                        session.scalars(
                            select(Shift.hours).where(
                                Shift.resource_requirement_id == requirement.id
                            )
                        ).all(),
                        Decimal("0.00"),
                    )
                    self.assertEqual(covered, Decimal("32.00"))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
