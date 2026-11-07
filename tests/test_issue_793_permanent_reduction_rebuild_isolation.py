from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.employee_sync import EmployeeSyncService, ExternalEmployeeRecord
from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.infrastructure.sql import (
    PlanningWorkQueueRelease,
    RequestCompletionReduction,
    RequestRequirementPlanningIntent,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.command_adapters import SqlPlanningCommandAdapter
from app.infrastructure.sql.employee_sync_repository import SqlEmployeeSyncRepository
from app.infrastructure.sql.idempotency import CommandIdempotencyReceipt
from app.infrastructure.sql.planning_version import SqlPlanningMutationVersionRepository
from app.infrastructure.sql.planning_intent_repository import (
    SqlRequestRequirementPlanningIntentRepository,
)
from app.server import create_api_app
from tests import test_request_work_queue_api_integration as work_queue_api
from tests.approval_test_support import routed_demand_payload
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


START = "2026-11-02"
END = "2026-11-06"
WEDNESDAY = "2026-11-04"
THURSDAY = "2026-11-05"
EXPECTED_DAYS = {
    "2026-11-02",
    "2026-11-03",
    "2026-11-04",
    "2026-11-05",
    "2026-11-06",
}
ALICE_ID = "R-778-A"
BOB_ID = "R-778-B"
CHARLIE_ID = "R-793-C"
ERIC_ID = "R-793-ERIC"
TIMMY_ID = "R-793-TIMMY"
BOB_NAME = "Bob 778 API"


class _EmployeeSource:
    def __init__(self, *rows: ExternalEmployeeRecord) -> None:
        self._rows = rows

    def list_employees(self):
        return self._rows


class Issue793PermanentReductionRebuildIsolationTests(TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        work_queue_api.RequestWorkQueueApiIntegrationTests._seed_database(session)
        alice = session.get(Resource, ALICE_ID)
        bob = session.get(Resource, BOB_ID)
        assert alice is not None and bob is not None
        alice.external_id = "EMP-793-ALICE"
        bob.external_id = "EMP-793-BOB"
        session.add_all(
            [
                Resource(
                    id=CHARLIE_ID,
                    external_id="EMP-793-CHARLIE",
                    name="Charlie 793",
                    active=True,
                    erp_active=True,
                    erp_status="Active",
                    sort_order=3,
                ),
                Resource(
                    id=ERIC_ID,
                    external_id="EMP-793-ERIC",
                    name="ERIC FONTAINE",
                    resource_class="ELECTRICAL",
                    active=True,
                    erp_active=True,
                    erp_status="Active",
                    sort_order=4,
                ),
                Resource(
                    id=TIMMY_ID,
                    external_id="EMP-793-TIMMY",
                    name="TIMMY CURIE",
                    resource_class="ELECTRICAL",
                    active=True,
                    erp_active=True,
                    erp_status="Active",
                    sort_order=5,
                ),
            ]
        )
        session.flush()
        for resource_id in (CHARLIE_ID, ERIC_ID, TIMMY_ID):
            session.add(
                ResourceAvailabilityRule(
                    id=f"SCH-{resource_id}",
                    resource_id=resource_id,
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=work_queue_api.time(8, 0),
                    end_time=work_queue_api.time(16, 0),
                    active=True,
                )
            )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="issue-793-characterization.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _approve_two_slot_request(client: TestClient) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": "P-778-API",
                    "submit": True,
                    "desired_start": START,
                    "desired_end": END,
                    "resource_count": 2,
                    "estimated_hours": 80,
                    "confirmation": "Confirmée",
                    "description": "#793 same-entry two-slot characterization",
                }
            ),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "#793 characterize two stable sibling slots"},
        )
        assert approved.status_code == 200, approved.text
        return number

    @staticmethod
    def _approve_sequential_24_request(client: TestClient) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": "P-778-API",
                    "submit": True,
                    "lines": [
                        {
                            "position": 0,
                            "desired_start": START,
                            "desired_end": WEDNESDAY,
                            "desired_active_days": 3,
                            "estimated_hours": 24,
                            "required_resource_class": "ELECTRICAL",
                            "confirmation": "Confirmée",
                            "description": "#793 sequential reduction segment A",
                        },
                        {
                            "position": 1,
                            "desired_start": START,
                            "desired_end": WEDNESDAY,
                            "desired_active_days": 3,
                            "estimated_hours": 24,
                            "required_resource_class": "ELECTRICAL",
                            "confirmation": "Confirmée",
                            "description": "#793 sequential reduction segment B",
                        },
                    ],
                }
            ),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "#793 characterize ERIC/TIMMY sequential reduction"},
        )
        assert approved.status_code == 200, approved.text
        return number

    @staticmethod
    def _approve_two_line_request(client: TestClient) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": "P-778-API",
                    "submit": True,
                    "lines": [
                        {
                            "position": 0,
                            "desired_start": START,
                            "desired_end": END,
                            "estimated_hours": 40,
                            "confirmation": "Confirmée",
                            "description": "#793 line A",
                        },
                        {
                            "position": 1,
                            "desired_start": START,
                            "desired_end": END,
                            "estimated_hours": 40,
                            "confirmation": "Confirmée",
                            "description": "#793 line B",
                        },
                    ],
                }
            ),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "#793 characterize two independent lines"},
        )
        assert approved.status_code == 200, approved.text
        return number

    @staticmethod
    def _assignment_actions(client: TestClient, number: str) -> list[dict]:
        response = client.get(
            "/api/v1/planning/actions",
            params={"start": START, "end": END},
        )
        assert response.status_code == 200, response.text
        return sorted(
            [
                row
                for row in response.json()
                if row.get("demand_number") == number and row.get("kind") == "ASSIGNMENT"
            ],
            key=lambda row: str(row.get("segment_id") or ""),
        )

    @staticmethod
    def _assign(client: TestClient, segment_id: str, resource_id: str) -> None:
        response = client.post(
            f"/api/v1/segments/{segment_id}/assign",
            json={"resource_id": resource_id},
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def _place_from_drop(
        client: TestClient,
        *,
        segment_id: str,
        resource_id: str,
        dropped_date: str,
        key: str,
    ) -> dict:
        preview = client.post(
            f"/api/v1/segments/{segment_id}/place-from-drop/preview",
            json={
                "resource_id": resource_id,
                "dropped_date": dropped_date,
            },
        )
        assert preview.status_code == 200, preview.text
        preview_payload = preview.json()
        assert preview_payload["allowed"], preview_payload
        response = client.post(
            f"/api/v1/segments/{segment_id}/place-from-drop",
            headers={"Idempotency-Key": key},
            json={
                "resource_id": resource_id,
                "dropped_date": dropped_date,
                "expected_planning_version": preview_payload["planning_version"],
                "expected_approval_revision_id": preview_payload["approval_revision_id"],
                "expected_operational_version": preview_payload["operational_version"],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    @staticmethod
    def _counts_as_coverage(shift) -> bool:
        return str(shift.allocation_type or "").strip() != MISSING_ALLOCATION_TYPE

    @classmethod
    def _capture(
        cls,
        session,
        number: str,
        *,
        nominal_slot_hours: Decimal = Decimal("40.00"),
    ) -> dict:
        request = session.scalar(
            select(WorkforceRequest).where(
                WorkforceRequest.legacy_demand_number == number
            )
        )
        assert request is not None

        intents = tuple(
            session.scalars(
                select(RequestRequirementPlanningIntent)
                .where(
                    RequestRequirementPlanningIntent.workforce_request_id == request.id
                )
                .order_by(
                    RequestRequirementPlanningIntent.approved_entry_key,
                    RequestRequirementPlanningIntent.slot_index,
                )
            )
        )
        requirements = tuple(
            session.scalars(
                select(ResourceRequirement)
                .where(
                    ResourceRequirement.workforce_request_id == request.id,
                    ResourceRequirement.status != "Annulé",
                )
                .order_by(ResourceRequirement.id)
            )
        )
        requirement_by_id = {row.id: row for row in requirements}
        intent_repo = SqlRequestRequirementPlanningIntentRepository(session)

        slots = {}
        for intent in intents:
            requirement_id = str(intent.current_resource_requirement_id or "")
            requirement = requirement_by_id.get(requirement_id)
            if requirement is None:
                continue
            shift_rows = tuple(
                session.scalars(
                    select(Shift)
                    .where(Shift.resource_requirement_id == requirement.id)
                    .order_by(Shift.work_date, Shift.id)
                )
            )
            counted_rows = tuple(
                row for row in shift_rows if cls._counts_as_coverage(row)
            )
            covered = sum(
                (Decimal(str(row.hours)) for row in counted_rows),
                Decimal("0.00"),
            ).quantize(Decimal("0.01"))
            planned = Decimal(str(requirement.planned_hours)).quantize(
                Decimal("0.01")
            )
            effective_need = intent_repo.effective_need(
                planning_intent_id=intent.id,
                nominal_slot_hours=nominal_slot_hours,
            )
            slots[requirement.id] = {
                "requirement_id": requirement.id,
                "segment_id": requirement.legacy_segment_id or requirement.id,
                "planned_hours": planned,
                "effective_need": effective_need,
                "assigned_resource_id": requirement.assigned_resource_id,
                "planning_intent_id": intent.id,
                "slot_identity_key": intent.slot_identity_key,
                "approved_entry_key": intent.approved_entry_key,
                "slot_index": int(intent.slot_index),
                "source_request_line_id": intent.source_request_line_id,
                "intent_mode": intent.intent_mode,
                "target_mode": intent.target_mode,
                "target_resource_id": intent.target_resource_id,
                "placement_not_before_date": (
                    intent.placement_not_before_date.isoformat()
                    if intent.placement_not_before_date
                    else None
                ),
                "covered_hours": covered,
                "residual": max(effective_need - covered, Decimal("0.00")),
                "shifts": tuple(
                    {
                        "id": shift.id,
                        "allocation_id": shift.legacy_allocation_id or shift.id,
                        "requirement_id": shift.resource_requirement_id,
                        "resource_id": shift.resource_id,
                        "work_date": shift.work_date.isoformat(),
                        "hours": Decimal(str(shift.hours)).quantize(Decimal("0.01")),
                        "source": shift.source,
                        "locked": bool(shift.locked),
                        "allocation_type": shift.allocation_type,
                        "counted": cls._counts_as_coverage(shift),
                    }
                    for shift in shift_rows
                ),
            }

        reductions = tuple(
            {
                "id": row.id,
                "planning_intent_id": row.planning_intent_id,
                "resource_requirement_id": row.resource_requirement_id,
                "reduction_hours": Decimal(str(row.reduction_hours)).quantize(
                    Decimal("0.01")
                ),
                "effective_need_before": Decimal(
                    str(row.effective_need_before)
                ).quantize(Decimal("0.01")),
                "effective_need_after": Decimal(
                    str(row.effective_need_after)
                ).quantize(Decimal("0.01")),
                "original_shift_id": row.original_shift_id,
            }
            for row in session.scalars(
                select(RequestCompletionReduction)
                .where(
                    RequestCompletionReduction.workforce_request_id == request.id
                )
                .order_by(
                    RequestCompletionReduction.created_at,
                    RequestCompletionReduction.id,
                )
            )
        )

        weekly = defaultdict(lambda: Decimal("0.00"))
        for values in slots.values():
            for shift in values["shifts"]:
                if shift["counted"]:
                    weekly[str(shift["resource_id"])] += shift["hours"]

        return {
            "request_id": request.id,
            "requirement_ids": frozenset(row.id for row in requirements),
            "intent_ids": frozenset(row.id for row in intents),
            "bound_requirement_ids": frozenset(
                str(row.current_resource_requirement_id)
                for row in intents
                if row.current_resource_requirement_id
            ),
            "slots": slots,
            "reductions": reductions,
            "weekly": {
                key: value.quantize(Decimal("0.01"))
                for key, value in weekly.items()
            },
        }

    @staticmethod
    def _by_target(state: dict, resource_id: str) -> dict:
        return next(
            slot
            for slot in state["slots"].values()
            if slot["assigned_resource_id"] == resource_id
        )

    @staticmethod
    def _target_shift(slot: dict) -> dict:
        return next(
            shift
            for shift in slot["shifts"]
            if shift["work_date"] == THURSDAY and shift["counted"]
        )

    @staticmethod
    def _preview(client: TestClient, allocation_id: str) -> dict:
        response = client.get(
            f"/api/v1/allocations/{allocation_id}/work-queue-preview"
        )
        assert response.status_code == 200, response.text
        return response.json()

    @staticmethod
    def _reduce(client: TestClient, allocation_id: str, preview: dict, key: str):
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
    def _assert_two_bound_slots(test: TestCase, state: dict) -> None:
        test.assertEqual(len(state["requirement_ids"]), 2)
        test.assertEqual(len(state["intent_ids"]), 2)
        test.assertEqual(state["bound_requirement_ids"], state["requirement_ids"])
        test.assertEqual(set(state["slots"]), set(state["requirement_ids"]))

    @staticmethod
    def _weekly_tuple(state: dict) -> tuple[Decimal, Decimal, Decimal]:
        return (
            state["weekly"].get(ALICE_ID, Decimal("0.00")),
            state["weekly"].get(BOB_ID, Decimal("0.00")),
            state["weekly"].get(CHARLIE_ID, Decimal("0.00")),
        )

    @classmethod
    def _assert_weekly(
        cls,
        test: TestCase,
        state: dict,
        *,
        alice: str,
        bob: str,
        charlie: str,
    ) -> None:
        test.assertEqual(
            cls._weekly_tuple(state),
            (Decimal(alice), Decimal(bob), Decimal(charlie)),
        )

    @staticmethod
    def _make_manual_locked(
        client: TestClient,
        *,
        allocation_id: str,
        day: str,
        resource_id: str,
    ) -> None:
        response = client.put(
            f"/api/v1/allocations/{allocation_id}",
            json={
                "resource_id": resource_id,
                "day": day,
                "hours": 8,
                "outside_standard_hours": False,
                "note": "#793 preserved Bob shift",
                "confirmation": None,
            },
        )
        assert response.status_code == 200, response.text

    @classmethod
    def _lock_four_bob_shifts(
        cls,
        client: TestClient,
        factory,
        number: str,
    ) -> None:
        for day in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"):
            with factory() as session:
                current = cls._capture(session, number)
            bob = cls._by_target(current, BOB_ID)
            shift = next(
                row
                for row in bob["shifts"]
                if row["work_date"] == day and row["counted"]
            )
            cls._make_manual_locked(
                client,
                allocation_id=shift["allocation_id"],
                day=day,
                resource_id=BOB_ID,
            )

    @staticmethod
    def _sync_charlie_name(factory) -> None:
        with factory.begin() as session:
            result = EmployeeSyncService(
                _EmployeeSource(
                    ExternalEmployeeRecord(
                        external_id="EMP-793-CHARLIE",
                        display_name=BOB_NAME,
                        erp_status="Active",
                        erp_active=True,
                    )
                ),
                SqlEmployeeSyncRepository(session),
            ).synchronize()
        assert result.updated == 1

    @staticmethod
    def _resource_identity(factory, resource_id: str) -> dict:
        with factory() as session:
            resource = session.get(Resource, resource_id)
            assert resource is not None
            schedule_count = len(
                tuple(
                    session.scalars(
                        select(ResourceAvailabilityRule).where(
                            ResourceAvailabilityRule.resource_id == resource_id,
                            ResourceAvailabilityRule.active.is_(True),
                            ResourceAvailabilityRule.availability_type
                            == "Horaire standard",
                        )
                    )
                )
            )
            return {
                "id": resource.id,
                "external_id": resource.external_id,
                "name": resource.name,
                "active": bool(resource.active),
                "erp_active": bool(resource.erp_active),
                "schedule_count": schedule_count,
            }

    def _exercise_explicit(self, database_url: str, *, two_lines: bool) -> dict:
        app = create_api_app(
            database_url,
            actor_name="admin-793",
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
        )
        engine = create_sql_engine(database_url)
        factory = create_session_factory(engine)
        boundary_before_rebuild: list[dict] = []
        preserve_calls: list[tuple[str, ...]] = []
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                number = (
                    self._approve_two_line_request(client)
                    if two_lines
                    else self._approve_two_slot_request(client)
                )
                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 2)
                self._assign(client, actions[0]["segment_id"], ALICE_ID)
                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 1)
                self._assign(client, actions[0]["segment_id"], BOB_ID)

                with factory() as session:
                    before = self._capture(session, number)
                self._assert_two_bound_slots(self, before)
                a_before = self._by_target(before, ALICE_ID)
                b_before = self._by_target(before, BOB_ID)
                self.assertEqual(a_before["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_before["planned_hours"], Decimal("40.00"))
                self.assertEqual(a_before["effective_need"], Decimal("40.00"))
                self.assertEqual(b_before["effective_need"], Decimal("40.00"))
                self.assertEqual(a_before["covered_hours"], Decimal("40.00"))
                self.assertEqual(b_before["covered_hours"], Decimal("40.00"))
                self.assertEqual(a_before["target_mode"], "EXPLICIT_RESOURCE")
                self.assertEqual(a_before["target_resource_id"], ALICE_ID)
                self.assertEqual(b_before["target_mode"], "EXPLICIT_RESOURCE")
                self.assertEqual(b_before["target_resource_id"], BOB_ID)
                self.assertEqual(len(a_before["shifts"]), 5)
                self.assertEqual(len(b_before["shifts"]), 5)
                self.assertEqual(
                    before["requirement_ids"],
                    frozenset(
                        {
                            a_before["requirement_id"],
                            b_before["requirement_id"],
                        }
                    ),
                )
                self.assertEqual(
                    before["intent_ids"],
                    frozenset(
                        {
                            a_before["planning_intent_id"],
                            b_before["planning_intent_id"],
                        }
                    ),
                )
                for slot, resource_id in (
                    (a_before, ALICE_ID),
                    (b_before, BOB_ID),
                ):
                    self.assertEqual(
                        {row["work_date"] for row in slot["shifts"]},
                        EXPECTED_DAYS,
                    )
                    self.assertEqual(
                        tuple(row["hours"] for row in slot["shifts"]),
                        (Decimal("8.00"),) * 5,
                    )
                    self.assertEqual(
                        {row["resource_id"] for row in slot["shifts"]},
                        {resource_id},
                    )
                    self.assertEqual(
                        {row["source"] for row in slot["shifts"]},
                        {"AUTO"},
                    )
                    self.assertEqual(
                        {row["locked"] for row in slot["shifts"]},
                        {False},
                    )
                    self.assertTrue(all(row["counted"] for row in slot["shifts"]))
                self._assert_weekly(
                    self,
                    before,
                    alice="40.00",
                    bob="40.00",
                    charlie="0.00",
                )

                target = self._target_shift(a_before)
                preview = self._preview(client, target["allocation_id"])
                original_rebuild = SqlPlanningCommandAdapter.rebuild

                def traced_rebuild(
                    adapter,
                    *,
                    preserve_shift_ids=(),
                    temporary_excluded_dates_by_segment=None,
                ):
                    preserve_calls.append(
                        tuple(str(value) for value in preserve_shift_ids)
                    )
                    boundary_before_rebuild.append(
                        self._capture(adapter._session, number)
                    )
                    return original_rebuild(
                        adapter,
                        preserve_shift_ids=preserve_shift_ids,
                        temporary_excluded_dates_by_segment=(
                            temporary_excluded_dates_by_segment
                        ),
                    )

                with patch.object(
                    SqlPlanningCommandAdapter,
                    "rebuild",
                    new=traced_rebuild,
                ):
                    reduced = self._reduce(
                        client,
                        target["allocation_id"],
                        preview,
                        (
                            "issue-793-explicit-two-lines"
                            if two_lines
                            else "issue-793-explicit-two-slot"
                        ),
                    )
                self.assertEqual(reduced.status_code, 200, reduced.text)

                self.assertEqual(len(boundary_before_rebuild), 1)
                pre_rebuild = boundary_before_rebuild[0]
                self._assert_two_bound_slots(self, pre_rebuild)
                a_pre = pre_rebuild["slots"][a_before["requirement_id"]]
                b_pre = pre_rebuild["slots"][b_before["requirement_id"]]
                self.assertEqual(a_pre["planned_hours"], Decimal("32.00"))
                self.assertEqual(a_pre["effective_need"], Decimal("32.00"))
                self.assertEqual(b_pre["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_pre["effective_need"], Decimal("40.00"))
                self.assertEqual(b_pre["assigned_resource_id"], BOB_ID)
                self.assertEqual(b_pre["target_resource_id"], BOB_ID)

                with factory() as session:
                    after = self._capture(session, number)
                    removed = session.get(Shift, target["id"])
                self.assertIsNone(removed)
                self._assert_two_bound_slots(self, after)
                a_after = after["slots"][a_before["requirement_id"]]
                b_after = after["slots"][b_before["requirement_id"]]
                self.assertEqual(a_after["planned_hours"], Decimal("32.00"))
                self.assertEqual(a_after["effective_need"], Decimal("32.00"))
                self.assertEqual(a_after["covered_hours"], Decimal("32.00"))
                self.assertEqual(b_after["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_after["effective_need"], Decimal("40.00"))
                self.assertEqual(b_after["covered_hours"], Decimal("40.00"))
                self.assertEqual(b_after["assigned_resource_id"], BOB_ID)
                self.assertEqual(b_after["target_mode"], "EXPLICIT_RESOURCE")
                self.assertEqual(b_after["target_resource_id"], BOB_ID)
                self.assertEqual(
                    {row["resource_id"] for row in b_after["shifts"]},
                    {BOB_ID},
                )
                self.assertEqual(b_after["residual"], Decimal("0.00"))
                self._assert_weekly(
                    self,
                    after,
                    alice="32.00",
                    bob="40.00",
                    charlie="0.00",
                )

                reduction = after["reductions"]
                self.assertEqual(len(reduction), 1)
                self.assertEqual(
                    reduction[0]["planning_intent_id"],
                    a_before["planning_intent_id"],
                )
                self.assertEqual(
                    reduction[0]["resource_requirement_id"],
                    a_before["requirement_id"],
                )
                self.assertEqual(
                    reduction[0]["original_shift_id"],
                    target["id"],
                )
                self.assertEqual(
                    reduction[0]["reduction_hours"],
                    Decimal("8.00"),
                )
                self.assertEqual(
                    reduction[0]["effective_need_before"],
                    Decimal("40.00"),
                )
                self.assertEqual(
                    reduction[0]["effective_need_after"],
                    Decimal("32.00"),
                )

                expected_preserved = {
                    row["id"]
                    for row in a_before["shifts"]
                    if row["id"] != target["id"]
                }
                self.assertEqual(len(preserve_calls), 1)
                self.assertEqual(set(preserve_calls[0]), expected_preserved)
                old_b_ids = {row["id"] for row in b_before["shifts"]}
                new_b_ids = {row["id"] for row in b_after["shifts"]}
                self.assertTrue(old_b_ids.isdisjoint(preserve_calls[0]))
                self.assertTrue(old_b_ids.isdisjoint(new_b_ids))

                with factory.begin() as session:
                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                with factory() as session:
                    after_resync = self._capture(session, number)
                self._assert_two_bound_slots(self, after_resync)
                a_resync = after_resync["slots"][a_before["requirement_id"]]
                b_resync = after_resync["slots"][b_before["requirement_id"]]
                self.assertEqual(a_resync["effective_need"], Decimal("32.00"))
                self.assertEqual(b_resync["effective_need"], Decimal("40.00"))
                self.assertEqual(b_resync["covered_hours"], Decimal("40.00"))
                self.assertEqual(b_resync["assigned_resource_id"], BOB_ID)
                self.assertEqual(b_resync["target_resource_id"], BOB_ID)
                self.assertEqual(
                    {row["resource_id"] for row in b_resync["shifts"]},
                    {BOB_ID},
                )
                self._assert_weekly(
                    self,
                    after_resync,
                    alice="32.00",
                    bob="40.00",
                    charlie="0.00",
                )

                with factory() as session:
                    reloaded = self._capture(session, number)
                self._assert_two_bound_slots(self, reloaded)
                a_reload = reloaded["slots"][a_before["requirement_id"]]
                b_reload = reloaded["slots"][b_before["requirement_id"]]
                self.assertEqual(a_reload["effective_need"], Decimal("32.00"))
                self.assertEqual(b_reload["effective_need"], Decimal("40.00"))
                self.assertEqual(b_reload["covered_hours"], Decimal("40.00"))
                self.assertEqual(b_reload["assigned_resource_id"], BOB_ID)
                self.assertEqual(b_reload["target_resource_id"], BOB_ID)
                self.assertEqual(b_reload["residual"], Decimal("0.00"))
                self._assert_weekly(
                    self,
                    reloaded,
                    alice="32.00",
                    bob="40.00",
                    charlie="0.00",
                )

                actions_after = self._assignment_actions(client, number)
                self.assertEqual(actions_after, [])

                return {
                    "number": number,
                    "before": before,
                    "pre_rebuild": pre_rebuild,
                    "after": after,
                    "after_resync": after_resync,
                    "reloaded": reloaded,
                    "preserve_shift_ids": preserve_calls[0],
                }
        finally:
            engine.dispose()

    def _exercise_duplicate_name(
        self,
        database_url: str,
        *,
        lock_four_bob: bool,
    ) -> dict:
        app = create_api_app(
            database_url,
            actor_name="admin-793-collision",
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
        )
        engine = create_sql_engine(database_url)
        factory = create_session_factory(engine)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve_two_slot_request(client)
                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 2)
                self._assign(client, actions[0]["segment_id"], ALICE_ID)
                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 1)
                self._assign(client, actions[0]["segment_id"], BOB_ID)

                if lock_four_bob:
                    self._lock_four_bob_shifts(client, factory, number)

                self._sync_charlie_name(factory)
                bob_identity = self._resource_identity(factory, BOB_ID)
                charlie_identity = self._resource_identity(factory, CHARLIE_ID)
                self.assertNotEqual(
                    bob_identity["id"],
                    charlie_identity["id"],
                )
                self.assertNotEqual(
                    bob_identity["external_id"],
                    charlie_identity["external_id"],
                )
                self.assertEqual(
                    bob_identity["name"],
                    charlie_identity["name"],
                )
                for identity in (bob_identity, charlie_identity):
                    self.assertTrue(identity["active"])
                    self.assertTrue(identity["erp_active"])
                    self.assertGreaterEqual(identity["schedule_count"], 1)

                with factory() as session:
                    before = self._capture(session, number)
                self._assert_two_bound_slots(self, before)
                a_before = self._by_target(before, ALICE_ID)
                b_before = self._by_target(before, BOB_ID)
                self.assertEqual(a_before["effective_need"], Decimal("40.00"))
                self.assertEqual(b_before["effective_need"], Decimal("40.00"))
                self.assertEqual(a_before["covered_hours"], Decimal("40.00"))
                self.assertEqual(b_before["covered_hours"], Decimal("40.00"))
                self._assert_weekly(
                    self,
                    before,
                    alice="40.00",
                    bob="40.00",
                    charlie="0.00",
                )
                self.assertEqual(b_before["target_mode"], "EXPLICIT_RESOURCE")
                self.assertEqual(b_before["target_resource_id"], BOB_ID)

                if lock_four_bob:
                    manual = [
                        row
                        for row in b_before["shifts"]
                        if row["source"] == "MANUAL" and row["locked"]
                    ]
                    automatic = [
                        row
                        for row in b_before["shifts"]
                        if row["source"] == "AUTO" and not row["locked"]
                    ]
                    self.assertEqual(len(manual), 4)
                    self.assertEqual(len(automatic), 1)
                else:
                    self.assertEqual(
                        {row["source"] for row in b_before["shifts"]},
                        {"AUTO"},
                    )
                    self.assertEqual(
                        {row["locked"] for row in b_before["shifts"]},
                        {False},
                    )

                target = self._target_shift(a_before)
                preview = self._preview(client, target["allocation_id"])
                reduced = self._reduce(
                    client,
                    target["allocation_id"],
                    preview,
                    (
                        "issue-793-duplicate-name-preserved"
                        if lock_four_bob
                        else "issue-793-duplicate-name-unlocked"
                    ),
                )
                self.assertEqual(reduced.status_code, 200, reduced.text)

                with factory() as session:
                    after = self._capture(session, number)
                a_after = after["slots"][a_before["requirement_id"]]
                b_after = after["slots"][b_before["requirement_id"]]

                # Need identity remains correct: this is not sibling need contamination.
                self.assertEqual(a_after["effective_need"], Decimal("32.00"))
                self.assertEqual(b_after["effective_need"], Decimal("40.00"))
                self.assertEqual(b_after["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_after["target_mode"], "EXPLICIT_RESOURCE")
                self.assertEqual(b_after["target_resource_id"], BOB_ID)

                post_reduction_resources = {
                    row["resource_id"] for row in b_after["shifts"]
                }
                post_reduction_weekly = self._weekly_tuple(after)

                with factory.begin() as session:
                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                with factory() as session:
                    after_resync = self._capture(session, number)
                b_resync = after_resync["slots"][b_before["requirement_id"]]
                self.assertEqual(b_resync["effective_need"], Decimal("40.00"))
                self.assertEqual(b_resync["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_resync["target_resource_id"], BOB_ID)
                post_resync_resources = {
                    row["resource_id"] for row in b_resync["shifts"]
                }
                post_resync_weekly = self._weekly_tuple(after_resync)

                # Correct ownership expectation. Current main is expected to fail here.
                # Both states are included in one assertion so the red regression records
                # whether the Bob -> Charlie corruption persists or recurs after resync.
                expected_weekly = (
                    Decimal("32.00"),
                    Decimal("40.00"),
                    Decimal("0.00"),
                )
                self.assertEqual(
                    (post_reduction_weekly, post_resync_weekly),
                    (expected_weekly, expected_weekly),
                    msg=(
                        "duplicate Resource.name must never move B coverage from "
                        "canonical Bob id to Charlie id"
                    ),
                )
                self.assertEqual(
                    (post_reduction_resources, post_resync_resources),
                    ({BOB_ID}, {BOB_ID}),
                )

                return {
                    "number": number,
                    "bob_identity": bob_identity,
                    "charlie_identity": charlie_identity,
                    "before": before,
                    "after": after,
                    "after_resync": after_resync,
                }
        finally:
            engine.dispose()

    def test_non_counted_diagnostic_shift_contributes_neither_coverage_nor_weekly_totals(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-793-missing",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_two_slot_request(client)
                    with factory.begin() as session:
                        state = self._capture(session, number)
                        self._assert_two_bound_slots(self, state)
                        slot = next(iter(state["slots"].values()))
                        session.add(
                            Shift(
                                id="S-793-MISSING",
                                resource_requirement_id=slot["requirement_id"],
                                resource_id=CHARLIE_ID,
                                work_date=work_queue_api.date.fromisoformat(START),
                                hours=Decimal("8.00"),
                                allocation_type=MISSING_ALLOCATION_TYPE,
                                source="AUTO",
                                locked=False,
                            )
                        )
                    with factory() as session:
                        captured = self._capture(session, number)
                    target = captured["slots"][slot["requirement_id"]]
                    self.assertEqual(target["covered_hours"], Decimal("0.00"))
                    self.assertEqual(
                        captured["weekly"].get(CHARLIE_ID, Decimal("0.00")),
                        Decimal("0.00"),
                    )
                    diagnostic = next(
                        row for row in target["shifts"] if row["id"] == "S-793-MISSING"
                    )
                    self.assertFalse(diagnostic["counted"])
            finally:
                engine.dispose()

    def test_same_entry_two_slot_explicit_assignments_remain_isolated(self) -> None:
        with TemporaryDirectory() as directory:
            result = self._exercise_explicit(
                self._database(directory),
                two_lines=False,
            )
        before = result["before"]
        intents = sorted(
            (
                slot["planning_intent_id"],
                slot["approved_entry_key"],
                slot["slot_index"],
            )
            for slot in before["slots"].values()
        )
        self.assertEqual(len(intents), 2)
        self.assertEqual(intents[0][1], intents[1][1])
        self.assertEqual({intents[0][2], intents[1][2]}, {0, 1})
        self.assertNotEqual(intents[0][0], intents[1][0])

    def test_two_independent_lines_explicit_assignments_remain_isolated(self) -> None:
        with TemporaryDirectory() as directory:
            result = self._exercise_explicit(
                self._database(directory),
                two_lines=True,
            )
        slots = tuple(result["before"]["slots"].values())
        self.assertEqual(len(slots), 2)
        self.assertNotEqual(
            slots[0]["source_request_line_id"],
            slots[1]["source_request_line_id"],
        )
        self.assertNotEqual(
            slots[0]["approved_entry_key"],
            slots[1]["approved_entry_key"],
        )
        self.assertNotEqual(
            slots[0]["requirement_id"],
            slots[1]["requirement_id"],
        )
        self.assertNotEqual(
            slots[0]["planning_intent_id"],
            slots[1]["planning_intent_id"],
        )

    def test_targetless_two_slot_request_does_not_fabricate_automatic_resource_choice(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-793-auto",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_two_slot_request(client)
                    with factory() as session:
                        before = self._capture(session, number)
                    self._assert_two_bound_slots(self, before)
                    before_identity = {
                        (
                            slot["planning_intent_id"],
                            slot["requirement_id"],
                            slot["slot_index"],
                        )
                        for slot in before["slots"].values()
                    }
                    self.assertEqual(
                        {row[2] for row in before_identity},
                        {0, 1},
                    )
                    for slot in before["slots"].values():
                        self.assertIsNone(slot["assigned_resource_id"])
                        self.assertEqual(slot["target_mode"], "EXPLICIT_NONE")
                        self.assertIsNone(slot["target_resource_id"])
                        self.assertEqual(slot["shifts"], ())

                    rebuilt = client.post("/api/v1/planning/rebuild")
                    self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                    with factory() as session:
                        after = self._capture(session, number)
                    self._assert_two_bound_slots(self, after)
                    after_identity = {
                        (
                            slot["planning_intent_id"],
                            slot["requirement_id"],
                            slot["slot_index"],
                        )
                        for slot in after["slots"].values()
                    }
                    self.assertEqual(after_identity, before_identity)
                    for slot in after["slots"].values():
                        self.assertIsNone(slot["assigned_resource_id"])
                        self.assertEqual(slot["target_mode"], "EXPLICIT_NONE")
                        self.assertIsNone(slot["target_resource_id"])
                        self.assertEqual(slot["shifts"], ())
            finally:
                engine.dispose()

    def test_dated_drop_sequential_reductions_characterize_survivor_regeneration(self) -> None:
        """Phase 2C: capture the exact current sequential-reduction defect, no correction."""

        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-793-sequential",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    number = self._approve_sequential_24_request(client)
                    actions = self._assignment_actions(client, number)
                    self.assertEqual(len(actions), 2)

                    self._place_from_drop(
                        client,
                        segment_id=actions[0]["segment_id"],
                        resource_id=ERIC_ID,
                        dropped_date=START,
                        key="issue-793-sequential-drop-eric",
                    )
                    actions = self._assignment_actions(client, number)
                    self.assertEqual(len(actions), 1)
                    self._place_from_drop(
                        client,
                        segment_id=actions[0]["segment_id"],
                        resource_id=TIMMY_ID,
                        dropped_date=START,
                        key="issue-793-sequential-drop-timmy",
                    )

                    with factory() as session:
                        initial = self._capture(
                            session,
                            number,
                            nominal_slot_hours=Decimal("24.00"),
                        )
                    eric_initial = self._by_target(initial, ERIC_ID)
                    timmy_initial = self._by_target(initial, TIMMY_ID)
                    for slot, resource_id in (
                        (eric_initial, ERIC_ID),
                        (timmy_initial, TIMMY_ID),
                    ):
                        self.assertEqual(slot["effective_need"], Decimal("24.00"))
                        self.assertEqual(slot["planned_hours"], Decimal("24.00"))
                        self.assertEqual(slot["covered_hours"], Decimal("24.00"))
                        self.assertEqual(slot["intent_mode"], "AUTO")
                        self.assertEqual(slot["target_mode"], "EXPLICIT_RESOURCE")
                        self.assertEqual(slot["target_resource_id"], resource_id)
                        self.assertEqual(slot["placement_not_before_date"], START)
                        self.assertEqual(
                            tuple(
                                (row["work_date"], row["hours"])
                                for row in slot["shifts"]
                                if row["counted"]
                            ),
                            (
                                (START, Decimal("8.00")),
                                ("2026-11-03", Decimal("8.00")),
                                (WEDNESDAY, Decimal("8.00")),
                            ),
                        )

                    eric_wed = next(
                        row
                        for row in eric_initial["shifts"]
                        if row["work_date"] == WEDNESDAY and row["counted"]
                    )
                    first_preview = self._preview(client, eric_wed["allocation_id"])
                    first_preserve_calls: list[tuple[str, ...]] = []
                    original_rebuild = SqlPlanningCommandAdapter.rebuild

                    def trace_first(
                        adapter,
                        *,
                        preserve_shift_ids=(),
                        temporary_excluded_dates_by_segment=None,
                    ):
                        first_preserve_calls.append(tuple(preserve_shift_ids))
                        return original_rebuild(
                            adapter,
                            preserve_shift_ids=preserve_shift_ids,
                            temporary_excluded_dates_by_segment=(
                                temporary_excluded_dates_by_segment
                            ),
                        )

                    with patch.object(
                        SqlPlanningCommandAdapter,
                        "rebuild",
                        new=trace_first,
                    ):
                        reduced_eric = self._reduce(
                            client,
                            eric_wed["allocation_id"],
                            first_preview,
                            "issue-793-sequential-reduce-eric",
                        )
                    self.assertEqual(reduced_eric.status_code, 200, reduced_eric.text)

                    with factory() as session:
                        after_eric = self._capture(
                            session,
                            number,
                            nominal_slot_hours=Decimal("24.00"),
                        )
                        eric_release_count = len(
                            tuple(
                                session.scalars(
                                    select(PlanningWorkQueueRelease).where(
                                        PlanningWorkQueueRelease.planning_intent_id
                                        == eric_initial["planning_intent_id"]
                                    )
                                )
                            )
                        )
                    eric_after_first = after_eric["slots"][
                        eric_initial["requirement_id"]
                    ]
                    timmy_after_first = after_eric["slots"][
                        timmy_initial["requirement_id"]
                    ]
                    eric_survivor_ids = {
                        row["id"] for row in eric_after_first["shifts"] if row["counted"]
                    }
                    self.assertEqual(eric_after_first["effective_need"], Decimal("16.00"))
                    self.assertEqual(eric_after_first["planned_hours"], Decimal("16.00"))
                    self.assertEqual(eric_after_first["covered_hours"], Decimal("16.00"))
                    self.assertEqual(
                        tuple(
                            (row["work_date"], row["hours"])
                            for row in eric_after_first["shifts"]
                            if row["counted"]
                        ),
                        (
                            (START, Decimal("8.00")),
                            ("2026-11-03", Decimal("8.00")),
                        ),
                    )
                    self.assertEqual(eric_after_first["intent_mode"], "AUTO")
                    self.assertEqual(eric_after_first["placement_not_before_date"], START)
                    eric_reductions = [
                        row
                        for row in after_eric["reductions"]
                        if row["planning_intent_id"] == eric_initial["planning_intent_id"]
                    ]
                    self.assertEqual(len(eric_reductions), 1)
                    self.assertEqual(eric_reductions[0]["original_shift_id"], eric_wed["id"])
                    self.assertEqual(eric_reductions[0]["reduction_hours"], Decimal("8.00"))
                    self.assertEqual(eric_reductions[0]["effective_need_before"], Decimal("24.00"))
                    self.assertEqual(eric_reductions[0]["effective_need_after"], Decimal("16.00"))
                    self.assertEqual(eric_release_count, 0)
                    self.assertEqual(len(first_preserve_calls), 1)
                    self.assertEqual(set(first_preserve_calls[0]), eric_survivor_ids)
                    self.assertEqual(timmy_after_first["effective_need"], Decimal("24.00"))
                    self.assertEqual(timmy_after_first["covered_hours"], Decimal("24.00"))

                    timmy_wed = next(
                        row
                        for row in timmy_after_first["shifts"]
                        if row["work_date"] == WEDNESDAY and row["counted"]
                    )
                    second_preview = self._preview(client, timmy_wed["allocation_id"])
                    second_preserve_calls: list[tuple[str, ...]] = []

                    def trace_second(
                        adapter,
                        *,
                        preserve_shift_ids=(),
                        temporary_excluded_dates_by_segment=None,
                    ):
                        second_preserve_calls.append(tuple(preserve_shift_ids))
                        return original_rebuild(
                            adapter,
                            preserve_shift_ids=preserve_shift_ids,
                            temporary_excluded_dates_by_segment=(
                                temporary_excluded_dates_by_segment
                            ),
                        )

                    with patch.object(
                        SqlPlanningCommandAdapter,
                        "rebuild",
                        new=trace_second,
                    ):
                        reduced_timmy = self._reduce(
                            client,
                            timmy_wed["allocation_id"],
                            second_preview,
                            "issue-793-sequential-reduce-timmy",
                        )
                    self.assertEqual(reduced_timmy.status_code, 200, reduced_timmy.text)
                    self.assertEqual(len(second_preserve_calls), 1)
                    timmy_expected_survivors = {
                        row["id"]
                        for row in timmy_after_first["shifts"]
                        if row["id"] != timmy_wed["id"] and row["counted"]
                    }
                    self.assertEqual(
                        set(second_preserve_calls[0]),
                        timmy_expected_survivors,
                    )
                    self.assertTrue(
                        eric_survivor_ids.isdisjoint(second_preserve_calls[0])
                    )

                    with factory() as session:
                        after_timmy = self._capture(
                            session,
                            number,
                            nominal_slot_hours=Decimal("24.00"),
                        )
                    eric_after_second = after_timmy["slots"][
                        eric_initial["requirement_id"]
                    ]
                    timmy_after_second = after_timmy["slots"][
                        timmy_initial["requirement_id"]
                    ]

                    # Current defect: the prior ERIC survivors are disposable AUTO rows.
                    self.assertTrue(
                        eric_survivor_ids.isdisjoint(
                            {row["id"] for row in eric_after_second["shifts"]}
                        )
                    )
                    self.assertEqual(eric_after_second["effective_need"], Decimal("16.00"))
                    self.assertEqual(timmy_after_second["effective_need"], Decimal("16.00"))
                    timmy_reductions = [
                        row
                        for row in after_timmy["reductions"]
                        if row["planning_intent_id"] == timmy_initial["planning_intent_id"]
                    ]
                    self.assertEqual(len(timmy_reductions), 1)
                    self.assertEqual(timmy_reductions[0]["original_shift_id"], timmy_wed["id"])
                    self.assertEqual(timmy_reductions[0]["reduction_hours"], Decimal("8.00"))
                    self.assertEqual(timmy_reductions[0]["effective_need_before"], Decimal("24.00"))
                    self.assertEqual(timmy_reductions[0]["effective_need_after"], Decimal("16.00"))
                    self.assertEqual(eric_after_second["target_resource_id"], ERIC_ID)
                    self.assertEqual(
                        {row["resource_id"] for row in eric_after_second["shifts"]},
                        {ERIC_ID},
                    )
                    self.assertEqual(
                        tuple(
                            (row["work_date"], row["hours"])
                            for row in eric_after_second["shifts"]
                            if row["counted"]
                        ),
                        (
                            (START, Decimal("5.33")),
                            ("2026-11-03", Decimal("5.33")),
                            (WEDNESDAY, Decimal("5.34")),
                        ),
                    )
                    self.assertEqual(eric_after_second["covered_hours"], Decimal("16.00"))
                    with factory() as session:
                        raw_sqlite_after_second = tuple(
                            Decimal(str(row[0]))
                            for row in session.connection().exec_driver_sql(
                                "SELECT hours FROM shifts "
                                "WHERE resource_requirement_id = ? "
                                "ORDER BY work_date, id",
                                (eric_initial["requirement_id"],),
                            ).all()
                        )
                    self.assertEqual(
                        raw_sqlite_after_second,
                        (
                            Decimal("5.33"),
                            Decimal("5.33"),
                            Decimal("5.34"),
                        ),
                    )
                    self.assertTrue(
                        all(
                            value == value.quantize(Decimal("0.01"))
                            and value >= Decimal("0.01")
                            for value in raw_sqlite_after_second
                        )
                    )

                    regenerated = next(
                        row
                        for row in eric_after_second["shifts"]
                        if row["work_date"] == WEDNESDAY and row["counted"]
                    )
                    third_preview = self._preview(client, regenerated["allocation_id"])
                    self.assertTrue(third_preview["permanent_reduction_allowed"])
                    self.assertEqual(
                        Decimal(str(third_preview["effective_need"])).quantize(Decimal("0.01")),
                        Decimal("16.00"),
                    )
                    self.assertEqual(
                        Decimal(str(third_preview["covered_hours"])).quantize(Decimal("0.01")),
                        Decimal("16.00"),
                    )
                    self.assertEqual(regenerated["hours"], Decimal("5.34"))
                    self.assertEqual(
                        Decimal(str(third_preview["effective_need_after_reduction"])).quantize(
                            Decimal("0.01")
                        ),
                        Decimal("10.66"),
                    )
                    survivor_hours = sum(
                        (
                            row["hours"]
                            for row in eric_after_second["shifts"]
                            if row["id"] != regenerated["id"] and row["counted"]
                        ),
                        Decimal("0.00"),
                    )
                    self.assertEqual(survivor_hours, Decimal("10.66"))
                    self.assertEqual(
                        Decimal(str(third_preview["remaining_after_reduction"])).quantize(
                            Decimal("0.01")
                        ),
                        Decimal("0.00"),
                    )

                    third_key = "issue-793-sequential-reduce-regenerated-eric"
                    with factory() as session:
                        request_before_third = session.scalar(
                            select(WorkforceRequest).where(
                                WorkforceRequest.legacy_demand_number == number
                            )
                        )
                        self.assertIsNotNone(request_before_third)
                        request_version_before_third = int(
                            request_before_third.aggregate_version or 1
                        )
                        planning_version_before_third = (
                            SqlPlanningMutationVersionRepository(session).current_version()
                        )

                    rebuild_exceptions: list[dict[str, str]] = []
                    original_third_rebuild = SqlPlanningCommandAdapter.rebuild

                    def trace_third_rebuild(
                        adapter,
                        *,
                        preserve_shift_ids=(),
                        temporary_excluded_dates_by_segment=None,
                    ):
                        try:
                            return original_third_rebuild(
                                adapter,
                                preserve_shift_ids=preserve_shift_ids,
                                temporary_excluded_dates_by_segment=(
                                    temporary_excluded_dates_by_segment
                                ),
                            )
                        except Exception as exc:
                            rebuild_exceptions.append(
                                {
                                    "type": type(exc).__name__,
                                    "message": str(exc),
                                    "repr": repr(exc),
                                }
                            )
                            raise

                    with patch.object(
                        SqlPlanningCommandAdapter,
                        "rebuild",
                        new=trace_third_rebuild,
                    ):
                        third_response = self._reduce(
                            client,
                            regenerated["allocation_id"],
                            third_preview,
                            third_key,
                        )

                    self.assertEqual(third_response.status_code, 200, third_response.text)
                    third_payload = third_response.json()
                    self.assertEqual(rebuild_exceptions, [])
                    self.assertEqual(
                        Decimal(str(third_payload["effective_need_after"])).quantize(
                            Decimal("0.01")
                        ),
                        Decimal("10.66"),
                    )
                    self.assertEqual(
                        Decimal(str(third_payload["covered_hours_after"])).quantize(
                            Decimal("0.01")
                        ),
                        Decimal("10.66"),
                    )
                    self.assertEqual(
                        Decimal(str(third_payload["remaining_hours_after"])).quantize(
                            Decimal("0.01")
                        ),
                        Decimal("0.00"),
                    )

                    with factory() as session:
                        post_third = self._capture(
                            session,
                            number,
                            nominal_slot_hours=Decimal("24.00"),
                        )
                        request_after_third = session.scalar(
                            select(WorkforceRequest).where(
                                WorkforceRequest.legacy_demand_number == number
                            )
                        )
                        self.assertIsNotNone(request_after_third)
                        request_version_after_third = int(
                            request_after_third.aggregate_version or 1
                        )
                        planning_version_after_third = (
                            SqlPlanningMutationVersionRepository(session).current_version()
                        )
                        raw_shifts_after_third = tuple(
                            session.execute(
                                select(
                                    Shift.id,
                                    Shift.work_date,
                                    Shift.hours,
                                    Shift.resource_id,
                                )
                                .where(
                                    Shift.resource_requirement_id
                                    == eric_initial["requirement_id"]
                                )
                                .order_by(Shift.work_date, Shift.id)
                            ).all()
                        )
                        raw_sqlite_after_third = tuple(
                            Decimal(str(row[0]))
                            for row in session.connection().exec_driver_sql(
                                "SELECT hours FROM shifts "
                                "WHERE resource_requirement_id = ? "
                                "ORDER BY work_date, id",
                                (eric_initial["requirement_id"],),
                            ).all()
                        )
                        receipts = tuple(
                            session.scalars(
                                select(CommandIdempotencyReceipt).where(
                                    CommandIdempotencyReceipt.command_scope
                                    == "request_work_queue.permanent_reduction",
                                    CommandIdempotencyReceipt.idempotency_key == third_key,
                                )
                            ).all()
                        )

                    eric_post_third = post_third["slots"][
                        eric_initial["requirement_id"]
                    ]
                    eric_reductions_after_third = [
                        row
                        for row in post_third["reductions"]
                        if row["planning_intent_id"]
                        == eric_initial["planning_intent_id"]
                    ]
                    self.assertEqual(
                        eric_post_third["effective_need"],
                        Decimal("10.66"),
                    )
                    self.assertEqual(
                        eric_post_third["planned_hours"],
                        Decimal("10.66"),
                    )
                    self.assertEqual(
                        eric_post_third["covered_hours"],
                        Decimal("10.66"),
                    )
                    self.assertEqual(
                        tuple(
                            (row["work_date"], row["hours"])
                            for row in eric_post_third["shifts"]
                            if row["counted"]
                        ),
                        (
                            (START, Decimal("5.33")),
                            ("2026-11-03", Decimal("5.33")),
                        ),
                    )
                    self.assertEqual(
                        raw_sqlite_after_third,
                        (Decimal("5.33"), Decimal("5.33")),
                    )
                    self.assertEqual(len(eric_reductions_after_third), 2)
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(
                        request_version_after_third,
                        request_version_before_third + 1,
                    )
                    self.assertGreater(
                        planning_version_after_third,
                        planning_version_before_third,
                    )

                    replay = self._reduce(
                        client,
                        regenerated["allocation_id"],
                        third_preview,
                        third_key,
                    )
                    self.assertEqual(replay.status_code, 200, replay.text)
                    self.assertEqual(replay.json(), third_payload)
                    with factory() as session:
                        replay_state = self._capture(
                            session,
                            number,
                            nominal_slot_hours=Decimal("24.00"),
                        )
                        replay_rows = tuple(
                            session.execute(
                                select(
                                    Shift.id,
                                    Shift.work_date,
                                    Shift.hours,
                                    Shift.resource_id,
                                )
                                .where(
                                    Shift.resource_requirement_id
                                    == eric_initial["requirement_id"]
                                )
                                .order_by(Shift.work_date, Shift.id)
                            ).all()
                        )
                        replay_receipts = tuple(
                            session.scalars(
                                select(CommandIdempotencyReceipt).where(
                                    CommandIdempotencyReceipt.command_scope
                                    == "request_work_queue.permanent_reduction",
                                    CommandIdempotencyReceipt.idempotency_key == third_key,
                                )
                            ).all()
                        )
                    self.assertEqual(
                        replay_state["slots"][eric_initial["requirement_id"]][
                            "effective_need"
                        ],
                        Decimal("10.66"),
                    )
                    self.assertEqual(replay_rows, raw_shifts_after_third)
                    self.assertEqual(len(replay_receipts), 1)
            finally:
                engine.dispose()

    def test_duplicate_resource_name_retargets_only_disposable_bob_auto_shift(self) -> None:
        with TemporaryDirectory() as directory:
            self._exercise_duplicate_name(
                self._database(directory),
                lock_four_bob=True,
            )

    def test_duplicate_resource_name_unlocked_auto_work_stays_on_bob(self) -> None:
        with TemporaryDirectory() as directory:
            self._exercise_duplicate_name(
                self._database(directory),
                lock_four_bob=False,
            )
