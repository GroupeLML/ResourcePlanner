from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from decimal import Decimal
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.infrastructure.sql import (
    Project,
    RequestCompletionReduction,
    RequestRequirementPlanningIntent,
    RequestShiftSurvivorProtection,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.base import utc_now
from app.infrastructure.sql.command_adapters import SqlPlanningCommandAdapter
from app.infrastructure.sql.idempotency import CommandIdempotencyReceipt
from app.infrastructure.sql.planning_intent_repository import (
    SqlRequestRequirementPlanningIntentRepository,
)
from app.server import create_api_app
from tests.approval_test_support import (
    routed_demand_payload,
    seed_test_approval_routing,
)
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")
MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)
WEDNESDAY = date(2026, 11, 4)


@unittest.skipUnless(
    IS_MSSQL,
    "Validation #793 réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class Issue793SqlServerPrecisionTests(unittest.TestCase):
    @staticmethod
    def _assignment_actions(client: TestClient, number: str) -> list[dict]:
        response = client.get(
            "/api/v1/planning/actions",
            params={
                "start": MONDAY.isoformat(),
                "end": WEDNESDAY.isoformat(),
                "scope": "global",
            },
        )
        assert response.status_code == 200, response.text
        return sorted(
            [
                row
                for row in response.json()
                if row.get("demand_number") == number
                and row.get("kind") == "ASSIGNMENT"
            ],
            key=lambda row: str(row.get("segment_id") or ""),
        )

    @staticmethod
    def _place_from_drop(
        client: TestClient,
        *,
        segment_id: str,
        resource_id: str,
        key: str,
    ) -> None:
        preview = client.post(
            f"/api/v1/segments/{segment_id}/place-from-drop/preview",
            json={
                "resource_id": resource_id,
                "dropped_date": MONDAY.isoformat(),
            },
        )
        assert preview.status_code == 200, preview.text
        contract = preview.json()
        assert contract["allowed"], contract
        placed = client.post(
            f"/api/v1/segments/{segment_id}/place-from-drop",
            headers={"Idempotency-Key": key},
            json={
                "resource_id": resource_id,
                "dropped_date": MONDAY.isoformat(),
                "expected_planning_version": contract["planning_version"],
                "expected_approval_revision_id": contract["approval_revision_id"],
                "expected_operational_version": contract["operational_version"],
            },
        )
        assert placed.status_code == 200, placed.text

    @staticmethod
    def _preview(client: TestClient, allocation_id: str) -> dict:
        response = client.get(
            f"/api/v1/allocations/{allocation_id}/work-queue-preview"
        )
        assert response.status_code == 200, response.text
        return response.json()

    @staticmethod
    def _reduce(
        client: TestClient,
        *,
        allocation_id: str,
        preview: dict,
        key: str,
    ):
        return client.post(
            f"/api/v1/allocations/{allocation_id}/permanent-reduction",
            headers={"Idempotency-Key": key},
            json={
                "expected_planning_version": preview["planning_version"],
                "expected_request_version": preview["request_version"],
                "expected_approval_revision_id": preview["approval_revision_id"],
                "expected_operational_version": preview["operational_version"],
            },
        )

    @staticmethod
    def _approval_task_code(project_number: str) -> str:
        return f"APPROVAL-{project_number[-12:]}"

    @classmethod
    def _seed_resources(
        cls,
        factory,
        *,
        project_id: str,
        project_number: str,
        resources: tuple[tuple[str, str], ...],
    ) -> None:
        with factory.begin() as session:
            session.add(
                Project(
                    id=project_id,
                    number=project_number,
                    name=f"Projet #793 {project_number}",
                )
            )
            approval_task = TaskCatalogEntry(
                id=f"TASK-{project_id}",
                project_number=project_number,
                task_code=cls._approval_task_code(project_number),
                label=f"Approbation #793 {project_number}",
                status="Actif",
                active=True,
                time_entry_enabled=True,
                expenses_enabled=False,
            )
            session.add(approval_task)
            for index, (resource_id, name) in enumerate(resources, start=1):
                session.add(
                    Resource(
                        id=resource_id,
                        external_id=f"EMP-{resource_id}",
                        name=name,
                        resource_class="ELECTRICAL",
                        active=True,
                        erp_active=True,
                        erp_status="Active",
                    )
                )
                session.flush()
                session.add(
                    ResourceAvailabilityRule(
                        id=f"AV793-{resource_id}-{index}",
                        resource_id=resource_id,
                        availability_type="Horaire standard",
                        weekdays="Lun,Mar,Mer",
                        start_time=time(8, 0),
                        end_time=time(16, 0),
                        active=True,
                    )
                )
            session.flush()
            # Route through a marker-scoped ERP task plus resource class. Never
            # reuse TEST-APPROVAL-TASK-N in the shared live SQL Server database.
            seed_test_approval_routing(
                session,
                task_ids=(approval_task.id,),
                include_default_task=False,
            )

    @staticmethod
    def _cleanup_survivor_protections(factory, project_id: str) -> None:
        """Remove only marker-owned #793 durable authority before shared migrations."""
        with factory.begin() as session:
            request_ids = tuple(
                session.scalars(
                    select(WorkforceRequest.id).where(
                        WorkforceRequest.project_id == project_id
                    )
                ).all()
            )
            if not request_ids:
                return
            intent_ids = tuple(
                session.scalars(
                    select(RequestRequirementPlanningIntent.id).where(
                        RequestRequirementPlanningIntent.workforce_request_id.in_(
                            request_ids
                        )
                    )
                ).all()
            )
            if intent_ids:
                session.execute(
                    delete(RequestShiftSurvivorProtection).where(
                        RequestShiftSurvivorProtection.planning_intent_id.in_(
                            intent_ids
                        )
                    )
                )

    @staticmethod
    def _slot_state(
        factory,
        number: str,
        resource_id: str,
        *,
        nominal_slot_hours: Decimal,
    ) -> dict:
        with factory() as session:
            request = session.scalar(
                select(WorkforceRequest).where(
                    WorkforceRequest.legacy_demand_number == number
                )
            )
            assert request is not None
            intent = session.scalar(
                select(RequestRequirementPlanningIntent).where(
                    RequestRequirementPlanningIntent.workforce_request_id == request.id,
                    RequestRequirementPlanningIntent.target_resource_id == resource_id,
                )
            )
            assert intent is not None
            requirement = session.get(
                ResourceRequirement,
                intent.current_resource_requirement_id,
            )
            assert requirement is not None
            shifts = tuple(
                session.scalars(
                    select(Shift)
                    .where(
                        Shift.resource_requirement_id == requirement.id,
                        (Shift.allocation_type.is_(None))
                        | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
                    )
                    .order_by(Shift.work_date, Shift.id)
                ).all()
            )
            effective_need = SqlRequestRequirementPlanningIntentRepository(
                session
            ).effective_need(
                planning_intent_id=intent.id,
                nominal_slot_hours=nominal_slot_hours,
            )
            reductions = tuple(
                session.scalars(
                    select(RequestCompletionReduction)
                    .where(
                        RequestCompletionReduction.planning_intent_id == intent.id,
                        RequestCompletionReduction.reconciliation_state == "ACTIVE",
                    )
                    .order_by(
                        RequestCompletionReduction.created_at,
                        RequestCompletionReduction.id,
                    )
                ).all()
            )
            protections = tuple(
                session.scalars(
                    select(RequestShiftSurvivorProtection)
                    .where(
                        RequestShiftSurvivorProtection.planning_intent_id == intent.id
                    )
                    .order_by(
                        RequestShiftSurvivorProtection.created_at,
                        RequestShiftSurvivorProtection.id,
                    )
                ).all()
            )
            stored_hours = tuple(
                Decimal(str(row[0])).quantize(Decimal("0.01"))
                for row in session.connection().exec_driver_sql(
                    "SELECT hours FROM shifts "
                    "WHERE resource_requirement_id = ? "
                    "AND (allocation_type IS NULL OR allocation_type <> ?) "
                    "ORDER BY work_date, id",
                    (requirement.id, MISSING_ALLOCATION_TYPE),
                ).all()
            )
            return {
                "request_id": request.id,
                "request_version": int(request.aggregate_version or 1),
                "intent_id": intent.id,
                "requirement_id": requirement.id,
                "effective_need": effective_need,
                "planned_hours": Decimal(str(requirement.planned_hours)).quantize(
                    Decimal("0.01")
                ),
                "shifts": tuple(
                    (
                        row.id,
                        row.legacy_allocation_id or row.id,
                        row.work_date,
                        Decimal(str(row.hours)).quantize(Decimal("0.01")),
                        row.resource_id,
                    )
                    for row in shifts
                ),
                "reductions": reductions,
                "protections": protections,
                "stored_hours": stored_hours,
            }

    def _create_request(
        self,
        client: TestClient,
        *,
        project_number: str,
        lines: list[dict],
        comment: str,
    ) -> str:
        task_code = self._approval_task_code(project_number)
        routed_lines = [
            {
                **dict(line),
                "task_code": str(dict(line).get("task_code") or task_code),
            }
            for line in lines
        ]
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": project_number,
                    "submit": True,
                    "lines": routed_lines,
                }
            ),
        )
        self.assertEqual(created.status_code, 201, created.text)
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": comment},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        return number

    def test_real_sqlserver_sequential_and_fractional_reduction_use_exact_cents(self) -> None:
        """Preserve Phase 3A precision proof independently from survivor regeneration."""

        marker = uuid4().hex[:10]
        project_id = f"P793-C-{marker}"
        project_number = f"P-793-C-{marker}"
        eric_id = f"R793-C-{marker}"
        key = f"793-cent-reduce-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            self._seed_resources(
                factory,
                project_id=project_id,
                project_number=project_number,
                resources=((eric_id, f"ERIC CENT {marker}"),),
            )
            app = create_api_app(
                DATABASE_URL,
                actor_name=f"admin-793-cent-{marker}",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create_request(
                    client,
                    project_number=project_number,
                    lines=[
                        {
                            "position": 0,
                            "desired_start": MONDAY.isoformat(),
                            "desired_end": WEDNESDAY.isoformat(),
                            "desired_active_days": 3,
                            "estimated_hours": 16,
                            "required_resource_class": "ELECTRICAL",
                            "confirmation": "Confirmée",
                            "description": "#793 independent cent precision",
                        }
                    ],
                    comment="#793 SQL cent precision",
                )
                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 1)
                self._place_from_drop(
                    client,
                    segment_id=actions[0]["segment_id"],
                    resource_id=eric_id,
                    key=f"793-cent-drop-{marker}",
                )

                initial = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("16.00"),
                )
                self.assertEqual(initial["effective_need"], Decimal("16.00"))
                self.assertEqual(
                    tuple((row[2], row[3]) for row in initial["shifts"]),
                    (
                        (MONDAY, Decimal("5.33")),
                        (TUESDAY, Decimal("5.33")),
                        (WEDNESDAY, Decimal("5.34")),
                    ),
                )
                self.assertEqual(
                    initial["stored_hours"],
                    (Decimal("5.33"), Decimal("5.33"), Decimal("5.34")),
                )

                target = next(row for row in initial["shifts"] if row[2] == WEDNESDAY)
                preview = self._preview(client, target[1])
                self.assertEqual(
                    Decimal(str(preview["effective_need_after_reduction"])).quantize(
                        Decimal("0.01")
                    ),
                    Decimal("10.66"),
                )
                reduced = self._reduce(
                    client,
                    allocation_id=target[1],
                    preview=preview,
                    key=key,
                )
                self.assertEqual(reduced.status_code, 200, reduced.text)
                payload = reduced.json()
                self.assertEqual(
                    Decimal(str(payload["covered_hours_after"])).quantize(
                        Decimal("0.01")
                    ),
                    Decimal("10.66"),
                )
                self.assertEqual(
                    Decimal(str(payload["remaining_hours_after"])).quantize(
                        Decimal("0.01")
                    ),
                    Decimal("0.00"),
                )

                after = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("16.00"),
                )
                self.assertEqual(after["effective_need"], Decimal("10.66"))
                self.assertEqual(
                    tuple((row[2], row[3]) for row in after["shifts"]),
                    (
                        (MONDAY, Decimal("5.33")),
                        (TUESDAY, Decimal("5.33")),
                    ),
                )
                self.assertEqual(
                    after["stored_hours"],
                    (Decimal("5.33"), Decimal("5.33")),
                )
                active = [row for row in after["protections"] if row.active]
                self.assertEqual(len(active), 2)
                self.assertEqual(
                    {row.shift_id for row in active},
                    {row[0] for row in initial["shifts"] if row[2] != WEDNESDAY},
                )

                replay = self._reduce(
                    client,
                    allocation_id=target[1],
                    preview=preview,
                    key=key,
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(replay.json(), payload)
                replay_state = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("16.00"),
                )
                self.assertEqual(replay_state["shifts"], after["shifts"])
                self.assertEqual(
                    {row.id for row in replay_state["protections"] if row.active},
                    {row.id for row in active},
                )
        finally:
            self._cleanup_survivor_protections(factory, project_id)
            engine.dispose()

    def test_real_sqlserver_durable_survivors_survive_sibling_and_fresh_rebuild(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P793-S-{marker}"
        project_number = f"P-793-S-{marker}"
        eric_id = f"R793-E-{marker}"
        timmy_id = f"R793-T-{marker}"
        eric_key = f"793-survivor-eric-{marker}"
        failed_key = f"793-survivor-fail-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            self._seed_resources(
                factory,
                project_id=project_id,
                project_number=project_number,
                resources=(
                    (eric_id, f"ERIC FONTAINE {marker}"),
                    (timmy_id, f"TIMMY CURIE {marker}"),
                ),
            )
            app = create_api_app(
                DATABASE_URL,
                actor_name=f"admin-793-survivor-{marker}",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create_request(
                    client,
                    project_number=project_number,
                    lines=[
                        {
                            "position": 0,
                            "desired_start": MONDAY.isoformat(),
                            "desired_end": WEDNESDAY.isoformat(),
                            "desired_active_days": 3,
                            "estimated_hours": 24,
                            "required_resource_class": "ELECTRICAL",
                            "confirmation": "Confirmée",
                            "description": "#793 SQL durable A",
                        },
                        {
                            "position": 1,
                            "desired_start": MONDAY.isoformat(),
                            "desired_end": WEDNESDAY.isoformat(),
                            "desired_active_days": 3,
                            "estimated_hours": 24,
                            "required_resource_class": "ELECTRICAL",
                            "confirmation": "Confirmée",
                            "description": "#793 SQL durable B",
                        },
                    ],
                    comment="#793 SQL durable survivor authority",
                )
                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 2)
                self._place_from_drop(
                    client,
                    segment_id=actions[0]["segment_id"],
                    resource_id=eric_id,
                    key=f"793-survivor-drop-e-{marker}",
                )
                actions = self._assignment_actions(client, number)
                self._place_from_drop(
                    client,
                    segment_id=actions[0]["segment_id"],
                    resource_id=timmy_id,
                    key=f"793-survivor-drop-t-{marker}",
                )

                eric_initial = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                original_survivors = {
                    row[2]: row[0]
                    for row in eric_initial["shifts"]
                    if row[2] != WEDNESDAY
                }
                eric_wed = next(
                    row for row in eric_initial["shifts"] if row[2] == WEDNESDAY
                )
                eric_preview = self._preview(client, eric_wed[1])
                eric_response = self._reduce(
                    client,
                    allocation_id=eric_wed[1],
                    preview=eric_preview,
                    key=eric_key,
                )
                self.assertEqual(eric_response.status_code, 200, eric_response.text)
                eric_payload = eric_response.json()

                eric_after_first = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                self.assertEqual(eric_after_first["effective_need"], Decimal("16.00"))
                self.assertEqual(
                    {row[2]: row[0] for row in eric_after_first["shifts"]},
                    original_survivors,
                )
                active_eric = [
                    row for row in eric_after_first["protections"] if row.active
                ]
                self.assertEqual(len(active_eric), 2)
                self.assertEqual(
                    {row.shift_id for row in active_eric},
                    set(original_survivors.values()),
                )
                first_reduction_id = eric_after_first["reductions"][0].id
                self.assertEqual(
                    {row.originating_reduction_id for row in active_eric},
                    {first_reduction_id},
                )

                # Fresh-session unrelated rebuild must load protection from SQL.
                with factory.begin() as session:
                    SqlPlanningCommandAdapter(session).rebuild()
                eric_after_rebuild = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                self.assertEqual(
                    {row[2]: row[0] for row in eric_after_rebuild["shifts"]},
                    original_survivors,
                )

                replay = self._reduce(
                    client,
                    allocation_id=eric_wed[1],
                    preview=eric_preview,
                    key=eric_key,
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(replay.json(), eric_payload)
                replay_state = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                self.assertEqual(
                    {row.id for row in replay_state["protections"] if row.active},
                    {row.id for row in active_eric},
                )

                timmy_before = self._slot_state(
                    factory,
                    number,
                    timmy_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                timmy_wed = next(
                    row for row in timmy_before["shifts"] if row[2] == WEDNESDAY
                )
                timmy_preview = self._preview(client, timmy_wed[1])
                timmy_response = self._reduce(
                    client,
                    allocation_id=timmy_wed[1],
                    preview=timmy_preview,
                    key=f"793-survivor-timmy-{marker}",
                )
                self.assertEqual(timmy_response.status_code, 200, timmy_response.text)

                eric_after_second = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                timmy_after_second = self._slot_state(
                    factory,
                    number,
                    timmy_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                self.assertEqual(eric_after_second["effective_need"], Decimal("16.00"))
                self.assertEqual(timmy_after_second["effective_need"], Decimal("16.00"))
                self.assertEqual(
                    {row[2]: row[0] for row in eric_after_second["shifts"]},
                    original_survivors,
                )
                self.assertEqual(
                    tuple((row[2], row[3]) for row in eric_after_second["shifts"]),
                    (
                        (MONDAY, Decimal("8.00")),
                        (TUESDAY, Decimal("8.00")),
                    ),
                )
                self.assertEqual(eric_after_second["stored_hours"], (
                    Decimal("8.00"),
                    Decimal("8.00"),
                ))
                self.assertFalse(
                    any(row[2] == WEDNESDAY for row in eric_after_second["shifts"])
                )

                active_eric_after = [
                    row for row in eric_after_second["protections"] if row.active
                ]
                active_timmy_after = [
                    row for row in timmy_after_second["protections"] if row.active
                ]
                self.assertEqual(
                    {row.id for row in active_eric_after},
                    {row.id for row in active_eric},
                )
                self.assertTrue(
                    {row.shift_id for row in active_eric_after}.isdisjoint(
                        {row.shift_id for row in active_timmy_after}
                    )
                )

                # The composite FK also rejects provenance from a reduction
                # belonging to another durable PlanningIntent.
                with self.assertRaises(IntegrityError):
                    with factory.begin() as session:
                        timmy_reduction = session.get(
                            RequestCompletionReduction,
                            timmy_after_second["reductions"][0].id,
                        )
                        assert timmy_reduction is not None
                        session.add(
                            RequestShiftSurvivorProtection(
                                planning_intent_id=eric_after_second["intent_id"],
                                shift_id=None,
                                original_shift_id=f"MISMATCH-{marker}",
                                originating_reduction_id=timmy_reduction.id,
                                active=False,
                                retired_at=utc_now(),
                                retired_reason="TEST_CONSTRAINT",
                            )
                        )
                        session.flush()

                # SQL Server filtered uniqueness rejects a second active owner for
                # the same live Shift.
                with self.assertRaises(IntegrityError):
                    with factory.begin() as session:
                        timmy_reduction = session.get(
                            RequestCompletionReduction,
                            timmy_after_second["reductions"][0].id,
                        )
                        assert timmy_reduction is not None
                        session.add(
                            RequestShiftSurvivorProtection(
                                planning_intent_id=timmy_after_second["intent_id"],
                                shift_id=next(iter(original_survivors.values())),
                                original_shift_id=next(iter(original_survivors.values())),
                                originating_reduction_id=timmy_reduction.id,
                                active=True,
                            )
                        )
                        session.flush()

                # Controlled failure occurs after the command has flushed reduction
                # and protection changes; SQL Server must roll back all of it.
                rollback_target = next(
                    row for row in eric_after_second["shifts"] if row[2] == TUESDAY
                )
                rollback_preview = self._preview(client, rollback_target[1])
                before_failed = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                with patch.object(
                    SqlPlanningCommandAdapter,
                    "rebuild",
                    side_effect=RuntimeError("issue793 sqlserver controlled rebuild failure"),
                ):
                    failed = self._reduce(
                        client,
                        allocation_id=rollback_target[1],
                        preview=rollback_preview,
                        key=failed_key,
                    )
                self.assertEqual(failed.status_code, 500, failed.text)
                self.assertEqual(
                    failed.json().get("error", {}).get("code"),
                    "internal_error",
                )
                after_failed = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                self.assertEqual(after_failed["shifts"], before_failed["shifts"])
                self.assertEqual(
                    {row.id for row in after_failed["protections"] if row.active},
                    {row.id for row in before_failed["protections"] if row.active},
                )
                self.assertEqual(
                    len(after_failed["reductions"]),
                    len(before_failed["reductions"]),
                )
                with factory() as session:
                    failed_receipt = session.scalar(
                        select(CommandIdempotencyReceipt).where(
                            CommandIdempotencyReceipt.command_scope
                            == "request_work_queue.permanent_reduction",
                            CommandIdempotencyReceipt.idempotency_key == failed_key,
                        )
                    )
                self.assertIsNone(failed_receipt)
        finally:
            self._cleanup_survivor_protections(factory, project_id)
            engine.dispose()

    def test_real_sqlserver_need_edit_vs_protected_return_never_admits_stale_coverage(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P793-N-{marker}"
        project_number = f"P-793-N-{marker}"
        eric_id = f"R793-N-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            self._seed_resources(
                factory,
                project_id=project_id,
                project_number=project_number,
                resources=((eric_id, f"ERIC NEED RACE {marker}"),),
            )
            setup_app = create_api_app(
                DATABASE_URL,
                actor_name=f"admin-793-need-race-{marker}",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(setup_app, raise_server_exceptions=False) as client:
                number = self._create_request(
                    client,
                    project_number=project_number,
                    lines=[
                        {
                            "position": 0,
                            "desired_start": MONDAY.isoformat(),
                            "desired_end": WEDNESDAY.isoformat(),
                            "desired_active_days": 3,
                            "estimated_hours": 24,
                            "required_resource_class": "ELECTRICAL",
                            "confirmation": "Confirmée",
                            "description": "#793 SQL concurrent need edit",
                        }
                    ],
                    comment="#793 SQL need-edit concurrency",
                )
                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 1)
                segment_id = str(actions[0]["segment_id"])
                self._place_from_drop(
                    client,
                    segment_id=segment_id,
                    resource_id=eric_id,
                    key=f"793-need-race-drop-{marker}",
                )
                initial = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                target = next(
                    row for row in initial["shifts"] if row[2] == WEDNESDAY
                )
                reduced = self._reduce(
                    client,
                    allocation_id=target[1],
                    preview=self._preview(client, target[1]),
                    key=f"793-need-race-reduce-{marker}",
                )
                self.assertEqual(reduced.status_code, 200, reduced.text)

                protected = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("24.00"),
                )
                self.assertEqual(protected["effective_need"], Decimal("16.00"))
                self.assertEqual(
                    sum((row[3] for row in protected["shifts"]), Decimal("0.00")),
                    Decimal("16.00"),
                )
                return_target = next(
                    row for row in protected["shifts"] if row[2] == MONDAY
                )
                return_preview = self._preview(client, return_target[1])

            def patch_need() -> tuple[int, dict]:
                app = create_api_app(
                    DATABASE_URL,
                    actor_name=f"admin-793-need-race-patch-{marker}",
                    auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                )
                with TestClient(app, raise_server_exceptions=False) as client:
                    response = client.patch(
                        f"/api/v1/segments/{segment_id}",
                        json={"planned_hours": 20},
                    )
                    return response.status_code, response.json()

            def return_shift() -> tuple[int, dict]:
                app = create_api_app(
                    DATABASE_URL,
                    actor_name=f"admin-793-need-race-return-{marker}",
                    auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                )
                with TestClient(app, raise_server_exceptions=False) as client:
                    response = client.post(
                        f"/api/v1/allocations/{return_target[1]}/return-to-work-queue",
                        headers={"Idempotency-Key": f"793-need-race-return-{marker}"},
                        json={
                            "expected_planning_version": return_preview["planning_version"],
                            "expected_approval_revision_id": return_preview["approval_revision_id"],
                            "expected_operational_version": return_preview["operational_version"],
                        },
                    )
                    return response.status_code, response.json()

            with ThreadPoolExecutor(max_workers=2) as executor:
                patch_future = executor.submit(patch_need)
                return_future = executor.submit(return_shift)
                patch_status, patch_payload = patch_future.result(timeout=30)
                return_status, return_payload = return_future.result(timeout=30)

            self.assertIn(patch_status, (200, 409, 422), patch_payload)
            self.assertIn(return_status, (200, 409), return_payload)
            self.assertIn(200, (patch_status, return_status))

            final = self._slot_state(
                factory,
                number,
                eric_id,
                nominal_slot_hours=Decimal("24.00"),
            )
            final_coverage = sum(
                (row[3] for row in final["shifts"]),
                Decimal("0.00"),
            )
            # Bare need reduction to base20 means H=12 after the active 8h
            # permanent reduction. It may commit only after serialization has made
            # retained coverage safe; H12/C16 is never an admissible final state.
            if patch_status == 200:
                self.assertEqual(final["planned_hours"], Decimal("12.00"))
                self.assertLessEqual(final_coverage, Decimal("12.00"))
            else:
                self.assertEqual(final["planned_hours"], Decimal("16.00"))
                self.assertLessEqual(final_coverage, Decimal("16.00"))

            if patch_status == 422:
                self.assertEqual(
                    patch_payload.get("error", {}).get("code"),
                    "segment_overallocation_choice_required",
                )
            if return_status == 409:
                self.assertEqual(
                    return_payload.get("error", {}).get("code"),
                    "planning_version_conflict",
                )
        finally:
            self._cleanup_survivor_protections(factory, project_id)
            engine.dispose()

    def test_real_sqlserver_partial_auto_residual_generates_exact_four_hours(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P793-R-{marker}"
        project_number = f"P-793-R-{marker}"
        eric_id = f"R793-R-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            self._seed_resources(
                factory,
                project_id=project_id,
                project_number=project_number,
                resources=((eric_id, f"ERIC RESIDUAL {marker}"),),
            )
            app = create_api_app(
                DATABASE_URL,
                actor_name=f"admin-793-residual-{marker}",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._create_request(
                    client,
                    project_number=project_number,
                    lines=[
                        {
                            "position": 0,
                            "desired_start": MONDAY.isoformat(),
                            "desired_end": WEDNESDAY.isoformat(),
                            "desired_active_days": 3,
                            "estimated_hours": 28,
                            "required_resource_class": "ELECTRICAL",
                            "confirmation": "Confirmée",
                            "description": "#793 SQL partial residual",
                        }
                    ],
                    comment="#793 SQL partial residual",
                )
                actions = self._assignment_actions(client, number)
                self._place_from_drop(
                    client,
                    segment_id=actions[0]["segment_id"],
                    resource_id=eric_id,
                    key=f"793-residual-drop-{marker}",
                )
                initial = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("28.00"),
                )
                self.assertEqual(
                    tuple((row[2], row[3]) for row in initial["shifts"]),
                    (
                        (MONDAY, Decimal("8.00")),
                        (TUESDAY, Decimal("8.00")),
                        (WEDNESDAY, Decimal("8.00")),
                    ),
                )
                original_survivors = {
                    row[2]: row[0]
                    for row in initial["shifts"]
                    if row[2] != WEDNESDAY
                }
                target = next(row for row in initial["shifts"] if row[2] == WEDNESDAY)
                preview = self._preview(client, target[1])
                response = self._reduce(
                    client,
                    allocation_id=target[1],
                    preview=preview,
                    key=f"793-residual-reduce-{marker}",
                )
                self.assertEqual(response.status_code, 200, response.text)

                after = self._slot_state(
                    factory,
                    number,
                    eric_id,
                    nominal_slot_hours=Decimal("28.00"),
                )
                self.assertEqual(after["effective_need"], Decimal("20.00"))
                self.assertEqual(
                    tuple((row[2], row[3]) for row in after["shifts"]),
                    (
                        (MONDAY, Decimal("8.00")),
                        (TUESDAY, Decimal("8.00")),
                        (WEDNESDAY, Decimal("4.00")),
                    ),
                )
                self.assertEqual(
                    {row[2]: row[0] for row in after["shifts"] if row[2] != WEDNESDAY},
                    original_survivors,
                )
                self.assertEqual(
                    sum((row[3] for row in after["shifts"]), Decimal("0.00")),
                    Decimal("20.00"),
                )
                self.assertEqual(
                    after["stored_hours"],
                    (Decimal("8.00"), Decimal("8.00"), Decimal("4.00")),
                )
                active = [row for row in after["protections"] if row.active]
                self.assertEqual(
                    {row.shift_id for row in active},
                    set(original_survivors.values()),
                )
                generated = next(row for row in after["shifts"] if row[2] == WEDNESDAY)
                self.assertNotIn(generated[0], {row.shift_id for row in active})
        finally:
            self._cleanup_survivor_protections(factory, project_id)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
