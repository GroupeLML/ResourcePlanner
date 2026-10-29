from __future__ import annotations

from datetime import time
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from app.application.errors import ApplicationConflictError
from sqlalchemy import select

from app.infrastructure.sql import (
    RequestCompletionReduction,
    RequestLine,
    Resource,
    ResourceAvailabilityRule,
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
                    self.assertEqual(competing_shifts, [])
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
