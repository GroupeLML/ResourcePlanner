from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application.errors import ApplicationConflictError
from app.application.security import AuthPrincipal, ROLE_ADMIN
from app.domain.approval_envelope import EnvelopeEntryIdentity
from app.infrastructure.sql import (
    AppUser,
    CommandIdempotencyReceipt,
    Project,
    RequestApprovalReference,
    RequestApprovalRevision,
    RequestCompletionReduction,
    RequestLine,
    RequestOperationalState,
    RequestRequirementPlanningIntent,
    RequestShiftSurvivorProtection,
    Resource,
    ResourceRequirement,
    Shift,
    SqlDemandRepository,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.approval_revision_models import APPROVAL_REFERENCE_CAPTURED
from app.infrastructure.sql.base import utc_now
from app.infrastructure.sql.planning_version import SqlPlanningMutationVersionRepository
from app.server import create_api_app
from app.server.security import static_auth_resolver


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")
DAY = date(2026, 11, 19)


@unittest.skipUnless(
    IS_MSSQL,
    "Validation #877 réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class Issue877SqlServerTests(unittest.TestCase):
    def _seed(self) -> dict[str, str]:
        marker = uuid4().hex[:10]
        ids = {
            "marker": marker,
            "project": f"P877-{marker}",
            "resource": f"R877-{marker}",
            "actor": f"U877-{marker}",
            "request": f"D877-{marker}",
            "number": f"DMO-877-{marker}",
            "line": f"L877-{marker}",
            "revision": f"REV877-{marker}",
            "requirement": f"REQ877-{marker}",
            "segment": f"SEG877-{marker}",
            "intent": f"INT877-{marker}",
            "shift": f"S877-{marker}",
            "reduction": f"RED877-{marker}",
            "protection": f"SP877-{marker}",
        }
        entry_key = EnvelopeEntryIdentity(line_id=ids["line"]).stable_key
        ids["entry_key"] = entry_key
        payload = json.dumps(
            {
                "format_version": 1,
                "request": {
                    "request_id": ids["request"],
                    "request_version": 1,
                    "project_id": ids["project"],
                    "priority": None,
                    "site_client": None,
                    "location": None,
                    "line_mode": True,
                },
                "authorization": {
                    "format_version": 1,
                    "authorization_fingerprint": marker.ljust(64, "8"),
                    "entries": [
                        {
                            "identity": entry_key,
                            "project_id": ids["project"],
                            "site_id": None,
                            "location": None,
                            "slot_count": 1,
                            "line_kind": "WORKFORCE",
                            "required_resource_class": None,
                            "competency_ids": [],
                            "task_ref": None,
                            "work_package_ref": None,
                            "start_date": DAY.isoformat(),
                            "end_date": DAY.isoformat(),
                            "hours": "8.00",
                        }
                    ],
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        Project(
                            id=ids["project"],
                            number=f"P-877-{marker}",
                            name=f"Projet 877 {marker}",
                        ),
                        Resource(
                            id=ids["resource"],
                            name=f"Ressource 877 {marker}",
                            active=True,
                            erp_active=True,
                        ),
                        AppUser(
                            id=ids["actor"],
                            issuer="urn:resourceplanner:test",
                            subject=ids["actor"],
                            display_name=f"Admin 877 {marker}",
                            roles_json=json.dumps([ROLE_ADMIN]),
                            active=True,
                        ),
                    ]
                )
                session.flush()
                session.add(
                    WorkforceRequest(
                        id=ids["request"],
                        legacy_demand_number=ids["number"],
                        project_id=ids["project"],
                        requester_user_id=ids["actor"],
                        requester_name=f"Admin 877 {marker}",
                        desired_start=DAY,
                        desired_end=DAY,
                        resource_count=1,
                        estimated_hours=Decimal("8.00"),
                        status="En planification",
                        aggregate_version=1,
                        line_mode=True,
                    )
                )
                session.flush()
                session.add(
                    RequestLine(
                        id=ids["line"],
                        workforce_request_id=ids["request"],
                        position=0,
                        kind="WORKFORCE",
                        slot_count=1,
                        desired_start=DAY,
                        desired_end=DAY,
                        estimated_hours=Decimal("8.00"),
                        active=True,
                    )
                )
                session.add(
                    RequestApprovalRevision(
                        id=ids["revision"],
                        workforce_request_id=ids["request"],
                        request_version=1,
                        approved_by_name=f"Admin 877 {marker}",
                        approved_at=utc_now(),
                        provenance="APPROVAL",
                        payload_format_version=1,
                        payload_text=payload,
                        authorization_fingerprint=marker.ljust(64, "8"),
                    )
                )
                session.flush()
                session.add(
                    RequestApprovalReference(
                        workforce_request_id=ids["request"],
                        active_revision_id=ids["revision"],
                        status=APPROVAL_REFERENCE_CAPTURED,
                    )
                )
                session.add(
                    RequestOperationalState(
                        workforce_request_id=ids["request"],
                        approval_revision_id=ids["revision"],
                        version=1,
                        selections_text="{}",
                        confirmations_text="{}",
                        budget_overrides_text="{}",
                    )
                )
                session.add(
                    ResourceRequirement(
                        id=ids["requirement"],
                        legacy_segment_id=ids["segment"],
                        project_id=ids["project"],
                        workforce_request_id=ids["request"],
                        source_request_line_id=ids["line"],
                        approval_revision_id=ids["revision"],
                        approved_entry_key=entry_key,
                        approval_reference_status=APPROVAL_REFERENCE_CAPTURED,
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("8.00"),
                        status="Planifié",
                        planning_type="Flexible",
                        confirmation="Confirmée",
                        origin="REQUEST",
                        assigned_resource_id=ids["resource"],
                    )
                )
                session.flush()
                session.add(
                    RequestRequirementPlanningIntent(
                        id=ids["intent"],
                        slot_identity_key=json.dumps(
                            [ids["request"], ids["line"], entry_key, "", 0],
                            separators=(",", ":"),
                        ),
                        workforce_request_id=ids["request"],
                        source_request_line_id=ids["line"],
                        approval_revision_id=ids["revision"],
                        approved_entry_key=entry_key,
                        slot_index=0,
                        current_resource_requirement_id=ids["requirement"],
                        intent_mode="AUTO",
                        target_mode="EXPLICIT_RESOURCE",
                        target_resource_id=ids["resource"],
                    )
                )
                session.add(
                    Shift(
                        id=ids["shift"],
                        resource_requirement_id=ids["requirement"],
                        resource_id=ids["resource"],
                        work_date=DAY,
                        hours=Decimal("8.00"),
                        source="MANUAL",
                        locked=False,
                    )
                )
                session.flush()
                session.add(
                    RequestCompletionReduction(
                        id=ids["reduction"],
                        planning_intent_id=ids["intent"],
                        workforce_request_id=ids["request"],
                        historical_approval_revision_id=ids["revision"],
                        source_request_line_id=ids["line"],
                        approved_entry_key=entry_key,
                        original_shift_id=f"REMOVED-{marker}",
                        shift_snapshot_text="{}",
                        reduction_hours=Decimal("8.00"),
                        effective_need_before=Decimal("16.00"),
                        effective_need_after=Decimal("8.00"),
                        actor_user_id=ids["actor"],
                        command_id=f"REDUCE-{marker}",
                        reconciliation_state="ABSORBED",
                    )
                )
                session.flush()
                session.add(
                    RequestShiftSurvivorProtection(
                        id=ids["protection"],
                        planning_intent_id=ids["intent"],
                        shift_id=ids["shift"],
                        original_shift_id=ids["shift"],
                        originating_reduction_id=ids["reduction"],
                        active=True,
                    )
                )
            return ids
        finally:
            engine.dispose()

    @staticmethod
    def _auth(actor_id: str):
        return static_auth_resolver(
            AuthPrincipal.from_roles(
                local_user_id=actor_id,
                issuer="urn:resourceplanner:test",
                subject=actor_id,
                display_name=actor_id,
                email=None,
                roles=(ROLE_ADMIN,),
                auth_mode="test",
            )
        )

    @staticmethod
    def _planning_version(factory) -> int:
        with factory() as session:
            return SqlPlanningMutationVersionRepository(session).current_version()

    def _request_cancellation(self, client: TestClient, ids: dict[str, str]):
        response = client.post(
            f"/api/v1/demands/{ids['number']}/request-cancellation",
            json={"reason": "Incident #877 SQL Server", "expected_version": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["cancellation_request_id"]

    def test_protected_cancellation_success_and_replay_use_real_sql_constraints(self) -> None:
        ids = self._seed()
        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            planning_version = self._planning_version(factory)
            app = create_api_app(DATABASE_URL, auth_resolver=self._auth(ids["actor"]))
            key = f"accept-877-{ids['marker']}"
            with TestClient(app, raise_server_exceptions=False) as client:
                cycle_id = self._request_cancellation(client, ids)
                body = {
                    "cancellation_request_id": cycle_id,
                    "comment": "Acceptation SQL Server",
                    "expected_version": 2,
                    "expected_planning_version": planning_version,
                }
                first = client.post(
                    f"/api/v1/demands/{ids['number']}/accept-cancellation",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                self.assertEqual(first.status_code, 200, first.text)
                with factory() as session:
                    protection = session.get(
                        RequestShiftSurvivorProtection,
                        ids["protection"],
                    )
                    assert protection is not None
                    first_retired_at = protection.retired_at
                    first_provenance = (
                        protection.original_shift_id,
                        protection.originating_reduction_id,
                        protection.planning_intent_id,
                    )
                replay = client.post(
                    f"/api/v1/demands/{ids['number']}/accept-cancellation",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
                changed = client.post(
                    f"/api/v1/demands/{ids['number']}/accept-cancellation",
                    json={**body, "comment": "Payload différent"},
                    headers={"Idempotency-Key": key},
                )

            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), first.json())
            self.assertEqual(changed.status_code, 409, changed.text)
            with factory() as session:
                request = session.get(WorkforceRequest, ids["request"])
                protection = session.get(RequestShiftSurvivorProtection, ids["protection"])
                assert request is not None
                assert protection is not None
                self.assertEqual(request.status, "Annulée")
                self.assertEqual(request.cancellation_state, "ACCEPTED")
                self.assertIsNone(session.get(Shift, ids["shift"]))
                self.assertFalse(protection.active)
                self.assertIsNone(protection.shift_id)
                self.assertIsNotNone(protection.retired_at)
                self.assertEqual(protection.retired_at, first_retired_at)
                self.assertEqual(
                    (
                        protection.original_shift_id,
                        protection.originating_reduction_id,
                        protection.planning_intent_id,
                    ),
                    first_provenance,
                )
                self.assertEqual(
                    SqlPlanningMutationVersionRepository(session).current_version(),
                    planning_version + 1,
                )
        finally:
            engine.dispose()

    def test_protected_cancellation_late_failure_rolls_back_everything(self) -> None:
        ids = self._seed()
        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            planning_version = self._planning_version(factory)
            app = create_api_app(DATABASE_URL, auth_resolver=self._auth(ids["actor"]))
            key = f"rollback-877-{ids['marker']}"
            with TestClient(app, raise_server_exceptions=False) as client:
                cycle_id = self._request_cancellation(client, ids)
                with patch.object(
                    SqlDemandRepository,
                    "_append_history",
                    side_effect=RuntimeError("877 injected late failure"),
                ):
                    failed = client.post(
                        f"/api/v1/demands/{ids['number']}/accept-cancellation",
                        json={
                            "cancellation_request_id": cycle_id,
                            "comment": "Rollback SQL Server",
                            "expected_version": 2,
                            "expected_planning_version": planning_version,
                        },
                        headers={"Idempotency-Key": key},
                    )
            self.assertEqual(failed.status_code, 500, failed.text)

            with factory() as session:
                request = session.get(WorkforceRequest, ids["request"])
                requirement = session.get(ResourceRequirement, ids["requirement"])
                protection = session.get(RequestShiftSurvivorProtection, ids["protection"])
                assert request is not None
                assert requirement is not None
                assert protection is not None
                self.assertEqual(request.status, "En planification")
                self.assertEqual(request.cancellation_state, "PENDING")
                self.assertEqual(request.aggregate_version, 2)
                self.assertEqual(requirement.status, "Planifié")
                self.assertIsNotNone(session.get(Shift, ids["shift"]))
                self.assertTrue(protection.active)
                self.assertEqual(protection.shift_id, ids["shift"])
                self.assertIsNone(protection.retired_at)
                self.assertEqual(
                    SqlPlanningMutationVersionRepository(session).current_version(),
                    planning_version,
                )
                self.assertEqual(
                    int(
                        session.scalar(
                            select(func.count())
                            .select_from(WorkforceRequestHistory)
                            .where(
                                WorkforceRequestHistory.workforce_request_id == ids["request"],
                                WorkforceRequestHistory.action == "Acceptation d'annulation",
                            )
                        )
                        or 0
                    ),
                    0,
                )
                self.assertEqual(
                    int(
                        session.scalar(
                            select(func.count())
                            .select_from(CommandIdempotencyReceipt)
                            .where(
                                CommandIdempotencyReceipt.command_scope
                                == "demand_cancellation.accept",
                                CommandIdempotencyReceipt.idempotency_key == key,
                            )
                        )
                        or 0
                    ),
                    0,
                )
        finally:
            engine.dispose()

    def test_cancellation_vs_resync_serializes_without_resurrection_or_invalid_terminal_work(self) -> None:
        ids = self._seed()
        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            app = create_api_app(DATABASE_URL, auth_resolver=self._auth(ids["actor"]))
            with TestClient(app, raise_server_exceptions=False) as client:
                cycle_id = self._request_cancellation(client, ids)
            planning_version = self._planning_version(factory)

            def accept() -> tuple[str, str | None]:
                with TestClient(app, raise_server_exceptions=False) as client:
                    response = client.post(
                        f"/api/v1/demands/{ids['number']}/accept-cancellation",
                        json={
                            "cancellation_request_id": cycle_id,
                            "comment": "Race cancellation",
                            "expected_version": 2,
                            "expected_planning_version": planning_version,
                        },
                        headers={"Idempotency-Key": f"race-877-{ids['marker']}"},
                    )
                if response.status_code == 200:
                    return "success", None
                return "conflict", response.json().get("error", {}).get("code")

            def resync() -> tuple[str, str | None]:
                try:
                    with factory.begin() as session:
                        SqlPeriodAwareApprovedDemandSyncAdapter(
                            session
                        ).sync_operational_choices(ids["number"])
                except ApplicationConflictError as exc:
                    return "conflict", exc.code
                return "success", None

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = (
                    executor.submit(accept).result(timeout=30),
                    executor.submit(resync).result(timeout=30),
                )

            self.assertEqual(sorted(row[0] for row in outcomes), ["conflict", "success"])
            with factory() as session:
                request = session.get(WorkforceRequest, ids["request"])
                requirement = session.get(ResourceRequirement, ids["requirement"])
                protection = session.get(RequestShiftSurvivorProtection, ids["protection"])
                assert request is not None
                assert requirement is not None
                assert protection is not None
                shift = session.get(Shift, ids["shift"])
                if request.cancellation_state == "ACCEPTED":
                    self.assertEqual(request.status, "Annulée")
                    self.assertEqual(requirement.status, "Annulé")
                    self.assertIsNone(shift)
                    self.assertFalse(protection.active)
                    self.assertIsNone(protection.shift_id)
                else:
                    self.assertEqual(request.cancellation_state, "PENDING")
                    self.assertNotEqual(requirement.status, "Annulé")
                    self.assertIsNotNone(shift)
                    self.assertTrue(protection.active)
                    self.assertEqual(protection.shift_id, ids["shift"])
                self.assertFalse(
                    requirement.status == "Annulé" and shift is not None,
                    "Committed Annulé + live Shift is forbidden for REQUEST requirements.",
                )
                self.assertEqual(
                    SqlPlanningMutationVersionRepository(session).current_version(),
                    planning_version + 1,
                )
        finally:
            engine.dispose()
