from __future__ import annotations

from datetime import time
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.infrastructure.sql import (
    PlanningWorkQueueRelease,
    PlanningWorkQueueRestoration,
    RequestCompletionReduction,
    RequestOperationalState,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    RequestApprovalRevision,
    Shift,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.approval_test_support import (
    TEST_ADMIN_USER_ID,
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
        seed_test_approval_routing(session, map_existing_tasks=True)

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
                    sibling_id = sibling.id
                    release_a_id = release_a.id
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

                patched = client.patch(
                    f"/api/v1/segments/{segment_id}",
                    json={"planned_hours": 36},
                )
                self.assertEqual(patched.status_code, 200, patched.text)

                current = client.get(f"/api/v1/segments/{segment_id}")
                self.assertEqual(current.status_code, 200, current.text)
                self.assertEqual(current.json()["planned_hours"], 28.0)
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
