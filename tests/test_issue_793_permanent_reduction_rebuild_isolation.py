from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.infrastructure.sql import (
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
from app.server import create_api_app
from tests import test_request_work_queue_api_integration as work_queue_api
from tests.approval_test_support import routed_demand_payload
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


START = "2026-11-02"
END = "2026-11-06"
THURSDAY = "2026-11-05"


class Issue793PermanentReductionRebuildIsolationTests(TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        work_queue_api.RequestWorkQueueApiIntegrationTests._seed_database(session)
        session.add(
            Resource(
                id="R-793-C",
                name="Charlie 793",
                active=True,
                erp_active=True,
                sort_order=3,
            )
        )
        session.flush()
        session.add(
            ResourceAvailabilityRule(
                id="SCH-R-793-C",
                resource_id="R-793-C",
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
    def _capture(session, number: str) -> dict:
        request = session.scalar(
            select(WorkforceRequest).where(
                WorkforceRequest.legacy_demand_number == number
            )
        )
        assert request is not None
        rows = session.execute(
            select(RequestRequirementPlanningIntent, ResourceRequirement)
            .join(
                ResourceRequirement,
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == ResourceRequirement.id,
            )
            .where(
                RequestRequirementPlanningIntent.workforce_request_id == request.id,
                ResourceRequirement.status != "Annulé",
            )
            .order_by(
                RequestRequirementPlanningIntent.approved_entry_key,
                RequestRequirementPlanningIntent.slot_index,
            )
        ).all()
        slots = {}
        for intent, requirement in rows:
            shift_rows = session.scalars(
                select(Shift)
                .where(Shift.resource_requirement_id == requirement.id)
                .order_by(Shift.work_date, Shift.id)
            ).all()
            covered = sum(
                (
                    Decimal(str(shift.hours))
                    for shift in shift_rows
                    if str(shift.allocation_type or "") != "MISSING"
                ),
                Decimal("0.00"),
            )
            effective = Decimal(str(requirement.planned_hours)).quantize(Decimal("0.01"))
            slots[requirement.id] = {
                "requirement_id": requirement.id,
                "segment_id": requirement.legacy_segment_id or requirement.id,
                "planned_hours": effective,
                "assigned_resource_id": requirement.assigned_resource_id,
                "planning_intent_id": intent.id,
                "approved_entry_key": intent.approved_entry_key,
                "slot_index": int(intent.slot_index),
                "source_request_line_id": intent.source_request_line_id,
                "intent_mode": intent.intent_mode,
                "target_mode": intent.target_mode,
                "target_resource_id": intent.target_resource_id,
                "covered_hours": covered,
                "residual": max(effective - covered, Decimal("0.00")),
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
                    }
                    for shift in shift_rows
                ),
            }
        reductions = tuple(
            {
                "id": row.id,
                "planning_intent_id": row.planning_intent_id,
                "resource_requirement_id": row.resource_requirement_id,
                "reduction_hours": Decimal(str(row.reduction_hours)).quantize(Decimal("0.01")),
                "effective_need_before": Decimal(str(row.effective_need_before)).quantize(Decimal("0.01")),
                "effective_need_after": Decimal(str(row.effective_need_after)).quantize(Decimal("0.01")),
                "original_shift_id": row.original_shift_id,
            }
            for row in session.scalars(
                select(RequestCompletionReduction)
                .where(RequestCompletionReduction.workforce_request_id == request.id)
                .order_by(RequestCompletionReduction.created_at, RequestCompletionReduction.id)
            ).all()
        )
        weekly = defaultdict(lambda: Decimal("0.00"))
        for values in slots.values():
            for shift in values["shifts"]:
                if str(shift["allocation_type"] or "") != "MISSING":
                    weekly[str(shift["resource_id"])] += shift["hours"]
        return {
            "request_id": request.id,
            "slots": slots,
            "reductions": reductions,
            "weekly": dict(weekly),
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
        return next(shift for shift in slot["shifts"] if shift["work_date"] == THURSDAY)

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
                self._assign(client, actions[0]["segment_id"], "R-778-A")
                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 1)
                self._assign(client, actions[0]["segment_id"], "R-778-B")

                with factory() as session:
                    before = self._capture(session, number)
                a_before = self._by_target(before, "R-778-A")
                b_before = self._by_target(before, "R-778-B")
                self.assertEqual(a_before["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_before["planned_hours"], Decimal("40.00"))
                self.assertEqual(a_before["target_mode"], "EXPLICIT_RESOURCE")
                self.assertEqual(a_before["target_resource_id"], "R-778-A")
                self.assertEqual(b_before["target_mode"], "EXPLICIT_RESOURCE")
                self.assertEqual(b_before["target_resource_id"], "R-778-B")
                self.assertEqual(len(a_before["shifts"]), 5)
                self.assertEqual(len(b_before["shifts"]), 5)
                self.assertEqual({row["resource_id"] for row in a_before["shifts"]}, {"R-778-A"})
                self.assertEqual({row["resource_id"] for row in b_before["shifts"]}, {"R-778-B"})

                target = self._target_shift(a_before)
                preview = self._preview(client, target["allocation_id"])
                original_rebuild = SqlPlanningCommandAdapter.rebuild

                def traced_rebuild(adapter, *, preserve_shift_ids=(), temporary_excluded_dates_by_segment=None):
                    preserve_calls.append(tuple(str(value) for value in preserve_shift_ids))
                    boundary_before_rebuild.append(self._capture(adapter._session, number))
                    return original_rebuild(
                        adapter,
                        preserve_shift_ids=preserve_shift_ids,
                        temporary_excluded_dates_by_segment=temporary_excluded_dates_by_segment,
                    )

                with patch.object(SqlPlanningCommandAdapter, "rebuild", new=traced_rebuild):
                    reduced = self._reduce(
                        client,
                        target["allocation_id"],
                        preview,
                        "issue-793-explicit-two-lines" if two_lines else "issue-793-explicit-two-slot",
                    )
                self.assertEqual(reduced.status_code, 200, reduced.text)

                self.assertEqual(len(boundary_before_rebuild), 1)
                pre_rebuild = boundary_before_rebuild[0]
                a_pre = pre_rebuild["slots"][a_before["requirement_id"]]
                b_pre = pre_rebuild["slots"][b_before["requirement_id"]]
                self.assertEqual(a_pre["planned_hours"], Decimal("32.00"))
                self.assertEqual(b_pre["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_pre["assigned_resource_id"], "R-778-B")
                self.assertEqual(b_pre["target_resource_id"], "R-778-B")
                self.assertEqual(
                    tuple(row["resource_id"] for row in b_pre["shifts"]),
                    tuple(row["resource_id"] for row in b_before["shifts"]),
                )

                with factory() as session:
                    after = self._capture(session, number)
                a_after = after["slots"][a_before["requirement_id"]]
                b_after = after["slots"][b_before["requirement_id"]]
                self.assertEqual(a_after["planned_hours"], Decimal("32.00"))
                self.assertEqual(b_after["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_after["assigned_resource_id"], "R-778-B")
                self.assertEqual(b_after["target_mode"], "EXPLICIT_RESOURCE")
                self.assertEqual(b_after["target_resource_id"], "R-778-B")
                self.assertEqual({row["resource_id"] for row in b_after["shifts"]}, {"R-778-B"})
                self.assertEqual(sum((row["hours"] for row in b_after["shifts"]), Decimal("0.00")), Decimal("40.00"))
                self.assertEqual(b_after["residual"], Decimal("0.00"))
                self.assertEqual(after["weekly"].get("R-793-C", Decimal("0.00")), Decimal("0.00"))

                reduction = after["reductions"]
                self.assertEqual(len(reduction), 1)
                self.assertEqual(reduction[0]["planning_intent_id"], a_before["planning_intent_id"])
                self.assertEqual(reduction[0]["resource_requirement_id"], a_before["requirement_id"])
                self.assertEqual(reduction[0]["reduction_hours"], Decimal("8.00"))

                expected_preserved = {
                    row["id"]
                    for row in a_before["shifts"]
                    if row["id"] != target["id"]
                }
                self.assertEqual(len(preserve_calls), 1)
                self.assertEqual(set(preserve_calls[0]), expected_preserved)
                self.assertTrue(
                    {row["id"] for row in b_before["shifts"]}.isdisjoint(preserve_calls[0])
                )

                with factory.begin() as session:
                    SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_operational_choices(number)
                with factory() as session:
                    after_resync = self._capture(session, number)
                b_resync = after_resync["slots"][b_before["requirement_id"]]
                self.assertEqual(b_resync["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_resync["assigned_resource_id"], "R-778-B")
                self.assertEqual(b_resync["target_resource_id"], "R-778-B")
                self.assertEqual({row["resource_id"] for row in b_resync["shifts"]}, {"R-778-B"})

                # New session = reload boundary.
                with factory() as session:
                    reloaded = self._capture(session, number)
                b_reload = reloaded["slots"][b_before["requirement_id"]]
                self.assertEqual(b_reload["planned_hours"], Decimal("40.00"))
                self.assertEqual(b_reload["assigned_resource_id"], "R-778-B")
                self.assertEqual(b_reload["target_resource_id"], "R-778-B")
                self.assertEqual(b_reload["residual"], Decimal("0.00"))

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
        entries = {
            slot["approved_entry_key"]
            for slot in result["before"]["slots"].values()
        }
        self.assertEqual(len(entries), 2)

    def test_targetless_two_slot_request_does_not_fabricate_automatic_resource_choice(self) -> None:
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
                    self.assertEqual(len(before["slots"]), 2)
                    for slot in before["slots"].values():
                        self.assertIsNone(slot["assigned_resource_id"])
                        self.assertEqual(slot["target_mode"], "EXPLICIT_NONE")
                        self.assertIsNone(slot["target_resource_id"])
                        self.assertEqual(slot["shifts"], ())

                    rebuilt = client.post("/api/v1/planning/rebuild")
                    self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                    with factory() as session:
                        after = self._capture(session, number)
                    for slot in after["slots"].values():
                        self.assertIsNone(slot["assigned_resource_id"])
                        self.assertEqual(slot["target_mode"], "EXPLICIT_NONE")
                        self.assertIsNone(slot["target_resource_id"])
                        self.assertEqual(slot["shifts"], ())
            finally:
                engine.dispose()
