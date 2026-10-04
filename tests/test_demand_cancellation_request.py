from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application.errors import ApplicationConflictError
from app.application.security import AuthPrincipal, ROLE_ADMIN
from app.infrastructure.sql import (
    AppUser,
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    Base,
    BusinessContact,
    CommandIdempotencyReceipt,
    PlanningMutationState,
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    SqlDemandRepository,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    SqlPlannerQueryRepository,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


DAY = date(2026, 9, 23)

TEST_CANCELLATION_ADMIN_AUTH_RESOLVER = static_auth_resolver(
    AuthPrincipal.from_roles(
        local_user_id="U-ADMIN",
        issuer="urn:resourceplanner:test",
        subject="cancellation-admin",
        display_name="Coordonnateur annulation",
        email=None,
        roles=(ROLE_ADMIN,),
        auth_mode="test",
    )
)


class DemandCancellationRequestTests(unittest.TestCase):
    @staticmethod
    def _database(directory: str) -> str:
        path = Path(directory) / "cancellation-request.db"
        url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    AppUser(
                        id="U-ADMIN",
                        issuer="urn:resourceplanner:test",
                        subject="cancellation-admin",
                        display_name="Coordonnateur annulation",
                        roles_json='["ADMIN"]',
                        active=True,
                    ),
                    Project(id="P1", number="P-1", name="Projet annulation"),
                    BusinessContact(
                        id="C-CANCEL-RESP",
                        display_name="Responsable quart annulé",
                        active=True,
                    ),
                    Resource(id="R1", name="Alice", active=True),
                    AssetType(
                        id="AT1",
                        code="VEH",
                        label="Véhicule",
                        category="VEHICLE",
                    ),
                ]
            )
            session.flush()
            session.add(Asset(id="A1", code="TRUCK-1", label="Camion 1", asset_type_id="AT1"))
            session.add_all(
                [
                    WorkforceRequest(
                        id="D-HUMAN",
                        legacy_demand_number="DMO-CANCEL-HUMAN",
                        project_id="P1",
                        status="En planification",
                        aggregate_version=4,
                        line_mode=True,
                    ),
                    WorkforceRequest(
                        id="D-ASSET",
                        legacy_demand_number="DMO-CANCEL-ASSET",
                        project_id="P1",
                        status="En planification",
                        aggregate_version=2,
                        line_mode=True,
                    ),
                    WorkforceRequest(
                        id="D-EMPTY",
                        legacy_demand_number="DMO-CANCEL-EMPTY",
                        project_id="P1",
                        status="En planification",
                        aggregate_version=3,
                        line_mode=True,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    RequestLine(
                        id="L-HUMAN",
                        workforce_request_id="D-HUMAN",
                        position=0,
                        kind="WORKFORCE",
                        desired_start=DAY,
                        desired_end=DAY,
                        estimated_hours=8,
                    ),
                    RequestLine(
                        id="L-ASSET",
                        workforce_request_id="D-ASSET",
                        position=0,
                        kind="ASSET",
                        desired_start=DAY,
                        desired_end=DAY,
                        estimated_hours=8,
                        asset_type_id="AT1",
                    ),
                    RequestLine(
                        id="L-EMPTY",
                        workforce_request_id="D-EMPTY",
                        position=0,
                        kind="WORKFORCE",
                        desired_start=DAY,
                        desired_end=DAY,
                        estimated_hours=8,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ResourceRequirement(
                        id="REQ-HUMAN",
                        legacy_segment_id="SEG-HUMAN",
                        project_id="P1",
                        workforce_request_id="D-HUMAN",
                        source_request_line_id="L-HUMAN",
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=8,
                        status="Planifié",
                        origin="REQUEST",
                    ),
                    ResourceRequirement(
                        id="REQ-EMPTY",
                        legacy_segment_id="SEG-EMPTY",
                        project_id="P1",
                        workforce_request_id="D-EMPTY",
                        source_request_line_id="L-EMPTY",
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=8,
                        status="Planifié",
                        origin="REQUEST",
                    ),
                    AssetRequirement(
                        id="AREQ-1",
                        project_id="P1",
                        workforce_request_id="D-ASSET",
                        source_request_line_id="L-ASSET",
                        approved_entry_key="LINE:L-ASSET",
                        slot_index=0,
                        asset_type_id="AT1",
                        start_date=DAY,
                        end_date=DAY,
                        usage_hours=8,
                        status="Planifié",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    Shift(
                        id="SHIFT-HUMAN",
                        resource_requirement_id="REQ-HUMAN",
                        resource_id="R1",
                        work_date=DAY,
                        hours=8,
                        source="MANUAL",
                        locked=True,
                        operational_responsible_override_contact_id="C-CANCEL-RESP",
                    ),
                    AssetAllocation(
                        id="ALLOC-ASSET",
                        asset_requirement_id="AREQ-1",
                        asset_id="A1",
                        start_date=DAY,
                        end_date=DAY,
                        locked=True,
                        source="MANUAL",
                    ),
                    PlanningMutationState(id="GLOBAL", version=9),
                ]
            )
        engine.dispose()
        return url

    @staticmethod
    def _row(database_url: str, number: str) -> tuple[WorkforceRequest, int, int]:
        engine = create_sql_engine(database_url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                request = session.scalar(
                    select(WorkforceRequest).where(
                        WorkforceRequest.legacy_demand_number == number
                    )
                )
                assert request is not None
                planning_version = session.scalar(
                    select(PlanningMutationState.version).where(
                        PlanningMutationState.id == "GLOBAL"
                    )
                )
                shift_count = session.scalar(select(func.count()).select_from(Shift))
                session.expunge(request)
                return request, int(planning_version or 0), int(shift_count or 0)
        finally:
            engine.dispose()

    def test_real_materialization_distinguishes_human_asset_and_requirement_only(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    rows = SqlPlannerQueryRepository(
                        session
                    ).list_demand_cancellation_materializations(
                        (
                            "DMO-CANCEL-HUMAN",
                            "DMO-CANCEL-ASSET",
                            "DMO-CANCEL-EMPTY",
                        )
                    )
                    by_number = {row.demand_number: row for row in rows}

                    human = by_number["DMO-CANCEL-HUMAN"]
                    self.assertEqual(human.human_shift_count, 1)
                    self.assertEqual(human.locked_human_shift_count, 1)
                    self.assertTrue(human.has_operational_decisions)

                    asset = by_number["DMO-CANCEL-ASSET"]
                    self.assertEqual(asset.asset_allocation_count, 1)
                    self.assertEqual(asset.locked_asset_allocation_count, 1)
                    self.assertTrue(asset.has_operational_decisions)

                    empty = by_number["DMO-CANCEL-EMPTY"]
                    self.assertEqual(empty.human_shift_count, 0)
                    self.assertEqual(empty.asset_allocation_count, 0)
                    self.assertFalse(empty.has_operational_decisions)
            finally:
                engine.dispose()

    def test_detail_workflow_and_list_share_the_same_materialized_policy(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                listed = client.get("/api/v1/demands")
                workflow = client.get(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/workflow-actions"
                )
                detail = client.get(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/detail"
                )

            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(workflow.status_code, 200, workflow.text)
            self.assertEqual(detail.status_code, 200, detail.text)

            listed_human = next(
                row
                for row in listed.json()
                if row["number"] == "DMO-CANCEL-HUMAN"
            )
            listed_empty = next(
                row
                for row in listed.json()
                if row["number"] == "DMO-CANCEL-EMPTY"
            )
            workflow_policy = workflow.json()["cancellation"]
            detail_body = detail.json()

            self.assertEqual(
                listed_human["cancellation_policy"],
                workflow_policy,
            )
            self.assertEqual(
                detail_body["demand"]["cancellation_policy"],
                workflow_policy,
            )
            self.assertEqual(
                detail_body["workflow"]["cancellation"],
                workflow_policy,
            )
            self.assertTrue(workflow_policy["has_operational_decisions"])
            self.assertFalse(workflow_policy["direct_cancel"])
            self.assertTrue(workflow_policy["request_cancellation"])
            self.assertIn(
                "request-cancellation",
                workflow.json()["available_actions"],
            )
            self.assertNotIn("cancel", workflow.json()["available_actions"])
            self.assertTrue(listed_empty["cancellation_policy"]["direct_cancel"])
            self.assertFalse(
                listed_empty["cancellation_policy"]["request_cancellation"]
            )

    def test_request_reject_and_successive_cycle_keep_plan_untouched(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )

            before, planning_before, shifts_before = self._row(
                database_url,
                "DMO-CANCEL-HUMAN",
            )
            self.assertEqual(before.aggregate_version, 4)
            self.assertEqual(planning_before, 9)

            with TestClient(app, raise_server_exceptions=False) as client:
                direct = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/cancel",
                    json={"expected_version": 4},
                )
                self.assertEqual(direct.status_code, 409, direct.text)
                self.assertEqual(
                    direct.json()["error"]["code"],
                    "active_operational_decisions",
                )

                missing_reason = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={"reason": "   ", "expected_version": 4},
                )
                self.assertEqual(missing_reason.status_code, 422, missing_reason.text)
                self.assertEqual(
                    missing_reason.json()["error"]["code"],
                    "cancellation_reason_required",
                )

                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={
                        "reason": "Le mandat est abandonné",
                        "expected_version": 4,
                    },
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                first_cycle = requested.json()["cancellation_request_id"]
                UUID(first_cycle)
                self.assertEqual(requested.json()["status"], "En planification")
                self.assertEqual(requested.json()["cancellation_state"], "PENDING")

                stale = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={
                        "reason": "Tentative obsolète",
                        "expected_version": 4,
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["error"]["code"],
                    "demand_version_conflict",
                )

                rejected = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/reject-cancellation",
                    json={
                        "cancellation_request_id": first_cycle,
                        "comment": "Le plan doit être conservé",
                        "expected_version": 5,
                    },
                )
                self.assertEqual(rejected.status_code, 200, rejected.text)
                self.assertEqual(rejected.json()["cancellation_state"], "REJECTED")

                requested_again = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={
                        "reason": "Nouvelle demande après discussion",
                        "expected_version": 6,
                    },
                )
                self.assertEqual(requested_again.status_code, 200, requested_again.text)
                second_cycle = requested_again.json()["cancellation_request_id"]
                UUID(second_cycle)
                self.assertNotEqual(first_cycle, second_cycle)

            after, planning_after, shifts_after = self._row(
                database_url,
                "DMO-CANCEL-HUMAN",
            )
            self.assertEqual(after.status, "En planification")
            self.assertEqual(after.aggregate_version, 7)
            self.assertEqual(after.cancellation_state, "PENDING")
            self.assertEqual(after.cancellation_request_id, second_cycle)
            self.assertEqual(after.cancellation_requested_by_user_id, "U-ADMIN")
            self.assertEqual(
                after.cancellation_reason,
                "Nouvelle demande après discussion",
            )
            self.assertIsNone(after.cancellation_resolved_at)
            self.assertIsNone(after.cancellation_resolution_comment)
            self.assertEqual(planning_after, planning_before)
            self.assertEqual(shifts_after, shifts_before)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    history = tuple(
                        session.scalars(
                            select(WorkforceRequestHistory)
                            .where(
                                WorkforceRequestHistory.workforce_request_id
                                == "D-HUMAN"
                            )
                            .order_by(
                                WorkforceRequestHistory.occurred_at,
                                WorkforceRequestHistory.id,
                            )
                        ).all()
                    )
                    cancellation_events = tuple(
                        row
                        for row in history
                        if row.action
                        in {"Demande d'annulation", "Refus d'annulation"}
                    )
                    self.assertEqual(
                        [row.action for row in cancellation_events],
                        [
                            "Demande d'annulation",
                            "Refus d'annulation",
                            "Demande d'annulation",
                        ],
                    )
                    first_details = json.loads(cancellation_events[0].details or "{}")
                    reject_details = json.loads(cancellation_events[1].details or "{}")
                    second_details = json.loads(cancellation_events[2].details or "{}")
                    self.assertEqual(
                        first_details["cancellation_request_id"],
                        first_cycle,
                    )
                    self.assertEqual(
                        reject_details["cancellation_request_id"],
                        first_cycle,
                    )
                    self.assertEqual(reject_details["resolution"], "REJECTED")
                    self.assertEqual(
                        second_details["cancellation_request_id"],
                        second_cycle,
                    )
                    self.assertEqual(
                        second_details["reason"],
                        "Nouvelle demande après discussion",
                    )
            finally:
                engine.dispose()

    def test_accept_cancellation_deletes_locked_human_scope_without_rebuild(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={"reason": "Mandat abandonné", "expected_version": 4},
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                cycle_id = requested.json()["cancellation_request_id"]

                accepted = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                    json={
                        "cancellation_request_id": cycle_id,
                        "comment": "Annulation confirmée par la coordination",
                        "expected_version": 5,
                        "expected_planning_version": 9,
                    },
                    headers={"Idempotency-Key": "accept-human-1"},
                )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            payload = accepted.json()
            self.assertEqual(payload["status"], "Annulée")
            self.assertEqual(payload["cancellation_state"], "ACCEPTED")
            self.assertEqual(payload["planning_version"], 10)
            self.assertEqual(payload["request_version"], 6)
            self.assertEqual(payload["deleted_human_shifts"], 1)
            self.assertEqual(payload["released_locked_human_shifts"], 1)
            self.assertEqual(payload["deleted_asset_allocations"], 0)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.get(WorkforceRequest, "D-HUMAN")
                    requirement = session.get(ResourceRequirement, "REQ-HUMAN")
                    self.assertIsNotNone(request)
                    self.assertIsNotNone(requirement)
                    assert request is not None
                    assert requirement is not None
                    self.assertEqual(request.status, "Annulée")
                    self.assertEqual(request.aggregate_version, 6)
                    self.assertEqual(request.cancellation_state, "ACCEPTED")
                    self.assertEqual(requirement.status, "Annulé")
                    self.assertIsNone(session.get(Shift, "SHIFT-HUMAN"))
                    # D-EMPTY remains untouched: an immediate global rebuild would be
                    # allowed to materialize other active planning output.
                    self.assertEqual(
                        int(session.scalar(select(func.count()).select_from(Shift)) or 0),
                        0,
                    )
                    planning_version = session.scalar(
                        select(PlanningMutationState.version).where(
                            PlanningMutationState.id == "GLOBAL"
                        )
                    )
                    self.assertEqual(int(planning_version or 0), 10)
                    history = session.scalar(
                        select(WorkforceRequestHistory).where(
                            WorkforceRequestHistory.workforce_request_id == "D-HUMAN",
                            WorkforceRequestHistory.action == "Acceptation d'annulation",
                        )
                    )
                    self.assertIsNotNone(history)
                    assert history is not None
                    details = json.loads(history.details or "{}")
                    self.assertEqual(details["correlation_id"], "accept-human-1")
                    self.assertEqual(details["planning_version"], 10)
                    self.assertEqual(details["resolution"], "ACCEPTED")
                    self.assertEqual(details["deleted_human_shift_ids"], ["SHIFT-HUMAN"])
                    self.assertEqual(
                        details[
                            "deleted_human_shift_operational_responsibility_overrides"
                        ],
                        [
                            {
                                "shift_id": "SHIFT-HUMAN",
                                "operational_responsible_override_contact_id": "C-CANCEL-RESP",
                            }
                        ],
                    )
            finally:
                engine.dispose()

    def test_accept_cancellation_deletes_shift_owned_ad_hoc_asset_scope(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                session.add(
                    Asset(
                        id="A-ADHOC",
                        code="TRUCK-ADHOC",
                        label="Camion ad hoc",
                        asset_type_id="AT1",
                    )
                )
                session.add(
                    AssetRequirement(
                        id="AREQ-ADHOC",
                        project_id="P1",
                        origin="SHIFT_AD_HOC",
                        shift_id="SHIFT-HUMAN",
                        asset_type_id="AT1",
                        start_date=DAY,
                        end_date=DAY,
                        status="Planifié",
                    )
                )
                session.flush()
                session.add(
                    AssetAllocation(
                        id="ALLOC-ADHOC",
                        asset_requirement_id="AREQ-ADHOC",
                        asset_id="A-ADHOC",
                        operator_resource_id="R1",
                        start_date=DAY,
                        end_date=DAY,
                        locked=True,
                        source="MANUAL",
                    )
                )
            engine.dispose()

            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={"reason": "Mandat avec actif ad hoc", "expected_version": 4},
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                accepted = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                    json={
                        "cancellation_request_id": requested.json()[
                            "cancellation_request_id"
                        ],
                        "comment": "Annulation coordonnée",
                        "expected_version": 5,
                        "expected_planning_version": 9,
                    },
                    headers={"Idempotency-Key": "accept-adhoc-560c"},
                )

            self.assertEqual(accepted.status_code, 200, accepted.text)
            payload = accepted.json()
            self.assertEqual(payload["deleted_human_shifts"], 1)
            self.assertEqual(payload["deleted_asset_allocations"], 1)
            self.assertEqual(payload["deleted_ad_hoc_asset_requirements"], 1)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    self.assertIsNone(session.get(Shift, "SHIFT-HUMAN"))
                    self.assertIsNone(
                        session.get(AssetRequirement, "AREQ-ADHOC")
                    )
                    self.assertIsNone(
                        session.get(AssetAllocation, "ALLOC-ADHOC")
                    )
                    # The REQUEST reservation from another demand is untouched.
                    self.assertIsNotNone(session.get(AssetRequirement, "AREQ-1"))
                    self.assertIsNotNone(session.get(AssetAllocation, "ALLOC-ASSET"))
                    history = session.scalar(
                        select(WorkforceRequestHistory).where(
                            WorkforceRequestHistory.workforce_request_id
                            == "D-HUMAN",
                            WorkforceRequestHistory.action
                            == "Acceptation d'annulation",
                        )
                    )
                    self.assertIsNotNone(history)
                    details = json.loads(history.details or "{}")
                    self.assertEqual(
                        details["deleted_ad_hoc_asset_requirement_ids"],
                        ["AREQ-ADHOC"],
                    )
            finally:
                engine.dispose()

    def test_segment_asset_forces_coordinated_cancellation_and_is_released_by_399(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                session.add_all(
                    [
                        Asset(
                            id="A-SEGMENT-399",
                            code="TRUCK-SEGMENT-399",
                            label="Camion segment",
                            asset_type_id="AT1",
                        ),
                        Asset(
                            id="A-DIRECT-399",
                            code="TRUCK-DIRECT-399",
                            label="Camion projet",
                            asset_type_id="AT1",
                        ),
                    ]
                )
                segment_requirement = AssetRequirement(
                    id="AREQ-SEGMENT-399",
                    project_id="P1",
                    origin="SEGMENT",
                    resource_requirement_id="REQ-EMPTY",
                    asset_type_id="AT1",
                    start_date=DAY,
                    end_date=DAY,
                    status="Planifié",
                )
                project_requirement = AssetRequirement(
                    id="AREQ-DIRECT-399",
                    project_id="P1",
                    origin="PROJECT_DIRECT",
                    asset_type_id="AT1",
                    start_date=DAY,
                    end_date=DAY,
                    status="Planifié",
                )
                session.add_all([segment_requirement, project_requirement])
                session.flush()
                session.add_all(
                    [
                        AssetAllocation(
                            id="ALLOC-SEGMENT-399",
                            asset_requirement_id=segment_requirement.id,
                            asset_id="A-SEGMENT-399",
                            operator_resource_id="R1",
                            start_date=DAY,
                            end_date=DAY,
                            locked=True,
                            source="MANUAL",
                        ),
                        AssetAllocation(
                            id="ALLOC-DIRECT-399",
                            asset_requirement_id=project_requirement.id,
                            asset_id="A-DIRECT-399",
                            operator_resource_id="R1",
                            start_date=DAY,
                            end_date=DAY,
                            locked=True,
                            source="MANUAL",
                        ),
                    ]
                )
            engine.dispose()

            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                workflow = client.get(
                    "/api/v1/demands/DMO-CANCEL-EMPTY/workflow-actions"
                )
                self.assertEqual(workflow.status_code, 200, workflow.text)
                self.assertTrue(
                    workflow.json()["cancellation"]["has_operational_decisions"]
                )
                self.assertFalse(
                    workflow.json()["cancellation"]["direct_cancel"]
                )
                self.assertTrue(
                    workflow.json()["cancellation"]["request_cancellation"]
                )

                direct = client.post(
                    "/api/v1/demands/DMO-CANCEL-EMPTY/cancel",
                    json={"expected_version": 3},
                )
                self.assertEqual(direct.status_code, 409, direct.text)
                self.assertEqual(
                    direct.json()["error"]["code"],
                    "active_operational_decisions",
                )

                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-EMPTY/request-cancellation",
                    json={
                        "reason": "Segment et actif à libérer",
                        "expected_version": 3,
                    },
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                accepted = client.post(
                    "/api/v1/demands/DMO-CANCEL-EMPTY/accept-cancellation",
                    json={
                        "cancellation_request_id": requested.json()[
                            "cancellation_request_id"
                        ],
                        "comment": "Annulation coordonnée du segment",
                        "expected_version": 4,
                        "expected_planning_version": 9,
                    },
                    headers={"Idempotency-Key": "accept-segment-575d"},
                )

            self.assertEqual(accepted.status_code, 200, accepted.text)
            payload = accepted.json()
            self.assertEqual(payload["deleted_asset_allocations"], 1)
            self.assertEqual(payload["deleted_segment_asset_requirements"], 1)
            self.assertEqual(payload["released_locked_asset_allocations"], 1)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    self.assertIsNone(
                        session.get(AssetRequirement, "AREQ-SEGMENT-399")
                    )
                    self.assertIsNone(
                        session.get(AssetAllocation, "ALLOC-SEGMENT-399")
                    )
                    # A project-direct reservation on the same project is not
                    # request-owned and must remain untouched by #399.
                    self.assertIsNotNone(
                        session.get(AssetRequirement, "AREQ-DIRECT-399")
                    )
                    self.assertIsNotNone(
                        session.get(AssetAllocation, "ALLOC-DIRECT-399")
                    )
                    owner = session.get(ResourceRequirement, "REQ-EMPTY")
                    self.assertIsNotNone(owner)
                    assert owner is not None
                    self.assertEqual(owner.status, "Annulé")
                    history = session.scalar(
                        select(WorkforceRequestHistory).where(
                            WorkforceRequestHistory.workforce_request_id
                            == "D-EMPTY",
                            WorkforceRequestHistory.action
                            == "Acceptation d'annulation",
                        )
                    )
                    self.assertIsNotNone(history)
                    assert history is not None
                    details = json.loads(history.details or "{}")
                    self.assertEqual(
                        details["deleted_segment_asset_requirement_ids"],
                        ["AREQ-SEGMENT-399"],
                    )
            finally:
                engine.dispose()

    def test_accept_cancellation_deletes_locked_asset_scope(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-ASSET/request-cancellation",
                    json={"reason": "Actif non requis", "expected_version": 2},
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                cycle_id = requested.json()["cancellation_request_id"]
                accepted = client.post(
                    "/api/v1/demands/DMO-CANCEL-ASSET/accept-cancellation",
                    json={
                        "cancellation_request_id": cycle_id,
                        "comment": "Annulation coordonnée",
                        "expected_version": 3,
                        "expected_planning_version": 9,
                    },
                    headers={"Idempotency-Key": "accept-asset-1"},
                )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            payload = accepted.json()
            self.assertEqual(payload["deleted_asset_allocations"], 1)
            self.assertEqual(payload["released_locked_asset_allocations"], 1)
            self.assertEqual(payload["cancelled_asset_requirements"], 1)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.get(WorkforceRequest, "D-ASSET")
                    requirement = session.get(AssetRequirement, "AREQ-1")
                    self.assertIsNotNone(request)
                    self.assertIsNotNone(requirement)
                    assert request is not None
                    assert requirement is not None
                    self.assertEqual(request.status, "Annulée")
                    self.assertEqual(request.cancellation_state, "ACCEPTED")
                    self.assertEqual(requirement.status, "Annulé")
                    self.assertIsNone(session.get(AssetAllocation, "ALLOC-ASSET"))
                    # Human planning owned by another demand is out of scope.
                    self.assertIsNotNone(session.get(Shift, "SHIFT-HUMAN"))
            finally:
                engine.dispose()

    def test_accept_replay_wins_before_stale_cas_and_records_once(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            headers = {"Idempotency-Key": "accept-replay-399c"}
            with TestClient(app, raise_server_exceptions=False) as client:
                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={"reason": "Retrait du mandat", "expected_version": 4},
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                body = {
                    "cancellation_request_id": requested.json()["cancellation_request_id"],
                    "comment": "Annulation acceptée",
                    "expected_version": 5,
                    "expected_planning_version": 9,
                }
                first = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                    json=body,
                    headers=headers,
                )
                replay = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                    json=body,
                    headers=headers,
                )
                stale_other_key = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                    json=body,
                    headers={"Idempotency-Key": "accept-replay-other-key"},
                )

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), first.json())
            self.assertEqual(stale_other_key.status_code, 409, stale_other_key.text)
            self.assertEqual(
                stale_other_key.json()["error"]["code"],
                "planning_version_conflict",
            )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    planning_version = session.scalar(
                        select(PlanningMutationState.version).where(
                            PlanningMutationState.id == "GLOBAL"
                        )
                    )
                    self.assertEqual(int(planning_version or 0), 10)
                    audit_count = session.scalar(
                        select(func.count())
                        .select_from(WorkforceRequestHistory)
                        .where(
                            WorkforceRequestHistory.workforce_request_id == "D-HUMAN",
                            WorkforceRequestHistory.action == "Acceptation d'annulation",
                        )
                    )
                    self.assertEqual(int(audit_count or 0), 1)
                    receipt_count = session.scalar(
                        select(func.count())
                        .select_from(CommandIdempotencyReceipt)
                        .where(
                            CommandIdempotencyReceipt.command_scope
                            == "demand_cancellation.accept",
                            CommandIdempotencyReceipt.idempotency_key
                            == "accept-replay-399c",
                        )
                    )
                    self.assertEqual(int(receipt_count or 0), 1)
            finally:
                engine.dispose()

    def test_stale_request_version_rolls_back_planning_guard_and_cleanup(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={"reason": "Retrait", "expected_version": 4},
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                rejected = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                    json={
                        "cancellation_request_id": requested.json()["cancellation_request_id"],
                        "comment": "Version obsolète",
                        "expected_version": 4,
                        "expected_planning_version": 9,
                    },
                    headers={"Idempotency-Key": "accept-stale-request-399c"},
                )
            self.assertEqual(rejected.status_code, 409, rejected.text)
            self.assertEqual(rejected.json()["error"]["code"], "demand_version_conflict")

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.get(WorkforceRequest, "D-HUMAN")
                    requirement = session.get(ResourceRequirement, "REQ-HUMAN")
                    assert request is not None
                    assert requirement is not None
                    self.assertEqual(request.status, "En planification")
                    self.assertEqual(request.cancellation_state, "PENDING")
                    self.assertEqual(request.aggregate_version, 5)
                    self.assertEqual(requirement.status, "Planifié")
                    self.assertIsNotNone(session.get(Shift, "SHIFT-HUMAN"))
                    self.assertEqual(
                        int(session.get(PlanningMutationState, "GLOBAL").version),
                        9,
                    )
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count())
                                .select_from(CommandIdempotencyReceipt)
                                .where(
                                    CommandIdempotencyReceipt.command_scope
                                    == "demand_cancellation.accept",
                                    CommandIdempotencyReceipt.idempotency_key
                                    == "accept-stale-request-399c",
                                )
                            )
                            or 0
                        ),
                        0,
                    )
            finally:
                engine.dispose()

    def test_injected_audit_failure_rolls_back_entire_acceptance(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={"reason": "Retrait", "expected_version": 4},
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                with patch.object(
                    SqlDemandRepository,
                    "_append_history",
                    side_effect=RuntimeError("399C injected audit failure"),
                ):
                    failed = client.post(
                        "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                        json={
                            "cancellation_request_id": requested.json()["cancellation_request_id"],
                            "comment": "Doit rollback",
                            "expected_version": 5,
                            "expected_planning_version": 9,
                        },
                        headers={"Idempotency-Key": "accept-rollback-399c"},
                    )
            self.assertEqual(failed.status_code, 500, failed.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.get(WorkforceRequest, "D-HUMAN")
                    requirement = session.get(ResourceRequirement, "REQ-HUMAN")
                    assert request is not None
                    assert requirement is not None
                    self.assertEqual(request.status, "En planification")
                    self.assertEqual(request.cancellation_state, "PENDING")
                    self.assertEqual(request.aggregate_version, 5)
                    self.assertEqual(requirement.status, "Planifié")
                    self.assertIsNotNone(session.get(Shift, "SHIFT-HUMAN"))
                    self.assertEqual(
                        int(session.get(PlanningMutationState, "GLOBAL").version),
                        9,
                    )
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count())
                                .select_from(WorkforceRequestHistory)
                                .where(
                                    WorkforceRequestHistory.workforce_request_id == "D-HUMAN",
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
                                    CommandIdempotencyReceipt.idempotency_key
                                    == "accept-rollback-399c",
                                )
                            )
                            or 0
                        ),
                        0,
                    )
            finally:
                engine.dispose()

    def test_planning_mutation_winner_forces_accept_retry_with_fresh_version(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={"reason": "Retrait", "expected_version": 4},
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                stale_accept = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                    json={
                        "cancellation_request_id": requested.json()["cancellation_request_id"],
                        "comment": "Version planning obsolète",
                        "expected_version": 5,
                        "expected_planning_version": 9,
                    },
                    headers={"Idempotency-Key": "accept-after-planning-399c"},
                )
            self.assertEqual(stale_accept.status_code, 409, stale_accept.text)
            self.assertEqual(
                stale_accept.json()["error"]["code"],
                "planning_version_conflict",
            )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.get(WorkforceRequest, "D-HUMAN")
                    assert request is not None
                    self.assertEqual(request.cancellation_state, "PENDING")
                    self.assertEqual(request.status, "En planification")
                    self.assertIsNotNone(session.get(Shift, "SHIFT-HUMAN"))
                    self.assertEqual(
                        int(session.get(PlanningMutationState, "GLOBAL").version),
                        10,
                    )
            finally:
                engine.dispose()

    def test_cancelled_request_cannot_be_rematerialized_by_approval_or_selection_sync(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={"reason": "Retrait", "expected_version": 4},
                )
                accepted = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                    json={
                        "cancellation_request_id": requested.json()["cancellation_request_id"],
                        "comment": "Annulation finale",
                        "expected_version": 5,
                        "expected_planning_version": 9,
                    },
                    headers={"Idempotency-Key": "accept-terminal-guard-399c"},
                )
                self.assertEqual(accepted.status_code, 200, accepted.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                for operation in ("approval", "selection"):
                    with self.subTest(operation=operation):
                        with self.assertRaises(ApplicationConflictError) as raised:
                            with factory.begin() as session:
                                adapter = SqlPeriodAwareApprovedDemandSyncAdapter(session)
                                if operation == "approval":
                                    adapter.sync_approved("DMO-CANCEL-HUMAN")
                                else:
                                    adapter.sync_operational_choices("DMO-CANCEL-HUMAN")
                        self.assertEqual(
                            raised.exception.code,
                            "cancelled_demand_materialization_forbidden",
                        )

                with factory() as session:
                    request = session.get(WorkforceRequest, "D-HUMAN")
                    requirement = session.get(ResourceRequirement, "REQ-HUMAN")
                    assert request is not None
                    assert requirement is not None
                    self.assertEqual(request.status, "Annulée")
                    self.assertEqual(request.cancellation_state, "ACCEPTED")
                    self.assertEqual(requirement.status, "Annulé")
                    self.assertIsNone(session.get(Shift, "SHIFT-HUMAN"))
                    self.assertEqual(
                        int(session.get(PlanningMutationState, "GLOBAL").version),
                        10,
                    )
            finally:
                engine.dispose()

    def test_later_global_rebuild_never_rematerializes_cancelled_request(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_CANCELLATION_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                requested = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/request-cancellation",
                    json={"reason": "Retrait", "expected_version": 4},
                )
                accepted = client.post(
                    "/api/v1/demands/DMO-CANCEL-HUMAN/accept-cancellation",
                    json={
                        "cancellation_request_id": requested.json()["cancellation_request_id"],
                        "comment": "Annulation finale",
                        "expected_version": 5,
                        "expected_planning_version": 9,
                    },
                    headers={"Idempotency-Key": "accept-before-rebuild-399c"},
                )
                self.assertEqual(accepted.status_code, 200, accepted.text)
                rebuilt = client.post("/api/v1/planning/rebuild")
                self.assertEqual(rebuilt.status_code, 200, rebuilt.text)

                stale_planning_command = client.post(
                    "/api/v1/allocations/SHIFT-HUMAN/duplicate",
                    json={
                        "resource_id": "R1",
                        "day": DAY.isoformat(),
                        "expected_planning_version": 9,
                    },
                    headers={"Idempotency-Key": "stale-planning-after-cancel-399c"},
                )

            self.assertEqual(stale_planning_command.status_code, 409, stale_planning_command.text)
            self.assertEqual(
                stale_planning_command.json()["error"]["code"],
                "planning_version_conflict",
            )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.get(WorkforceRequest, "D-HUMAN")
                    requirement = session.get(ResourceRequirement, "REQ-HUMAN")
                    assert request is not None
                    assert requirement is not None
                    self.assertEqual(request.status, "Annulée")
                    self.assertEqual(requirement.status, "Annulé")
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count())
                                .select_from(Shift)
                                .where(Shift.resource_requirement_id == "REQ-HUMAN")
                            )
                            or 0
                        ),
                        0,
                    )
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count())
                                .select_from(AssetAllocation)
                                .join(
                                    AssetRequirement,
                                    AssetAllocation.asset_requirement_id == AssetRequirement.id,
                                )
                                .where(AssetRequirement.workforce_request_id == "D-HUMAN")
                            )
                            or 0
                        ),
                        0,
                    )
                    self.assertEqual(
                        int(session.get(PlanningMutationState, "GLOBAL").version),
                        11,
                    )
            finally:
                engine.dispose()

    def test_repository_cas_rejects_stale_request_without_partial_write(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request = session.get(WorkforceRequest, "D-HUMAN")
                    assert request is not None
                    request.aggregate_version = 5

                with self.assertRaises(ApplicationConflictError) as raised:
                    with factory.begin() as session:
                        SqlDemandRepository(
                            session,
                            actor_name="Coordonnateur",
                        ).request_cancellation(
                            "DMO-CANCEL-HUMAN",
                            cancellation_request_id=(
                                "11111111-1111-1111-1111-111111111111"
                            ),
                            reason="Stale",
                            expected_version=4,
                        )
                self.assertEqual(raised.exception.code, "demand_version_conflict")

                with factory() as session:
                    request = session.get(WorkforceRequest, "D-HUMAN")
                    assert request is not None
                    self.assertEqual(request.aggregate_version, 5)
                    self.assertIsNone(request.cancellation_state)
                    self.assertIsNone(request.cancellation_request_id)
                    audit_count = session.scalar(
                        select(func.count())
                        .select_from(WorkforceRequestHistory)
                        .where(
                            WorkforceRequestHistory.workforce_request_id
                            == "D-HUMAN",
                            WorkforceRequestHistory.action
                            == "Demande d'annulation",
                        )
                    )
                    self.assertEqual(int(audit_count or 0), 0)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
