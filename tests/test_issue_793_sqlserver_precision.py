from __future__ import annotations

from datetime import date, time
from decimal import Decimal
import os
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.infrastructure.sql import (
    Project,
    RequestCompletionReduction,
    RequestRequirementPlanningIntent,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
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
    def _slot_state(factory, number: str, resource_id: str) -> dict:
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
                nominal_slot_hours=Decimal("24.00"),
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
                "stored_hours": stored_hours,
            }

    def test_real_sqlserver_sequential_and_fractional_reduction_use_exact_cents(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P793-{marker}"
        project_number = f"P-793-{marker}"
        eric_id = f"R793-E-{marker}"
        timmy_id = f"R793-T-{marker}"
        third_key = f"793-third-{marker}"
        failed_key = f"793-stale-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add(
                    Project(
                        id=project_id,
                        number=project_number,
                        name=f"Projet précision #793 {marker}",
                    )
                )
                session.add_all(
                    [
                        Resource(
                            id=eric_id,
                            external_id=f"EMP-{eric_id}",
                            name=f"ERIC FONTAINE {marker}",
                            resource_class="ELECTRICAL",
                            active=True,
                            erp_active=True,
                            erp_status="Active",
                        ),
                        Resource(
                            id=timmy_id,
                            external_id=f"EMP-{timmy_id}",
                            name=f"TIMMY CURIE {marker}",
                            resource_class="ELECTRICAL",
                            active=True,
                            erp_active=True,
                            erp_status="Active",
                        ),
                    ]
                )
                session.flush()
                for index, resource_id in enumerate((eric_id, timmy_id), start=1):
                    session.add(
                        ResourceAvailabilityRule(
                            id=f"AV793-{marker}-{index}",
                            resource_id=resource_id,
                            availability_type="Horaire standard",
                            weekdays="Lun,Mar,Mer",
                            start_time=time(8, 0),
                            end_time=time(16, 0),
                            active=True,
                        )
                    )
                session.flush()
                seed_test_approval_routing(session)

            app = create_api_app(
                DATABASE_URL,
                actor_name=f"admin-793-sql-{marker}",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload(
                        {
                            "project_number": project_number,
                            "submit": True,
                            "lines": [
                                {
                                    "position": 0,
                                    "desired_start": MONDAY.isoformat(),
                                    "desired_end": WEDNESDAY.isoformat(),
                                    "desired_active_days": 3,
                                    "estimated_hours": 24,
                                    "required_resource_class": "ELECTRICAL",
                                    "confirmation": "Confirmée",
                                    "description": "#793 SQL precision A",
                                },
                                {
                                    "position": 1,
                                    "desired_start": MONDAY.isoformat(),
                                    "desired_end": WEDNESDAY.isoformat(),
                                    "desired_active_days": 3,
                                    "estimated_hours": 24,
                                    "required_resource_class": "ELECTRICAL",
                                    "confirmation": "Confirmée",
                                    "description": "#793 SQL precision B",
                                },
                            ],
                        }
                    ),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "#793 SQL precision approval"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 2)
                self._place_from_drop(
                    client,
                    segment_id=actions[0]["segment_id"],
                    resource_id=eric_id,
                    key=f"793-drop-eric-{marker}",
                )
                actions = self._assignment_actions(client, number)
                self.assertEqual(len(actions), 1)
                self._place_from_drop(
                    client,
                    segment_id=actions[0]["segment_id"],
                    resource_id=timmy_id,
                    key=f"793-drop-timmy-{marker}",
                )

                eric_initial = self._slot_state(factory, number, eric_id)
                timmy_initial = self._slot_state(factory, number, timmy_id)
                expected_24 = (
                    (MONDAY, Decimal("8.00")),
                    (TUESDAY, Decimal("8.00")),
                    (WEDNESDAY, Decimal("8.00")),
                )
                for state in (eric_initial, timmy_initial):
                    self.assertEqual(state["effective_need"], Decimal("24.00"))
                    self.assertEqual(
                        tuple((row[2], row[3]) for row in state["shifts"]),
                        expected_24,
                    )

                eric_wed = next(
                    row for row in eric_initial["shifts"] if row[2] == WEDNESDAY
                )
                preview = self._preview(client, eric_wed[1])
                reduced = self._reduce(
                    client,
                    allocation_id=eric_wed[1],
                    preview=preview,
                    key=f"793-reduce-eric-{marker}",
                )
                self.assertEqual(reduced.status_code, 200, reduced.text)
                eric_after_first = self._slot_state(factory, number, eric_id)
                self.assertEqual(eric_after_first["effective_need"], Decimal("16.00"))
                self.assertEqual(
                    tuple((row[2], row[3]) for row in eric_after_first["shifts"]),
                    (
                        (MONDAY, Decimal("8.00")),
                        (TUESDAY, Decimal("8.00")),
                    ),
                )
                first_survivor_ids = {row[0] for row in eric_after_first["shifts"]}

                timmy_before_second = self._slot_state(factory, number, timmy_id)
                timmy_wed = next(
                    row for row in timmy_before_second["shifts"] if row[2] == WEDNESDAY
                )
                preview = self._preview(client, timmy_wed[1])
                reduced = self._reduce(
                    client,
                    allocation_id=timmy_wed[1],
                    preview=preview,
                    key=f"793-reduce-timmy-{marker}",
                )
                self.assertEqual(reduced.status_code, 200, reduced.text)

                eric_after_second = self._slot_state(factory, number, eric_id)
                timmy_after_second = self._slot_state(factory, number, timmy_id)
                self.assertEqual(eric_after_second["effective_need"], Decimal("16.00"))
                self.assertEqual(timmy_after_second["effective_need"], Decimal("16.00"))
                self.assertTrue(
                    first_survivor_ids.isdisjoint(
                        {row[0] for row in eric_after_second["shifts"]}
                    )
                )
                self.assertEqual(
                    tuple((row[2], row[3]) for row in eric_after_second["shifts"]),
                    (
                        (MONDAY, Decimal("5.33")),
                        (TUESDAY, Decimal("5.33")),
                        (WEDNESDAY, Decimal("5.34")),
                    ),
                )
                self.assertEqual(
                    sum((row[3] for row in eric_after_second["shifts"]), Decimal("0.00")),
                    Decimal("16.00"),
                )
                self.assertEqual(
                    eric_after_second["stored_hours"],
                    (
                        Decimal("5.33"),
                        Decimal("5.33"),
                        Decimal("5.34"),
                    ),
                )
                self.assertTrue(
                    all(row[3] >= Decimal("0.01") for row in eric_after_second["shifts"])
                )

                regenerated = next(
                    row for row in eric_after_second["shifts"] if row[2] == WEDNESDAY
                )
                third_preview = self._preview(client, regenerated[1])
                self.assertEqual(
                    Decimal(str(third_preview["effective_need"])).quantize(Decimal("0.01")),
                    Decimal("16.00"),
                )
                self.assertEqual(
                    Decimal(str(third_preview["covered_hours"])).quantize(Decimal("0.01")),
                    Decimal("16.00"),
                )
                self.assertEqual(
                    Decimal(str(third_preview["effective_need_after_reduction"])).quantize(
                        Decimal("0.01")
                    ),
                    Decimal("10.66"),
                )
                self.assertEqual(
                    Decimal(str(third_preview["remaining_after_reduction"])).quantize(
                        Decimal("0.01")
                    ),
                    Decimal("0.00"),
                )

                third = self._reduce(
                    client,
                    allocation_id=regenerated[1],
                    preview=third_preview,
                    key=third_key,
                )
                self.assertEqual(third.status_code, 200, third.text)
                third_payload = third.json()
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

                eric_after_third = self._slot_state(factory, number, eric_id)
                self.assertEqual(eric_after_third["effective_need"], Decimal("10.66"))
                self.assertEqual(eric_after_third["planned_hours"], Decimal("10.66"))
                self.assertEqual(
                    tuple((row[2], row[3]) for row in eric_after_third["shifts"]),
                    (
                        (MONDAY, Decimal("5.33")),
                        (TUESDAY, Decimal("5.33")),
                    ),
                )
                self.assertEqual(len(eric_after_third["reductions"]), 2)
                self.assertEqual(
                    sum((row[3] for row in eric_after_third["shifts"]), Decimal("0.00")),
                    Decimal("10.66"),
                )
                self.assertEqual(
                    eric_after_third["stored_hours"],
                    (Decimal("5.33"), Decimal("5.33")),
                )

                replay = self._reduce(
                    client,
                    allocation_id=regenerated[1],
                    preview=third_preview,
                    key=third_key,
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(replay.json(), third_payload)
                replay_state = self._slot_state(factory, number, eric_id)
                self.assertEqual(replay_state["shifts"], eric_after_third["shifts"])
                self.assertEqual(len(replay_state["reductions"]), 2)

                with factory() as session:
                    receipts = tuple(
                        session.scalars(
                            select(CommandIdempotencyReceipt).where(
                                CommandIdempotencyReceipt.command_scope
                                == "request_work_queue.permanent_reduction",
                                CommandIdempotencyReceipt.idempotency_key == third_key,
                            )
                        ).all()
                    )
                self.assertEqual(len(receipts), 1)

                rollback_target = eric_after_third["shifts"][0]
                rollback_preview = self._preview(client, rollback_target[1])
                stale_preview = {
                    **rollback_preview,
                    "planning_version": int(rollback_preview["planning_version"]) - 1,
                }
                before_failed = self._slot_state(factory, number, eric_id)
                failed = self._reduce(
                    client,
                    allocation_id=rollback_target[1],
                    preview=stale_preview,
                    key=failed_key,
                )
                self.assertEqual(failed.status_code, 409, failed.text)
                self.assertEqual(
                    failed.json()["error"]["code"],
                    "planning_version_conflict",
                )
                after_failed = self._slot_state(factory, number, eric_id)
                self.assertEqual(after_failed["effective_need"], before_failed["effective_need"])
                self.assertEqual(after_failed["shifts"], before_failed["shifts"])
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
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
