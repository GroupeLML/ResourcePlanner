from __future__ import annotations

import json
from datetime import timedelta, time
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from app.application.errors import ApplicationConflictError
from app.application.idempotency import IdempotentCommandExecutor
from sqlalchemy import select

from app.infrastructure.sql import (
    PlanningWorkQueueRelease,
    PlanningWorkQueueRestoration,
    RequestCompletionReduction,
    RequestLine,
    Resource,
    ResourceAvailabilityRule,
    RequestOperationalState,
    RequestRequirementPlanningIntent,
    ResourceRequirement,
    Shift,
    SqlPlanningCommandAdapter,
    SqlAllocationCommandAdapter,
    SqlPlannerQueryRepositoryWithLoadProfiles,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.request_plan_preparation import SqlRequestPlanPreparer
from app.infrastructure.sql.planning_intent_repository import SqlRequestRequirementPlanningIntentRepository
from app.infrastructure.sql.idempotency import SqlCommandIdempotencyAdapter
from app.infrastructure.sql.asset_models import Asset, AssetAllocation, AssetRequirement, AssetType
from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.infrastructure.sql.demand_period_repository import SqlDemandPeriodRepository
from app.infrastructure.sql.approval_revision_models import RequestApprovalReference, RequestApprovalRevision
from app.infrastructure.sql.approval_scope_models import ApprovalScopeApprover
from app.domain.demand_periods import DemandPeriodDefinition
from app.domain.request_reduction import reduction_subject_fingerprint
from app.server import create_api_app
from tests.approval_test_support import (
    TEST_ADMIN_USER_ID,
    routed_demand_payload,
    seed_test_approval_routing,
)
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


class RequestWorkQueuePhase3Tests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        from app.infrastructure.sql import Project

        session.add(Project(id="P-778-P3", number="P-778-P3", name="Projet 778 Phase 3"))
        session.add(
            Resource(
                id="R-778-P3",
                name="Alice 778 P3",
                resource_class="GENERAL",
                active=True,
                erp_active=True,
            )
        )
        session.flush()
        session.add(
            ResourceAvailabilityRule(
                id="AV-778-P3",
                resource_id="R-778-P3",
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
            filename="request-work-queue-phase3.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _approve(client: TestClient, hours: int) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload(
                {
                    "project_number": "P-778-P3",
                    "desired_start": "2026-10-28",
                    "desired_end": "2026-10-28",
                    "estimated_hours": hours,
                    "submit": True,
                }
            ),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "Approbation Phase 3 #778"},
        )
        assert approved.status_code == 200, approved.text
        return number

    @staticmethod
    def _context(session, number: str):
        request = session.scalar(
            select(WorkforceRequest).where(
                WorkforceRequest.legacy_demand_number == number
            )
        )
        assert request is not None
        requirement = session.scalar(
            select(ResourceRequirement).where(
                ResourceRequirement.workforce_request_id == request.id,
                ResourceRequirement.status != "Annulé",
            )
        )
        assert requirement is not None
        intent = session.scalar(
            select(RequestRequirementPlanningIntent).where(
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == requirement.id
            )
        )
        assert intent is not None
        return request, requirement, intent

    @staticmethod
    def _reduce(session, request, requirement, intent, hours: Decimal) -> None:
        session.add(
            RequestCompletionReduction(
                planning_intent_id=intent.id,
                workforce_request_id=request.id,
                historical_approval_revision_id=requirement.approval_revision_id,
                source_request_line_id=intent.source_request_line_id,
                approved_entry_key=intent.approved_entry_key,
                resource_requirement_id=requirement.id,
                original_shift_id="SHIFT-HIST-778",
                shift_snapshot_text="{}",
                reduction_hours=hours,
                effective_need_before=Decimal(requirement.planned_hours),
                effective_need_after=max(
                    Decimal(requirement.planned_hours) - hours,
                    Decimal("0"),
                ),
                actor_user_id=TEST_ADMIN_USER_ID,
                command_id=f"phase3-reduction-{hours}",
                reconciliation_state="ACTIVE",
            )
        )
        session.flush()

    def test_prepare_and_resync_use_reduced_effective_need(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    self._reduce(
                        session,
                        request,
                        requirement,
                        intent,
                        Decimal("2.00"),
                    )
                    prepared = SqlRequestPlanPreparer(session).prepare_active(
                        request,
                        current=(requirement,),
                    )
                    self.assertEqual(len(prepared.specs), 1)
                    self.assertEqual(prepared.specs[0].planned_hours, Decimal("6.00"))

                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    session.flush()
                    self.assertEqual(requirement.planned_hours, Decimal("6.00"))
                    self.assertEqual(
                        requirement.approval_revision_id,
                        prepared.approval_revision_id,
                    )
            finally:
                engine.dispose()

    def test_preserve_existing_survivor_keeps_exact_shift_identity_across_rebuild(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-preserve",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    _request, requirement, intent = self._context(session, number)
                    requirement.assigned_resource_id = "R-778-P3"
                    intent.intent_mode = "PRESERVE_EXISTING"
                    session.add(
                        Shift(
                            id="SHIFT-PRESERVE-778",
                            legacy_allocation_id="ALLOC-PRESERVE-778",
                            resource_requirement_id=requirement.id,
                            resource_id="R-778-P3",
                            work_date=requirement.start_date,
                            hours=Decimal("8.00"),
                            source="AUTO",
                            locked=False,
                        )
                    )
                    session.flush()
                    SqlPlanningCommandAdapter(session).rebuild()
                    rows = session.scalars(
                        select(Shift).where(
                            Shift.resource_requirement_id == requirement.id
                        )
                    ).all()
                    self.assertEqual([row.id for row in rows], ["SHIFT-PRESERVE-778"])
                    self.assertEqual(rows[0].hours, Decimal("8.00"))
                    self.assertEqual(rows[0].resource_id, "R-778-P3")
            finally:
                engine.dispose()

    def test_queued_slot_stays_empty_and_unassigned_through_resync_and_rebuild(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-queued",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    _request, requirement, intent = self._context(session, number)
                    requirement.assigned_resource_id = "R-778-P3"
                    intent.intent_mode = "QUEUED"
                    intent.target_mode = "EXPLICIT_NONE"
                    intent.target_resource_id = None
                    session.flush()

                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    self.assertIsNone(requirement.assigned_resource_id)
                    SqlPlanningCommandAdapter(session).rebuild()
                    self.assertEqual(
                        session.scalars(
                            select(Shift).where(
                                Shift.resource_requirement_id == requirement.id
                            )
                        ).all(),
                        [],
                    )
                    self.assertIsNone(requirement.assigned_resource_id)
            finally:
                engine.dispose()

    def test_work_queue_exposes_only_preserved_residual(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-queue-residual",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    _request, requirement, intent = self._context(session, number)
                    requirement.assigned_resource_id = "R-778-P3"
                    intent.intent_mode = "PRESERVE_EXISTING"
                    session.add(
                        Shift(
                            id="SHIFT-RESIDUAL-778",
                            legacy_allocation_id="ALLOC-RESIDUAL-778",
                            resource_requirement_id=requirement.id,
                            resource_id="R-778-P3",
                            work_date=requirement.start_date,
                            hours=Decimal("4.00"),
                            source="AUTO",
                            locked=False,
                        )
                    )
                    session.flush()
                    SqlPlanningCommandAdapter(session).rebuild()
                    actions = SqlPlannerQueryRepositoryWithLoadProfiles(
                        session
                    ).list_planning_actions(
                        start=requirement.start_date,
                        end=requirement.end_date,
                    )
                    assignments = [
                        row for row in actions
                        if row.kind == "ASSIGNMENT"
                        and row.segment_id
                        == (requirement.legacy_segment_id or requirement.id)
                    ]
                    self.assertEqual(len(assignments), 1)
                    self.assertEqual(assignments[0].planned_hours, 4.0)
            finally:
                engine.dispose()

    def test_work_queue_exposes_full_need_for_queued_slot(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-queue-full",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    _request, requirement, intent = self._context(session, number)
                    intent.intent_mode = "QUEUED"
                    intent.target_mode = "EXPLICIT_NONE"
                    intent.target_resource_id = None
                    requirement.assigned_resource_id = None
                    session.flush()
                    SqlPlanningCommandAdapter(session).rebuild()
                    actions = SqlPlannerQueryRepositoryWithLoadProfiles(
                        session
                    ).list_planning_actions(
                        start=requirement.start_date,
                        end=requirement.end_date,
                    )
                    assignments = [
                        row for row in actions
                        if row.kind == "ASSIGNMENT"
                        and row.segment_id
                        == (requirement.legacy_segment_id or requirement.id)
                    ]
                    self.assertEqual(len(assignments), 1)
                    self.assertEqual(assignments[0].planned_hours, 8.0)
            finally:
                engine.dispose()

    def test_full_reduction_removes_zero_hour_slot_from_active_materialization(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-zero",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    self._reduce(
                        session,
                        request,
                        requirement,
                        intent,
                        Decimal("8.00"),
                    )
                    prepared = SqlRequestPlanPreparer(session).prepare_active(
                        request,
                        current=(requirement,),
                    )
                    self.assertEqual(prepared.specs, ())

                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    session.flush()
                    self.assertEqual(requirement.status, "Annulé")
                    active = session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                    ).all()
                    self.assertEqual(active, [])
            finally:
                engine.dispose()

    def test_zero_reduction_keeps_lifecycle_and_cannot_rematerialize_after_rebuild_or_reapproval(self) -> None:
        for previous_status, expected_status in (("Planifié", "Annulé"), ("Terminé", "Terminé")):
            with self.subTest(previous=previous_status), TemporaryDirectory() as directory:
                database_url = self._database(directory)
                app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                try:
                    with TestClient(app, raise_server_exceptions=False) as client:
                        number = self._approve(client, 8)
                        with factory() as session:
                            _, requirement, _ = self._context(session, number)
                            requirement_id = requirement.id
                        assigned = client.post(f"/api/v1/segments/{requirement_id}/assign",
                                               json={"resource_id": "R-778-P3"})
                        self.assertEqual(assigned.status_code, 200, assigned.text)
                        with factory.begin() as session:
                            request, requirement, intent = self._context(session, number)
                            request_id, requirement_id, intent_id = request.id, requirement.id, intent.id
                            requirement.status = previous_status
                            shift = session.scalar(select(Shift).where(Shift.resource_requirement_id == requirement_id))
                            self.assertIsNotNone(shift)
                            shift_id = shift.legacy_allocation_id or shift.id
                            original_revision = requirement.approval_revision_id
                            original_history = session.get(RequestApprovalRevision, original_revision).payload_text
                        preview = client.get(f"/api/v1/allocations/{shift_id}/work-queue-preview").json()
                        reduced = client.post(
                            f"/api/v1/allocations/{shift_id}/permanent-reduction",
                            headers={"Idempotency-Key": f"zero-lifecycle-{expected_status == 'Terminé'}"},
                            json={"expected_planning_version": preview["planning_version"],
                                  "expected_request_version": preview["request_version"],
                                  "expected_approval_revision_id": preview["approval_revision_id"],
                                  "expected_operational_version": preview["operational_version"]},
                        )
                        self.assertEqual(reduced.status_code, 200, reduced.text)
                        self.assertEqual(reduced.json()["effective_need_after"], 0)
                        for synchronize in (None, "sync_operational_choices", "sync_approved"):
                            if synchronize:
                                with factory.begin() as session:
                                    getattr(SqlPeriodAwareApprovedDemandSyncAdapter(session), synchronize)(number)
                            rebuilt = client.post("/api/v1/planning/rebuild")
                            self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
                            with factory() as session:
                                self.assertEqual(session.get(ResourceRequirement, requirement_id).status, expected_status)
                                self.assertEqual(session.scalars(select(Shift).join(ResourceRequirement).where(
                                    ResourceRequirement.workforce_request_id == request_id
                                )).all(), [])
                                intent = session.get(RequestRequirementPlanningIntent, intent_id)
                                current_id = intent.current_resource_requirement_id
                                current = session.get(ResourceRequirement, current_id)
                                self.assertEqual(current.status, expected_status)
                                self.assertEqual(SqlRequestRequirementPlanningIntentRepository(session)
                                                 .current_effective_need_for_requirement(current_id, fallback_hours=current.planned_hours),
                                                 Decimal("0.00"))
                                self.assertEqual(session.get(RequestApprovalRevision, original_revision).payload_text, original_history)
                                self.assertEqual(len(session.scalars(select(RequestCompletionReduction).where(
                                    RequestCompletionReduction.planning_intent_id == intent_id,
                                    RequestCompletionReduction.reconciliation_state == "ACTIVE",
                                )).all()), 1)
                                if synchronize is None:
                                    # Both current zero projections and immutable history are observable immediately.
                                    response = client.get(f"/api/v1/segments/{requirement_id}")
                                    self.assertEqual(response.status_code, 200, response.text)
                                    self.assertEqual(response.json()["planned_hours"], 0)
                                    self.assertEqual(response.json()["remaining_hours"], 0)
                finally:
                    engine.dispose()




    def test_http_preview_is_authoritative_and_revoked_current_approver_is_denied(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-http-preview",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    _request, requirement, _intent = self._context(session, number)
                    session.query(Shift).filter_by(
                        resource_requirement_id=requirement.id
                    ).delete(synchronize_session=False)
                    requirement.assigned_resource_id = "R-778-P3"
                    requirement.status = "Planifié"
                    session.add(
                        Shift(
                            id="SHIFT-HTTP-778",
                            legacy_allocation_id="ALLOC-HTTP-778",
                            resource_requirement_id=requirement.id,
                            resource_id="R-778-P3",
                            work_date=requirement.start_date,
                            hours=Decimal("8.00"),
                            source="AUTO",
                            locked=False,
                        )
                    )
                    session.flush()

                with TestClient(app, raise_server_exceptions=False) as client:
                    preview_response = client.get(
                        "/api/v1/allocations/ALLOC-HTTP-778/work-queue-preview"
                    )
                    self.assertEqual(
                        preview_response.status_code,
                        200,
                        preview_response.text,
                    )
                    preview = preview_response.json()
                    self.assertTrue(preview["return_shift_allowed"])
                    self.assertTrue(preview["return_segment_allowed"])
                    self.assertTrue(preview["permanent_reduction_allowed"])
                    self.assertEqual(preview["shift_hours"], 8.0)
                    self.assertEqual(preview["effective_need"], 8.0)
                    self.assertEqual(preview["remaining_after_return_shift"], 8.0)
                    self.assertEqual(preview["remaining_after_return_segment"], 8.0)
                    self.assertEqual(preview["effective_need_after_reduction"], 0.0)
                    self.assertEqual(preview["remaining_after_reduction"], 0.0)

                with factory.begin() as session:
                    memberships = session.query(ApprovalScopeApprover).filter_by(
                        app_user_id=TEST_ADMIN_USER_ID
                    ).all()
                    self.assertTrue(memberships)
                    for membership in memberships:
                        session.delete(membership)

                with TestClient(app, raise_server_exceptions=False) as client:
                    blocked_preview = client.get(
                        "/api/v1/allocations/ALLOC-HTTP-778/work-queue-preview"
                    )
                    self.assertEqual(
                        blocked_preview.status_code,
                        200,
                        blocked_preview.text,
                    )
                    blocked = blocked_preview.json()
                    self.assertTrue(blocked["return_shift_allowed"])
                    self.assertTrue(blocked["return_segment_allowed"])
                    self.assertFalse(blocked["permanent_reduction_allowed"])
                    self.assertEqual(
                        blocked["permanent_reduction_block_code"],
                        "reduction_approval_authority_denied",
                    )
                    denied = client.post(
                        "/api/v1/allocations/ALLOC-HTTP-778/permanent-reduction",
                        headers={"Idempotency-Key": "http-revoked-778"},
                        json={
                            "expected_planning_version": blocked["planning_version"],
                            "expected_request_version": blocked["request_version"],
                            "expected_approval_revision_id": blocked["approval_revision_id"],
                            "expected_operational_version": blocked["operational_version"],
                        },
                    )
                    self.assertEqual(denied.status_code, 403, denied.text)
                    self.assertEqual(
                        denied.json()["error"]["code"],
                        "reduction_approval_authority_denied",
                    )

                with factory() as session:
                    self.assertIsNotNone(session.get(Shift, "SHIFT-HTTP-778"))
                    self.assertEqual(
                        session.query(RequestCompletionReduction).count(),
                        0,
                    )
            finally:
                engine.dispose()



    def test_pending_candidate_does_not_hide_active_approved_residual(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-pending-residual",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    request.status = "Soumise"
                    intent.intent_mode = "QUEUED"
                    intent.target_mode = "EXPLICIT_NONE"
                    intent.target_resource_id = None
                    requirement.assigned_resource_id = None
                    session.query(Shift).filter_by(
                        resource_requirement_id=requirement.id
                    ).delete(synchronize_session=False)
                    session.flush()

                    actions = SqlPlannerQueryRepositoryWithLoadProfiles(
                        session
                    ).list_planning_actions(
                        start=requirement.start_date,
                        end=requirement.end_date,
                    )
                    assignments = [
                        row
                        for row in actions
                        if row.kind == "ASSIGNMENT"
                        and row.segment_id
                        == (requirement.legacy_segment_id or requirement.id)
                    ]
                    self.assertEqual(len(assignments), 1)
                    self.assertEqual(assignments[0].planned_hours, 8.0)
            finally:
                engine.dispose()


    def test_compatible_reapproval_new_physical_period_preserves_40_to_32_reduction(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-compatible-reapproval",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload(
                        {
                            "project_number": "P-778-P3",
                            "submit": True,
                            "lines": [
                                {
                                    "desired_start": "2026-10-28",
                                    "desired_end": "2026-10-28",
                                    "estimated_hours": 40,
                                    "confirmation": "Confirmée",
                                }
                            ],
                        }
                    ),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                demand = client.get(f"/api/v1/demands/{number}").json()
                line_id = demand["lines"][0]["line_id"]
                periods = client.put(
                    f"/api/v1/demands/{number}/lines/{line_id}/periods",
                    json={
                        "expected_request_version": demand["version"],
                        "periods": [
                            {
                                "period_id": "WORK",
                                "start_date": "2026-10-28",
                                "end_date": "2026-10-28",
                                "hours": 40,
                                "kind": "CUMULATIVE",
                                "confirmation": "Confirmée",
                                "resource_count": 1,
                            }
                        ],
                    },
                )
                self.assertEqual(periods.status_code, 200, periods.text)
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Approbation initiale #778"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    reference = session.get(RequestApprovalReference, request.id)
                    self.assertIsNotNone(reference)
                    revision = session.get(
                        RequestApprovalRevision,
                        reference.active_revision_id,
                    )
                    payload = __import__("json").loads(revision.payload_text)
                    entry = next(
                        row
                        for row in payload["authorization"]["entries"]
                        if row["identity"] == intent.approved_entry_key
                    )
                    fingerprint = reduction_subject_fingerprint(
                        request_context=payload["request"],
                        authorization_entry=entry,
                    )
                    session.add(
                        RequestCompletionReduction(
                            planning_intent_id=intent.id,
                            workforce_request_id=request.id,
                            historical_approval_revision_id=revision.id,
                            source_request_line_id=intent.source_request_line_id,
                            approved_entry_key=intent.approved_entry_key,
                            resource_requirement_id=requirement.id,
                            original_shift_id="SHIFT-HIST-COMPAT-778",
                            shift_snapshot_text="{}",
                            reduction_hours=Decimal("8.00"),
                            effective_need_before=Decimal("40.00"),
                            effective_need_after=Decimal("32.00"),
                            actor_user_id=TEST_ADMIN_USER_ID,
                            command_id="compatible-reapproval-reduction-778",
                            reconciliation_state="ACTIVE",
                            authorization_fingerprint=fingerprint,
                            reconciled_approval_revision_id=revision.id,
                        )
                    )
                    session.flush()
                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    self.assertEqual(requirement.planned_hours, Decimal("32.00"))
                    original_intent_id = intent.id
                    original_period_id = intent.source_period_id

                    SqlDemandPeriodRepository(session).replace_for_demand(
                        number,
                        (
                            DemandPeriodDefinition(
                                period_id="WORK",
                                start_date=requirement.start_date,
                                end_date=requirement.end_date,
                                hours=Decimal("40.00"),
                                kind="CUMULATIVE",
                                confirmation="Confirmée",
                                resource_count=1,
                            ),
                        ),
                        request_line_id=line_id,
                    )
                    session.flush()

                    SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved(
                        number,
                        approved_request_version=int(request.aggregate_version or 1),
                    )
                    session.flush()

                    current_intents = session.scalars(
                        select(RequestRequirementPlanningIntent).where(
                            RequestRequirementPlanningIntent.workforce_request_id
                            == request.id,
                            RequestRequirementPlanningIntent.approved_entry_key
                            == intent.approved_entry_key,
                        )
                    ).all()
                    self.assertEqual(len(current_intents), 1)
                    rebound = current_intents[0]
                    self.assertEqual(rebound.id, original_intent_id)
                    self.assertNotEqual(rebound.source_period_id, original_period_id)
                    current_requirement = session.get(
                        ResourceRequirement,
                        rebound.current_resource_requirement_id,
                    )
                    self.assertEqual(current_requirement.planned_hours, Decimal("32.00"))
                    reduction = session.query(RequestCompletionReduction).one()
                    self.assertEqual(
                        reduction.reconciled_approval_revision_id,
                        current_requirement.approval_revision_id,
                    )
            finally:
                engine.dispose()



    def test_compatible_reapproval_with_later_start_retains_cutoff(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-777-later-start",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json=routed_demand_payload(
                        {
                            "project_number": "P-778-P3",
                            "desired_start": "2026-10-28",
                            "desired_end": "2026-10-30",
                            "estimated_hours": 24,
                            "submit": True,
                        }
                    ),
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]
                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Approbation initiale cutoff #777"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    original_cutoff = requirement.start_date + timedelta(days=1)
                    requirement.assigned_resource_id = "R-778-P3"
                    requirement.status = "Planifié"
                    intent.target_mode = "EXPLICIT_RESOURCE"
                    intent.target_resource_id = "R-778-P3"
                    intent.placement_not_before_date = original_cutoff
                    line = session.get(RequestLine, intent.source_request_line_id)
                    self.assertIsNotNone(line)
                    line.desired_start = requirement.start_date + timedelta(days=2)
                    line.desired_end = requirement.start_date + timedelta(days=2)
                    session.flush()

                    SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved(
                        number,
                        approved_request_version=int(request.aggregate_version or 1),
                    )
                    session.flush()

                    rebound = session.get(
                        RequestRequirementPlanningIntent,
                        intent.id,
                    )
                    current_requirement = session.get(
                        ResourceRequirement,
                        rebound.current_resource_requirement_id,
                    )
                    self.assertEqual(
                        rebound.placement_not_before_date,
                        original_cutoff,
                    )
                    self.assertEqual(
                        current_requirement.start_date,
                        original_cutoff + timedelta(days=1),
                    )
                    SqlPlanningCommandAdapter(session).rebuild()
                    generated = session.scalars(
                        select(Shift).where(
                            Shift.resource_requirement_id
                            == current_requirement.id,
                            Shift.allocation_type != "Hors horaire requis",
                        )
                    ).all()
                    self.assertTrue(generated)
                    self.assertTrue(
                        all(
                            row.work_date >= current_requirement.start_date
                            for row in generated
                        )
                    )
            finally:
                engine.dispose()


    def test_compatible_reapproval_preserves_cutoff_and_no_interval_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-777-compatible-reapproval",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    cutoff = requirement.start_date
                    intent.placement_not_before_date = cutoff
                    intent_id = intent.id
                    session.flush()
                    SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved(
                        number,
                        approved_request_version=int(request.aggregate_version or 1),
                    )
                    rebound = session.get(RequestRequirementPlanningIntent, intent_id)
                    self.assertEqual(rebound.placement_not_before_date, cutoff)

                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    intent.placement_not_before_date = requirement.end_date + timedelta(days=1)
                    session.flush()
                    with self.assertRaises(ApplicationConflictError) as raised:
                        SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved(
                            number,
                            approved_request_version=int(request.aggregate_version or 1),
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "placement_not_before_reconciliation_required",
                    )
            finally:
                engine.dispose()

    def test_zero_effective_terminal_slot_keeps_dormant_cutoff(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-777-zero-cutoff",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    cutoff = requirement.start_date
                    intent.placement_not_before_date = cutoff
                    self._reduce(
                        session,
                        request,
                        requirement,
                        intent,
                        Decimal("8.00"),
                    )
                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    self.assertEqual(requirement.status, "Annulé")
                    self.assertEqual(intent.placement_not_before_date, cutoff)

                with factory.begin() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved(
                        number,
                        approved_request_version=int(request.aggregate_version or 1),
                    )
                    intent = session.scalar(select(RequestRequirementPlanningIntent))
                    self.assertEqual(intent.placement_not_before_date, cutoff)
            finally:
                engine.dispose()


    def test_incompatible_resource_class_reapproval_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-incompatible-reapproval",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    self._reduce(
                        session,
                        request,
                        requirement,
                        intent,
                        Decimal("2.00"),
                    )
                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    line = session.get(RequestLine, intent.source_request_line_id)
                    self.assertIsNotNone(line)
                    line.required_resource_class = "ARCHITECT-CHANGED-CLASS"
                    session.flush()

                    with self.assertRaises(ApplicationConflictError) as raised:
                        SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved(
                            number,
                            approved_request_version=int(request.aggregate_version or 1),
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "reduction_rebase_required",
                    )
                    self.assertEqual(requirement.planned_hours, Decimal("6.00"))
            finally:
                engine.dispose()


    def test_preserved_survivor_consumes_capacity_before_other_requirement(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-capacity",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    _request, requirement, intent = self._context(session, number)
                    requirement.assigned_resource_id = "R-778-P3"
                    intent.intent_mode = "PRESERVE_EXISTING"
                    session.add(
                        Shift(
                            id="SHIFT-CAPACITY-778",
                            legacy_allocation_id="ALLOC-CAPACITY-778",
                            resource_requirement_id=requirement.id,
                            resource_id="R-778-P3",
                            work_date=requirement.start_date,
                            hours=Decimal("8.00"),
                            source="AUTO",
                            locked=False,
                        )
                    )
                    other = ResourceRequirement(
                        id="REQ-CAPACITY-OTHER-778",
                        legacy_segment_id="SEG-CAPACITY-OTHER-778",
                        project_id=requirement.project_id,
                        start_date=requirement.start_date,
                        end_date=requirement.start_date,
                        planned_hours=Decimal("8.00"),
                        status="Planifié",
                        origin="AD_HOC",
                        assigned_resource_id="R-778-P3",
                        planning_type="Flexible",
                    )
                    session.add(other)
                    session.flush()

                    SqlPlanningCommandAdapter(session).rebuild()
                    competing_shifts = session.scalars(
                        select(Shift).where(
                            Shift.resource_requirement_id == other.id
                        )
                    ).all()
                    self.assertTrue(competing_shifts)
                    self.assertTrue(
                        all(
                            row.allocation_type == MISSING_ALLOCATION_TYPE
                            for row in competing_shifts
                        )
                    )
                    self.assertEqual(
                        sum(
                            (
                                row.hours
                                for row in competing_shifts
                                if row.allocation_type != MISSING_ALLOCATION_TYPE
                            ),
                            Decimal("0"),
                        ),
                        Decimal("0"),
                    )
                    other_after = SqlPlannerQueryRepositoryWithLoadProfiles(
                        session
                    ).get_segment(other.id)
                    self.assertIsNotNone(other_after)
                    self.assertEqual(other_after.covered_hours, 0.0)
                    self.assertEqual(other_after.remaining_hours, 8.0)
                    survivor = session.get(Shift, "SHIFT-CAPACITY-778")
                    self.assertIsNotNone(survivor)
                    self.assertEqual(survivor.hours, Decimal("8.00"))
            finally:
                engine.dispose()

    def test_auto_locked_survivor_consumes_capacity_exactly_once(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-locked-capacity",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    _request, requirement, intent = self._context(session, number)
                    requirement.assigned_resource_id = "R-778-P3"
                    requirement.planned_hours = Decimal("4.00")
                    intent.intent_mode = "AUTO"
                    session.add(
                        Shift(
                            id="SHIFT-LOCKED-CAPACITY-778",
                            legacy_allocation_id="ALLOC-LOCKED-CAPACITY-778",
                            resource_requirement_id=requirement.id,
                            resource_id="R-778-P3",
                            work_date=requirement.start_date,
                            hours=Decimal("4.00"),
                            source="MANUAL",
                            locked=True,
                        )
                    )
                    other = ResourceRequirement(
                        id="REQ-LOCKED-CAPACITY-OTHER-778",
                        legacy_segment_id="SEG-LOCKED-CAPACITY-OTHER-778",
                        project_id=requirement.project_id,
                        start_date=requirement.start_date,
                        end_date=requirement.start_date,
                        planned_hours=Decimal("4.00"),
                        status="Planifié",
                        origin="AD_HOC",
                        assigned_resource_id="R-778-P3",
                        planning_type="Flexible",
                    )
                    session.add(other)
                    session.flush()

                    SqlPlanningCommandAdapter(session).rebuild(
                        preserve_shift_ids=("SHIFT-LOCKED-CAPACITY-778",)
                    )
                    competing = session.scalars(
                        select(Shift).where(
                            Shift.resource_requirement_id == other.id,
                            Shift.allocation_type != MISSING_ALLOCATION_TYPE,
                        )
                    ).all()
                    self.assertEqual(sum((row.hours for row in competing), Decimal("0")), Decimal("4.00"))
                    survivor = session.get(Shift, "SHIFT-LOCKED-CAPACITY-778")
                    self.assertIsNotNone(survivor)
                    self.assertTrue(survivor.locked)
                    self.assertEqual(survivor.hours, Decimal("4.00"))
            finally:
                engine.dispose()

    def test_auto_asset_locked_survivor_consumes_capacity_exactly_once(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-asset-locked-capacity",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    _request, requirement, intent = self._context(session, number)
                    requirement.assigned_resource_id = "R-778-P3"
                    requirement.planned_hours = Decimal("4.00")
                    intent.intent_mode = "AUTO"
                    shift = Shift(
                        id="SHIFT-ASSET-LOCKED-CAPACITY-778",
                        legacy_allocation_id="ALLOC-ASSET-LOCKED-CAPACITY-778",
                        resource_requirement_id=requirement.id,
                        resource_id="R-778-P3",
                        work_date=requirement.start_date,
                        hours=Decimal("4.00"),
                        source="MANUAL",
                        locked=True,
                    )
                    session.add(shift)
                    session.add(
                        AssetType(
                            id="AT-ASSET-LOCKED-778",
                            code="AT-ASSET-LOCKED-778",
                            label="Asset locked 778",
                            category="EQUIPMENT",
                        )
                    )
                    session.flush()
                    session.add(
                        Asset(
                            id="AS-ASSET-LOCKED-778",
                            code="AS-ASSET-LOCKED-778",
                            label="Asset locked 778",
                            asset_type_id="AT-ASSET-LOCKED-778",
                        )
                    )
                    session.add(
                        AssetRequirement(
                            id="AR-ASSET-LOCKED-778",
                            project_id=requirement.project_id,
                            origin="SHIFT_AD_HOC",
                            shift_id=shift.id,
                            asset_type_id="AT-ASSET-LOCKED-778",
                            start_date=requirement.start_date,
                            end_date=requirement.start_date,
                            status="Planifié",
                        )
                    )
                    session.flush()
                    session.add(
                        AssetAllocation(
                            id="AA-ASSET-LOCKED-778",
                            asset_requirement_id="AR-ASSET-LOCKED-778",
                            asset_id="AS-ASSET-LOCKED-778",
                            operator_resource_id="R-778-P3",
                            start_date=requirement.start_date,
                            end_date=requirement.start_date,
                            locked=True,
                        )
                    )
                    other = ResourceRequirement(
                        id="REQ-ASSET-LOCKED-OTHER-778",
                        legacy_segment_id="SEG-ASSET-LOCKED-OTHER-778",
                        project_id=requirement.project_id,
                        start_date=requirement.start_date,
                        end_date=requirement.start_date,
                        planned_hours=Decimal("4.00"),
                        status="Planifié",
                        origin="AD_HOC",
                        assigned_resource_id="R-778-P3",
                        planning_type="Flexible",
                    )
                    session.add(other)
                    session.flush()

                    SqlPlanningCommandAdapter(session).rebuild(
                        preserve_shift_ids=(shift.id,)
                    )
                    competing = session.scalars(
                        select(Shift).where(
                            Shift.resource_requirement_id == other.id,
                            Shift.allocation_type != MISSING_ALLOCATION_TYPE,
                        )
                    ).all()
                    self.assertEqual(sum((row.hours for row in competing), Decimal("0")), Decimal("4.00"))
                    self.assertIsNotNone(session.get(AssetAllocation, "AA-ASSET-LOCKED-778"))
                    self.assertTrue(session.get(Shift, shift.id).locked)
            finally:
                engine.dispose()

    def test_current_override_projects_materialized_need_not_historical_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-current-override",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 40)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    self._reduce(
                        session,
                        request,
                        requirement,
                        intent,
                        Decimal("8.00"),
                    )
                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    self.assertEqual(requirement.planned_hours, Decimal("32.00"))

                    state = session.get(RequestOperationalState, request.id)
                    self.assertIsNotNone(state)
                    state.budget_overrides_text = json.dumps(
                        {intent.approved_entry_key: 36.0},
                        separators=(",", ":"),
                    )
                    session.flush()
                    SqlPeriodAwareApprovedDemandSyncAdapter(
                        session
                    ).sync_operational_choices(number)
                    session.flush()

                    self.assertEqual(requirement.planned_hours, Decimal("28.00"))
                    current_need = (
                        SqlRequestRequirementPlanningIntentRepository(
                            session
                        ).current_effective_need_for_requirement(
                            requirement.id,
                            fallback_hours=requirement.planned_hours,
                        )
                    )
                    self.assertEqual(current_need, Decimal("28.00"))

                    session.query(Shift).filter_by(
                        resource_requirement_id=requirement.id
                    ).delete(synchronize_session=False)
                    session.add(
                        Shift(
                            id="SHIFT-CURRENT-OVERRIDE-778",
                            legacy_allocation_id="ALLOC-CURRENT-OVERRIDE-778",
                            resource_requirement_id=requirement.id,
                            resource_id="R-778-P3",
                            work_date=requirement.start_date,
                            hours=Decimal("28.00"),
                            source="AUTO",
                            locked=False,
                        )
                    )
                    session.flush()
                    segment = SqlPlannerQueryRepositoryWithLoadProfiles(
                        session
                    ).get_segment(requirement.id)
                    self.assertIsNotNone(segment)
                    self.assertEqual(segment.planned_hours, 28.0)
                    self.assertEqual(segment.covered_hours, 28.0)
                    self.assertEqual(segment.remaining_hours, 0.0)
            finally:
                engine.dispose()

    def test_controlled_placement_rejects_above_residual_and_accepts_exact_residual(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-placement-bound",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    intent.intent_mode = "PRESERVE_EXISTING"
                    session.query(Shift).filter_by(
                        resource_requirement_id=requirement.id
                    ).delete(synchronize_session=False)
                    survivor = Shift(
                        id="SHIFT-PLACEMENT-SURVIVOR-778",
                        legacy_allocation_id="ALLOC-PLACEMENT-SURVIVOR-778",
                        resource_requirement_id=requirement.id,
                        resource_id="R-778-P3",
                        work_date=requirement.start_date,
                        hours=Decimal("4.00"),
                        source="AUTO",
                        locked=False,
                    )
                    release = PlanningWorkQueueRelease(
                        id="REL-PLACEMENT-778",
                        planning_intent_id=intent.id,
                        resource_requirement_id=requirement.id,
                        original_shift_id="SHIFT-PLACEMENT-RETURNED-778",
                        workforce_request_id=request.id,
                        approval_revision_id=requirement.approval_revision_id,
                        approved_entry_key=intent.approved_entry_key,
                        resource_id="R-778-P3",
                        work_date=requirement.start_date,
                        hours=Decimal("4.00"),
                        shift_snapshot_text="{}",
                        actor_user_id=TEST_ADMIN_USER_ID,
                        command_id="release-placement-778",
                        active=True,
                    )
                    session.add_all((survivor, release))
                    session.flush()

                    allocation = SqlAllocationCommandAdapter(session)
                    with self.assertRaises(ApplicationConflictError) as raised:
                        allocation.create_manual(
                            requirement.id,
                            "R-778-P3",
                            requirement.start_date,
                            Decimal("8.00"),
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "request_work_queue_placement_exceeds_residual",
                    )
                    self.assertTrue(release.active)
                    self.assertEqual(
                        session.query(Shift).filter_by(
                            resource_requirement_id=requirement.id
                        ).count(),
                        1,
                    )

                    allocation.create_manual(
                        requirement.id,
                        "R-778-P3",
                        requirement.start_date,
                        Decimal("4.00"),
                    )
                    self.assertFalse(release.active)
                    counted = sum(
                        (
                            row.hours
                            for row in session.scalars(
                                select(Shift).where(
                                    Shift.resource_requirement_id == requirement.id,
                                    (Shift.allocation_type.is_(None))
                                    | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
                                )
                            ).all()
                        ),
                        Decimal("0"),
                    )
                    self.assertEqual(counted, Decimal("8.00"))
            finally:
                engine.dispose()

    def test_partial_controlled_placement_leaves_residual_and_release_active(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-placement-partial",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    intent.intent_mode = "PRESERVE_EXISTING"
                    session.query(Shift).filter_by(
                        resource_requirement_id=requirement.id
                    ).delete(synchronize_session=False)
                    release = PlanningWorkQueueRelease(
                        id="REL-PLACEMENT-PARTIAL-778",
                        planning_intent_id=intent.id,
                        resource_requirement_id=requirement.id,
                        original_shift_id="SHIFT-PLACEMENT-PARTIAL-RETURNED-778",
                        workforce_request_id=request.id,
                        approval_revision_id=requirement.approval_revision_id,
                        approved_entry_key=intent.approved_entry_key,
                        resource_id="R-778-P3",
                        work_date=requirement.start_date,
                        hours=Decimal("8.00"),
                        shift_snapshot_text="{}",
                        actor_user_id=TEST_ADMIN_USER_ID,
                        command_id="release-placement-partial-778",
                        active=True,
                    )
                    session.add(release)
                    session.flush()

                    allocation = SqlAllocationCommandAdapter(session)
                    allocation.create_manual(
                        requirement.id,
                        "R-778-P3",
                        requirement.start_date,
                        Decimal("4.00"),
                    )
                    self.assertTrue(release.active)
                    segment = SqlPlannerQueryRepositoryWithLoadProfiles(
                        session
                    ).get_segment(requirement.id)
                    self.assertIsNotNone(segment)
                    self.assertEqual(segment.covered_hours, 4.0)
                    self.assertEqual(segment.remaining_hours, 4.0)
            finally:
                engine.dispose()

    def test_same_resource_date_release_fragments_resolve_independently(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-release-fragments",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    requirement.assigned_resource_id = None
                    intent.intent_mode = "PRESERVE_EXISTING"
                    session.query(Shift).filter_by(
                        resource_requirement_id=requirement.id
                    ).delete(synchronize_session=False)
                    session.add_all([
                        PlanningWorkQueueRelease(
                            id="REL-FRAG-A-778",
                            planning_intent_id=intent.id,
                            resource_requirement_id=requirement.id,
                            original_shift_id="SHIFT-FRAG-A-778",
                            workforce_request_id=request.id,
                            approval_revision_id=requirement.approval_revision_id,
                            approved_entry_key=intent.approved_entry_key,
                            resource_id="R-778-P3",
                            work_date=requirement.start_date,
                            hours=Decimal("4.00"),
                            shift_snapshot_text="{}",
                            actor_user_id=TEST_ADMIN_USER_ID,
                            command_id="release-frag-a-778",
                            active=True,
                        ),
                        PlanningWorkQueueRelease(
                            id="REL-FRAG-B-778",
                            planning_intent_id=intent.id,
                            resource_requirement_id=requirement.id,
                            original_shift_id="SHIFT-FRAG-B-778",
                            workforce_request_id=request.id,
                            approval_revision_id=requirement.approval_revision_id,
                            approved_entry_key=intent.approved_entry_key,
                            resource_id="R-778-P3",
                            work_date=requirement.start_date,
                            hours=Decimal("4.00"),
                            shift_snapshot_text="{}",
                            actor_user_id=TEST_ADMIN_USER_ID,
                            command_id="release-frag-b-778",
                            active=True,
                        ),
                    ])
                    session.flush()
                    allocation_adapter = SqlAllocationCommandAdapter(session)
                    executor = IdempotentCommandExecutor(
                        SqlCommandIdempotencyAdapter(
                            session,
                            actor_name="admin-778-release-fragments",
                        )
                    )
                    payload = {
                        "segment_id": requirement.id,
                        "resource_id": "R-778-P3",
                        "day": requirement.start_date.isoformat(),
                        "hours": 4.0,
                    }
                    first = executor.execute(
                        scope="manual_allocation.create",
                        key="restore-fragment-a-778",
                        request_payload=payload,
                        action=lambda: {
                            "allocation_id": allocation_adapter.create_manual(
                                requirement.id,
                                "R-778-P3",
                                requirement.start_date,
                                Decimal("4.00"),
                            )
                        },
                    )
                    releases = session.scalars(
                        select(PlanningWorkQueueRelease)
                        .where(
                            PlanningWorkQueueRelease.planning_intent_id == intent.id
                        )
                        .order_by(PlanningWorkQueueRelease.id)
                    ).all()
                    self.assertEqual(sum(1 for row in releases if not row.active), 1)
                    self.assertEqual(sum(1 for row in releases if row.active), 1)
                    segment = SqlPlannerQueryRepositoryWithLoadProfiles(session).get_segment(
                        requirement.id
                    )
                    self.assertIsNotNone(segment)
                    self.assertEqual(segment.covered_hours, 4.0)
                    self.assertEqual(segment.remaining_hours, 4.0)

                    active_before = {row.id for row in releases if row.active}
                    replay = executor.execute(
                        scope="manual_allocation.create",
                        key="restore-fragment-a-778",
                        request_payload=payload,
                        action=lambda: {
                            "allocation_id": allocation_adapter.create_manual(
                                requirement.id,
                                "R-778-P3",
                                requirement.start_date,
                                Decimal("4.00"),
                            )
                        },
                    )
                    self.assertEqual(replay, first)
                    active_after = {
                        row.id
                        for row in session.scalars(
                            select(PlanningWorkQueueRelease).where(
                                PlanningWorkQueueRelease.planning_intent_id == intent.id,
                                PlanningWorkQueueRelease.active.is_(True),
                            )
                        ).all()
                    }
                    self.assertEqual(active_after, active_before)
                    counted = session.scalars(
                        select(Shift).where(
                            Shift.resource_requirement_id == requirement.id,
                            Shift.allocation_type != MISSING_ALLOCATION_TYPE,
                        )
                    ).all()
                    self.assertEqual(
                        sum((row.hours for row in counted), Decimal("0.00")),
                        Decimal("4.00"),
                    )
            finally:
                engine.dispose()

    def test_unrelated_preexisting_coverage_does_not_resolve_release(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-unrelated-coverage",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 12)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request, requirement, intent = self._context(session, number)
                    intent.intent_mode = "PRESERVE_EXISTING"
                    intent.target_mode = "EXPLICIT_RESOURCE"
                    intent.target_resource_id = "R-778-P3"
                    session.query(Shift).filter_by(
                        resource_requirement_id=requirement.id
                    ).delete(synchronize_session=False)

                    preexisting = Shift(
                        id="SHIFT-UNRELATED-PREEXISTING-778",
                        legacy_allocation_id="ALLOC-UNRELATED-PREEXISTING-778",
                        resource_requirement_id=requirement.id,
                        resource_id="R-778-P3",
                        work_date=requirement.start_date,
                        hours=Decimal("8.00"),
                        source="MANUAL",
                        locked=True,
                    )
                    release = PlanningWorkQueueRelease(
                        id="REL-UNRELATED-COVERAGE-778",
                        planning_intent_id=intent.id,
                        resource_requirement_id=requirement.id,
                        original_shift_id="SHIFT-RETURNED-UNRELATED-778",
                        workforce_request_id=request.id,
                        approval_revision_id=requirement.approval_revision_id,
                        approved_entry_key=intent.approved_entry_key,
                        resource_id="R-778-P3",
                        work_date=requirement.start_date,
                        hours=Decimal("8.00"),
                        shift_snapshot_text="{}",
                        actor_user_id=TEST_ADMIN_USER_ID,
                        command_id="release-unrelated-coverage-778",
                        active=True,
                    )
                    session.add_all((preexisting, release))
                    session.flush()

                    allocation = SqlAllocationCommandAdapter(session)
                    allocation.create_manual(
                        requirement.id,
                        "R-778-P3",
                        requirement.start_date,
                        Decimal("4.00"),
                    )

                    self.assertTrue(release.active)
                    restorations = session.scalars(
                        select(PlanningWorkQueueRestoration).where(
                            PlanningWorkQueueRestoration.release_id == release.id
                        )
                    ).all()
                    self.assertEqual(len(restorations), 1)
                    self.assertEqual(restorations[0].restored_hours, Decimal("4.00"))
                    self.assertNotEqual(
                        restorations[0].shift_id,
                        preexisting.id,
                    )
            finally:
                engine.dispose()

    def test_queued_work_can_be_explicitly_placed_and_remains_durable(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="admin-778-p3-place",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number = self._approve(client, 8)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    _request, requirement, intent = self._context(session, number)
                    intent.intent_mode = "QUEUED"
                    intent.target_mode = "EXPLICIT_NONE"
                    intent.target_resource_id = None
                    requirement.assigned_resource_id = None
                    session.query(Shift).filter_by(
                        resource_requirement_id=requirement.id
                    ).delete(synchronize_session=False)
                    session.flush()

                    SqlAllocationCommandAdapter(session).assign_segment(
                        requirement.id,
                        "R-778-P3",
                    )
                    shifts = session.scalars(
                        select(Shift).where(
                            Shift.resource_requirement_id == requirement.id
                        )
                    ).all()
                    self.assertTrue(shifts)
                    self.assertEqual(
                        sum((row.hours for row in shifts), Decimal("0")),
                        Decimal("8.00"),
                    )
                    self.assertEqual({row.resource_id for row in shifts}, {"R-778-P3"})
                    self.assertEqual(intent.intent_mode, "PRESERVE_EXISTING")
                    self.assertEqual(intent.target_mode, "EXPLICIT_RESOURCE")
                    self.assertEqual(intent.target_resource_id, "R-778-P3")

                    shift_ids = {row.id for row in shifts}
                    SqlPlanningCommandAdapter(session).rebuild()
                    reloaded = session.scalars(
                        select(Shift).where(
                            Shift.resource_requirement_id == requirement.id
                        )
                    ).all()
                    self.assertEqual({row.id for row in reloaded}, shift_ids)
            finally:
                engine.dispose()



if __name__ == "__main__":
    unittest.main()
