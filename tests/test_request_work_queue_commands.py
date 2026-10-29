from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from app.application.errors import ApplicationAuthorizationError, ApplicationConflictError
from app.application.idempotency import IdempotentCommandExecutor
from app.application.request_work_queue import (
    PermanentlyReduceShiftHoursCommand,
    ReturnSegmentToWorkQueueCommand,
    ReturnShiftToWorkQueueCommand,
)
from app.infrastructure.sql.approval_cycle_models import (
    ApprovalRequirement,
    ApprovalRequirementApprover,
    RequestApprovalCycle,
)
from app.infrastructure.sql.approval_scope_models import ApprovalScope, ApprovalScopeApprover
from app.infrastructure.sql.approval_revision_models import (
    RequestApprovalReference,
    RequestApprovalRevision,
)
from app.infrastructure.sql.asset_models import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
)
from app.infrastructure.sql.base import Base
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.idempotency import SqlCommandIdempotencyAdapter
from app.infrastructure.sql.command_adapters import SqlAllocationCommandAdapter
from app.infrastructure.sql.models import (
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
)
from app.infrastructure.sql.operational_choice_models import (
    PLANNING_INTENT_AUTO,
    PLANNING_INTENT_PRESERVE_EXISTING,
    PLANNING_INTENT_QUEUED,
    PLANNING_TARGET_EXPLICIT_NONE,
    PLANNING_TARGET_INHERIT,
    PlanningWorkQueueRelease,
    RequestCompletionReduction,
    RequestOperationalState,
    RequestRequirementPlanningIntent,
)
from app.infrastructure.sql.planning_audit import SqlPlanningAuditJournal
from app.infrastructure.sql.planning_version import (
    PlanningMutationState,
    SqlPlanningMutationVersionRepository,
)
from app.infrastructure.sql.request_work_queue_commands import (
    SqlRequestWorkQueueCommandAdapter,
)
from app.infrastructure.sql.session import create_session_factory, create_sql_engine, transactional_session


DAY = date(2026, 10, 26)
ENTRY_KEY = "ENTRY-778"


class _PlanningStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def rebuild(self, *, preserve_shift_ids=()):
        self.calls.append(tuple(preserve_shift_ids))
        return {
            "segments": 1,
            "allocations": len(preserve_shift_ids),
            "locked_allocations": 0,
            "requested_hours": 40,
            "allocated_hours": 0,
            "unallocated_hours": 40,
            "planning_engine": "test",
        }


class RequestWorkQueueCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        approved_payload = json.dumps(
            {
                "authorization": {
                    "entries": [
                        {
                            "identity": ENTRY_KEY,
                            "hours": "40.00",
                            "slot_count": 1,
                            "start_date": DAY.isoformat(),
                            "end_date": (DAY + timedelta(days=4)).isoformat(),
                        }
                    ]
                }
            },
            separators=(",", ":"),
        )
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet 778"))
            session.add(ApprovalScope(id="SCOPE1", code="SCOPE1", label="Scope 778", active=True, version=1))
            session.add(Resource(id="R1", name="Alice", active=True, erp_active=True))
            session.add(
                AppUser(
                    id="U1",
                    issuer="test",
                    subject="u1",
                    display_name="Planificateur approbateur",
                    roles_json='["ADMIN"]',
                    active=True,
                )
            )
            session.flush()
            session.add(ApprovalScopeApprover(approval_scope_id="SCOPE1", app_user_id="U1"))
            session.add(
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DMO-778",
                    project_id="P1",
                    status="En planification",
                    aggregate_version=1,
                    line_mode=True,
                    desired_start=DAY,
                    desired_end=DAY + timedelta(days=4),
                    estimated_hours=Decimal("40"),
                )
            )
            session.flush()
            session.add(
                RequestLine(
                    id="L1",
                    workforce_request_id="D1",
                    position=0,
                    kind="WORKFORCE",
                    slot_count=1,
                    desired_start=DAY,
                    desired_end=DAY + timedelta(days=4),
                    estimated_hours=Decimal("40"),
                    active=True,
                )
            )
            session.add(
                RequestApprovalRevision(
                    id="REV1",
                    workforce_request_id="D1",
                    request_version=1,
                    approved_at=datetime.now(timezone.utc),
                    payload_text=approved_payload,
                    authorization_fingerprint="a" * 64,
                )
            )
            session.flush()
            session.add(
                RequestApprovalReference(
                    workforce_request_id="D1",
                    active_revision_id="REV1",
                    status="CAPTURED",
                )
            )
            session.add(
                RequestOperationalState(
                    workforce_request_id="D1",
                    approval_revision_id="REV1",
                    version=1,
                    selections_text="{}",
                    confirmations_text="{}",
                    budget_overrides_text="{}",
                )
            )
            session.add(
                ResourceRequirement(
                    id="REQ1",
                    legacy_segment_id="SEG-778",
                    project_id="P1",
                    workforce_request_id="D1",
                    source_request_line_id="L1",
                    approval_revision_id="REV1",
                    approved_entry_key=ENTRY_KEY,
                    approval_reference_status="CAPTURED",
                    start_date=DAY,
                    end_date=DAY + timedelta(days=4),
                    planned_hours=Decimal("40"),
                    status="Planifié",
                    origin="REQUEST",
                    assigned_resource_id="R1",
                )
            )
            session.flush()
            session.add(
                RequestRequirementPlanningIntent(
                    id="INT1",
                    slot_identity_key='["D1","L1","ENTRY-778","",0]',
                    workforce_request_id="D1",
                    source_request_line_id="L1",
                    approval_revision_id="REV1",
                    approved_entry_key=ENTRY_KEY,
                    slot_index=0,
                    current_resource_requirement_id="REQ1",
                    intent_mode=PLANNING_INTENT_AUTO,
                    target_mode=PLANNING_TARGET_INHERIT,
                )
            )
            for index in range(5):
                session.add(
                    Shift(
                        id=f"S{index + 1}",
                        legacy_allocation_id=f"A{index + 1}",
                        resource_requirement_id="REQ1",
                        resource_id="R1",
                        work_date=DAY + timedelta(days=index),
                        hours=Decimal("8"),
                        source="AUTO",
                        locked=False,
                    )
                )
            session.add(
                RequestApprovalCycle(
                    id="C1",
                    workforce_request_id="D1",
                    submitted_request_version=1,
                    state="COMPLETED",
                    subject_fingerprint="b" * 64,
                    initialization_reason="SUBMISSION",
                    submitted_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                    approved_revision_id="REV1",
                )
            )
            session.flush()
            session.add(
                ApprovalRequirement(
                    id="AR1",
                    approval_cycle_id="C1",
                    request_line_id="L1",
                    approval_scope_id="SCOPE1",
                    routing_sources_text="[]",
                )
            )
            session.flush()
            session.add(
                ApprovalRequirementApprover(
                    requirement_id="AR1",
                    app_user_id="U1",
                    sources_text='["TEST"]',
                )
            )
            session.add(PlanningMutationState(id="GLOBAL", version=1))

    def tearDown(self) -> None:
        self.engine.dispose()

    def _adapter(self, session, *, permissions=None):
        planning = _PlanningStub()
        adapter = SqlRequestWorkQueueCommandAdapter(
            session,
            planning=planning,
            versioning=SqlPlanningMutationVersionRepository(session),
            journal=SqlPlanningAuditJournal(session, actor_name="Test"),
            actor_user_id="U1",
            actor_name="Test",
            permissions=permissions
            or ("manage_planning", "read_demands", "approve_demands", "read"),
            roles=("ADMIN",),
        )
        return adapter, planning

    def test_return_shift_persists_release_and_preserves_nominal_need(self) -> None:
        with transactional_session(self.factory) as session:
            adapter, planning = self._adapter(session)
            result = adapter.return_shift(
                ReturnShiftToWorkQueueCommand(
                    allocation_id="A4",
                    expected_planning_version=1,
                    expected_approval_revision_id="REV1",
                    expected_operational_version=1,
                    command_id="return-shift-778",
                )
            )
            self.assertIsNone(session.get(Shift, "S4"))
            self.assertEqual(
                session.get(ResourceRequirement, "REQ1").planned_hours,
                Decimal("40"),
            )
            intent = session.get(RequestRequirementPlanningIntent, "INT1")
            self.assertEqual(intent.intent_mode, PLANNING_INTENT_PRESERVE_EXISTING)
            release = session.query(PlanningWorkQueueRelease).one()
            self.assertEqual(release.original_shift_id, "S4")
            self.assertEqual(release.resource_id, "R1")
            self.assertEqual(release.hours, Decimal("8"))
            self.assertEqual(set(planning.calls[-1]), {"S1", "S2", "S3", "S5"})
            self.assertEqual(result["effective_need_after"], 40.0)
            self.assertEqual(result["covered_hours_after"], 32.0)
            self.assertEqual(result["remaining_hours_after"], 8.0)

    def test_return_segment_queues_entire_requirement_and_clears_target(self) -> None:
        with transactional_session(self.factory) as session:
            adapter, planning = self._adapter(session)
            result = adapter.return_segment(
                ReturnSegmentToWorkQueueCommand(
                    segment_id="SEG-778",
                    expected_planning_version=1,
                    expected_approval_revision_id="REV1",
                    expected_operational_version=1,
                    command_id="return-segment-778",
                )
            )
            self.assertEqual(
                session.query(Shift).filter_by(resource_requirement_id="REQ1").count(),
                0,
            )
            requirement = session.get(ResourceRequirement, "REQ1")
            self.assertEqual(requirement.planned_hours, Decimal("40"))
            self.assertIsNone(requirement.assigned_resource_id)
            intent = session.get(RequestRequirementPlanningIntent, "INT1")
            self.assertEqual(intent.intent_mode, PLANNING_INTENT_QUEUED)
            self.assertEqual(intent.target_mode, PLANNING_TARGET_EXPLICIT_NONE)
            self.assertEqual(planning.calls[-1], ())
            self.assertEqual(result["removed_shift_count"], 5)
            self.assertEqual(result["remaining_hours_after"], 40.0)

    def test_return_segment_clears_existing_overcoverage_instead_of_blocking(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                Shift(
                    id="S6",
                    legacy_allocation_id="A6",
                    resource_requirement_id="REQ1",
                    resource_id="R1",
                    work_date=DAY,
                    hours=Decimal("8"),
                    source="MANUAL",
                    locked=True,
                )
            )
        with transactional_session(self.factory) as session:
            adapter, _planning = self._adapter(session)
            result = adapter.return_segment(
                ReturnSegmentToWorkQueueCommand(
                    segment_id="SEG-778",
                    expected_planning_version=1,
                    expected_approval_revision_id="REV1",
                    expected_operational_version=1,
                    command_id="return-overcovered-segment-778",
                )
            )
            self.assertEqual(
                session.query(Shift).filter_by(resource_requirement_id="REQ1").count(),
                0,
            )
            self.assertEqual(result["removed_shift_count"], 6)
            self.assertEqual(result["removed_hours"], 48.0)
            self.assertEqual(result["remaining_hours_after"], 40.0)

    def test_permanent_reduction_appends_once_and_advances_request_cas(self) -> None:
        with transactional_session(self.factory) as session:
            adapter, planning = self._adapter(session)
            result = adapter.permanently_reduce_shift(
                PermanentlyReduceShiftHoursCommand(
                    allocation_id="A4",
                    expected_planning_version=1,
                    expected_request_version=1,
                    expected_approval_revision_id="REV1",
                    expected_operational_version=1,
                    command_id="reduce-shift-778",
                )
            )
            self.assertIsNone(session.get(Shift, "S4"))
            requirement = session.get(ResourceRequirement, "REQ1")
            self.assertEqual(requirement.planned_hours, Decimal("32"))
            request = session.get(WorkforceRequest, "D1")
            self.assertEqual(request.aggregate_version, 2)
            reduction = session.query(RequestCompletionReduction).one()
            self.assertEqual(reduction.reduction_hours, Decimal("8"))
            self.assertEqual(reduction.effective_need_before, Decimal("40"))
            self.assertEqual(reduction.effective_need_after, Decimal("32"))
            self.assertEqual(
                session.get(RequestApprovalRevision, "REV1").payload_text,
                json.dumps(
                    {
                        "authorization": {
                            "entries": [
                                {
                                    "identity": ENTRY_KEY,
                                    "hours": "40.00",
                                    "slot_count": 1,
                                    "start_date": DAY.isoformat(),
                                    "end_date": (DAY + timedelta(days=4)).isoformat(),
                                }
                            ]
                        }
                    },
                    separators=(",", ":"),
                ),
            )
            self.assertEqual(set(planning.calls[-1]), {"S1", "S2", "S3", "S5"})
            self.assertEqual(result["request_version"], 2)
            self.assertEqual(result["effective_need_after"], 32.0)
            self.assertEqual(result["covered_hours_after"], 32.0)
            self.assertEqual(result["remaining_hours_after"], 0.0)

    def test_pending_candidate_cycle_does_not_replace_active_reduction_authority(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                RequestApprovalCycle(
                    id="C2",
                    workforce_request_id="D1",
                    submitted_request_version=2,
                    state="OPEN",
                    subject_fingerprint="c" * 64,
                    initialization_reason="RESUBMISSION",
                    submitted_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                )
            )
            session.flush()
            session.add(
                ApprovalRequirement(
                    id="AR2",
                    approval_cycle_id="C2",
                    request_line_id="L1",
                    routing_sources_text="[]",
                )
            )
        with transactional_session(self.factory) as session:
            adapter, _planning = self._adapter(session)
            result = adapter.permanently_reduce_shift(
                PermanentlyReduceShiftHoursCommand(
                    allocation_id="A4",
                    expected_planning_version=1,
                    expected_request_version=1,
                    expected_approval_revision_id="REV1",
                    expected_operational_version=1,
                    command_id="reduce-with-pending-candidate-778",
                )
            )
            self.assertEqual(result["effective_need_after"], 32.0)
            self.assertIsNone(session.get(Shift, "S4"))
            self.assertEqual(session.query(RequestCompletionReduction).count(), 1)
            candidate = session.get(RequestApprovalCycle, "C2")
            self.assertEqual(candidate.state, "INVALIDATED")
            self.assertEqual(candidate.invalidation_reason, "POST_APPROVAL_REDUCTION")

    def test_same_stable_actor_key_replays_after_shift_is_gone(self) -> None:
        payload = {
            "operation": "RETURN_SHIFT_TO_WORK_QUEUE",
            "allocation_id": "A4",
            "expected_planning_version": 1,
            "expected_approval_revision_id": "REV1",
            "expected_operational_version": 1,
        }
        with transactional_session(self.factory) as session:
            adapter, _planning = self._adapter(session)
            executor = IdempotentCommandExecutor(
                SqlCommandIdempotencyAdapter(session, actor_name="U1")
            )
            first = executor.execute(
                scope="request_work_queue.return_shift",
                key="replay-return-778",
                request_payload=payload,
                action=lambda: dict(
                    adapter.return_shift(
                        ReturnShiftToWorkQueueCommand(
                            allocation_id="A4",
                            expected_planning_version=1,
                            expected_approval_revision_id="REV1",
                            expected_operational_version=1,
                            command_id="replay-return-778",
                        )
                    )
                ),
            )
            self.assertIsNone(session.get(Shift, "S4"))
            self.assertEqual(session.query(PlanningWorkQueueRelease).count(), 1)

        with transactional_session(self.factory) as session:
            adapter, _planning = self._adapter(session)
            executor = IdempotentCommandExecutor(
                SqlCommandIdempotencyAdapter(session, actor_name="U1")
            )
            replay = executor.execute(
                scope="request_work_queue.return_shift",
                key="replay-return-778",
                request_payload=payload,
                action=lambda: dict(
                    adapter.return_shift(
                        ReturnShiftToWorkQueueCommand(
                            allocation_id="A4",
                            expected_planning_version=1,
                            expected_approval_revision_id="REV1",
                            expected_operational_version=1,
                            command_id="replay-return-778",
                        )
                    )
                ),
            )
            self.assertEqual(replay, first)
            self.assertEqual(session.query(PlanningWorkQueueRelease).count(), 1)

    def test_permanent_reduction_to_zero_marks_completion_not_cancellation(self) -> None:
        with transactional_session(self.factory) as session:
            revision = session.get(RequestApprovalRevision, "REV1")
            revision.payload_text = json.dumps(
                {
                    "authorization": {
                        "entries": [
                            {
                                "identity": ENTRY_KEY,
                                "hours": "8.00",
                                "slot_count": 1,
                                "start_date": DAY.isoformat(),
                                "end_date": DAY.isoformat(),
                            }
                        ]
                    }
                },
                separators=(",", ":"),
            )
            requirement = session.get(ResourceRequirement, "REQ1")
            requirement.planned_hours = Decimal("8.00")
            requirement.end_date = DAY
            session.query(Shift).filter(
                Shift.id.in_(("S2", "S3", "S4", "S5"))
            ).delete(synchronize_session=False)

        with transactional_session(self.factory) as session:
            adapter, _planning = self._adapter(session)
            result = adapter.permanently_reduce_shift(
                PermanentlyReduceShiftHoursCommand(
                    allocation_id="A1",
                    expected_planning_version=1,
                    expected_request_version=1,
                    expected_approval_revision_id="REV1",
                    expected_operational_version=1,
                    command_id="reduce-to-zero-778",
                )
            )
            requirement = session.get(ResourceRequirement, "REQ1")
            self.assertEqual(result["effective_need_after"], 0.0)
            self.assertEqual(requirement.planned_hours, Decimal("8.00"))
            self.assertEqual(requirement.status, "Terminé")
            self.assertIsNone(session.get(Shift, "S1"))

    def test_stale_planning_version_rolls_back_without_release(self) -> None:
        with self.assertRaises(ApplicationConflictError):
            with transactional_session(self.factory) as session:
                adapter, _planning = self._adapter(session)
                adapter.return_shift(
                    ReturnShiftToWorkQueueCommand(
                        allocation_id="A4",
                        expected_planning_version=99,
                        expected_approval_revision_id="REV1",
                        expected_operational_version=1,
                        command_id="stale-778",
                    )
                )
        with transactional_session(self.factory) as session:
            self.assertIsNotNone(session.get(Shift, "S4"))
            self.assertEqual(session.query(PlanningWorkQueueRelease).count(), 0)

    def test_stale_operational_version_rolls_back_without_mutation(self) -> None:
        with self.assertRaises(ApplicationConflictError):
            with transactional_session(self.factory) as session:
                adapter, _planning = self._adapter(session)
                adapter.return_shift(
                    ReturnShiftToWorkQueueCommand(
                        allocation_id="A4",
                        expected_planning_version=1,
                        expected_approval_revision_id="REV1",
                        expected_operational_version=2,
                        command_id="stale-operational-778",
                    )
                )
        with transactional_session(self.factory) as session:
            self.assertIsNotNone(session.get(Shift, "S4"))
            self.assertEqual(session.query(PlanningWorkQueueRelease).count(), 0)
            self.assertEqual(
                session.get(PlanningMutationState, "GLOBAL").version,
                1,
            )

    def test_legacy_generic_release_and_delete_fail_closed_for_request_shift(self) -> None:
        for operation in ("release", "delete"):
            with self.subTest(operation=operation):
                with self.assertRaises(ApplicationConflictError) as raised:
                    with transactional_session(self.factory) as session:
                        versioning = SqlPlanningMutationVersionRepository(session)
                        adapter = SqlAllocationCommandAdapter(
                            session,
                            planning=_PlanningStub(),
                            versioning=versioning,
                        )
                        if operation == "release":
                            adapter.release_manual("A4")
                        else:
                            adapter.delete_manual("A4")
                self.assertEqual(
                    raised.exception.code,
                    "request_shift_action_requires_explicit_command",
                )
                with transactional_session(self.factory) as session:
                    self.assertIsNotNone(session.get(Shift, "S4"))
                    self.assertEqual(
                        session.get(PlanningMutationState, "GLOBAL").version,
                        1,
                    )

    def test_revoked_current_approver_is_denied_even_if_historical_cycle_contains_actor(self) -> None:
        with transactional_session(self.factory) as session:
            membership = session.get(ApprovalScopeApprover, ("SCOPE1", "U1"))
            self.assertIsNotNone(membership)
            session.delete(membership)

        with self.assertRaises(ApplicationAuthorizationError) as raised:
            with transactional_session(self.factory) as session:
                adapter, _planning = self._adapter(session)
                adapter.permanently_reduce_shift(
                    PermanentlyReduceShiftHoursCommand(
                        allocation_id="A4",
                        expected_planning_version=1,
                        expected_request_version=1,
                        expected_approval_revision_id="REV1",
                        expected_operational_version=1,
                        command_id="revoked-current-approver-778",
                    )
                )
        self.assertEqual(
            raised.exception.code,
            "reduction_approval_authority_denied",
        )
        with transactional_session(self.factory) as session:
            self.assertIsNotNone(session.get(Shift, "S4"))
            self.assertEqual(session.query(RequestCompletionReduction).count(), 0)

    def test_terminal_return_rejects_before_any_shift_is_deleted(self) -> None:
        with transactional_session(self.factory) as session:
            session.get(ResourceRequirement, "REQ1").status = "Terminé"

        with self.assertRaises(ApplicationConflictError) as raised:
            with transactional_session(self.factory) as session:
                adapter, _planning = self._adapter(session)
                adapter.return_shift(
                    ReturnShiftToWorkQueueCommand(
                        allocation_id="A4",
                        expected_planning_version=1,
                        expected_approval_revision_id="REV1",
                        expected_operational_version=1,
                        command_id="terminal-return-778",
                    )
                )
        self.assertEqual(
            raised.exception.code,
            "request_work_queue_terminal_requirement",
        )
        with transactional_session(self.factory) as session:
            shifts = session.query(Shift).filter_by(resource_requirement_id="REQ1").all()
            self.assertEqual(len(shifts), 5)
            self.assertEqual(session.query(PlanningWorkQueueRelease).count(), 0)

    def test_return_shift_deletes_only_shift_owned_asset_state_across_multi_resource_survivors(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(Resource(id="R2", name="Bob", active=True, erp_active=True))
            session.add(
                AssetType(
                    id="AT-778",
                    code="AT-778",
                    label="Type 778",
                    category="EQUIPMENT",
                )
            )
            session.flush()
            session.add_all(
                [
                    Asset(id="AS-OWN-778", code="AS-OWN-778", label="Owned", asset_type_id="AT-778"),
                    Asset(id="AS-PROJ-778", code="AS-PROJ-778", label="Project", asset_type_id="AT-778"),
                    Asset(id="AS-SEG-778", code="AS-SEG-778", label="Segment", asset_type_id="AT-778"),
                ]
            )
            session.get(Shift, "S5").resource_id = "R2"
            session.add_all(
                [
                    AssetRequirement(
                        id="AR-OWN-778",
                        project_id="P1",
                        origin="SHIFT_AD_HOC",
                        shift_id="S4",
                        asset_type_id="AT-778",
                        start_date=DAY + timedelta(days=3),
                        end_date=DAY + timedelta(days=3),
                    ),
                    AssetRequirement(
                        id="AR-PROJ-778",
                        project_id="P1",
                        origin="PROJECT_DIRECT",
                        asset_type_id="AT-778",
                        start_date=DAY,
                        end_date=DAY + timedelta(days=4),
                    ),
                    AssetRequirement(
                        id="AR-SEG-778",
                        project_id="P1",
                        origin="SEGMENT",
                        resource_requirement_id="REQ1",
                        asset_type_id="AT-778",
                        start_date=DAY,
                        end_date=DAY + timedelta(days=4),
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    AssetAllocation(
                        id="AA-OWN-778",
                        asset_requirement_id="AR-OWN-778",
                        asset_id="AS-OWN-778",
                        operator_resource_id="R1",
                        start_date=DAY + timedelta(days=3),
                        end_date=DAY + timedelta(days=3),
                        locked=True,
                    ),
                    AssetAllocation(
                        id="AA-PROJ-778",
                        asset_requirement_id="AR-PROJ-778",
                        asset_id="AS-PROJ-778",
                        operator_resource_id="R2",
                        start_date=DAY,
                        end_date=DAY + timedelta(days=4),
                        locked=True,
                    ),
                    AssetAllocation(
                        id="AA-SEG-778",
                        asset_requirement_id="AR-SEG-778",
                        asset_id="AS-SEG-778",
                        operator_resource_id="R1",
                        start_date=DAY,
                        end_date=DAY + timedelta(days=4),
                        locked=True,
                    ),
                ]
            )

        with transactional_session(self.factory) as session:
            adapter, _planning = self._adapter(session)
            adapter.return_shift(
                ReturnShiftToWorkQueueCommand(
                    allocation_id="A4",
                    expected_planning_version=1,
                    expected_approval_revision_id="REV1",
                    expected_operational_version=1,
                    command_id="asset-owned-return-778",
                )
            )
            self.assertIsNone(session.get(Shift, "S4"))
            self.assertIsNone(session.get(AssetRequirement, "AR-OWN-778"))
            self.assertIsNone(session.get(AssetAllocation, "AA-OWN-778"))
            self.assertIsNotNone(session.get(AssetRequirement, "AR-PROJ-778"))
            self.assertIsNotNone(session.get(AssetAllocation, "AA-PROJ-778"))
            self.assertIsNotNone(session.get(AssetRequirement, "AR-SEG-778"))
            self.assertIsNotNone(session.get(AssetAllocation, "AA-SEG-778"))
            survivor = session.get(Shift, "S5")
            self.assertIsNotNone(survivor)
            self.assertEqual(survivor.resource_id, "R2")

    def test_terminal_rejection_rolls_back_shift_owned_asset_cleanup(self) -> None:
        with transactional_session(self.factory) as session:
            session.add(
                AssetType(
                    id="AT-ROLLBACK-778",
                    code="AT-ROLLBACK-778",
                    label="Type rollback 778",
                    category="EQUIPMENT",
                )
            )
            session.flush()
            session.add(
                Asset(
                    id="AS-ROLLBACK-778",
                    code="AS-ROLLBACK-778",
                    label="Rollback",
                    asset_type_id="AT-ROLLBACK-778",
                )
            )
            session.add(
                AssetRequirement(
                    id="AR-ROLLBACK-778",
                    project_id="P1",
                    origin="SHIFT_AD_HOC",
                    shift_id="S4",
                    asset_type_id="AT-ROLLBACK-778",
                    start_date=DAY + timedelta(days=3),
                    end_date=DAY + timedelta(days=3),
                )
            )
            session.flush()
            session.add(
                AssetAllocation(
                    id="AA-ROLLBACK-778",
                    asset_requirement_id="AR-ROLLBACK-778",
                    asset_id="AS-ROLLBACK-778",
                    operator_resource_id="R1",
                    start_date=DAY + timedelta(days=3),
                    end_date=DAY + timedelta(days=3),
                    locked=True,
                )
            )
            session.get(ResourceRequirement, "REQ1").status = "Terminé"

        with self.assertRaises(ApplicationConflictError):
            with transactional_session(self.factory) as session:
                adapter, _planning = self._adapter(session)
                adapter.return_shift(
                    ReturnShiftToWorkQueueCommand(
                        allocation_id="A4",
                        expected_planning_version=1,
                        expected_approval_revision_id="REV1",
                        expected_operational_version=1,
                        command_id="asset-terminal-rollback-778",
                    )
                )
        with transactional_session(self.factory) as session:
            self.assertIsNotNone(session.get(Shift, "S4"))
            self.assertIsNotNone(session.get(AssetRequirement, "AR-ROLLBACK-778"))
            self.assertIsNotNone(session.get(AssetAllocation, "AA-ROLLBACK-778"))
            self.assertEqual(session.query(PlanningWorkQueueRelease).count(), 0)

    def test_permanent_reduction_requires_enhanced_permissions(self) -> None:
        with self.assertRaises(ApplicationAuthorizationError):
            with transactional_session(self.factory) as session:
                adapter, _planning = self._adapter(
                    session,
                    permissions=("manage_planning",),
                )
                adapter.permanently_reduce_shift(
                    PermanentlyReduceShiftHoursCommand(
                        allocation_id="A4",
                        expected_planning_version=1,
                        expected_request_version=1,
                        expected_approval_revision_id="REV1",
                        expected_operational_version=1,
                        command_id="permission-778",
                    )
                )
        with transactional_session(self.factory) as session:
            self.assertIsNotNone(session.get(Shift, "S4"))
            self.assertEqual(session.query(RequestCompletionReduction).count(), 0)


if __name__ == "__main__":
    unittest.main()
