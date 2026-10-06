from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import func, select

from app.application import (
    AllocationDropEvaluateCommand,
    AllocationWindowOverrideMoveCommand,
    ApplicationAuthorizationError,
    PlanningWindowOverrideExtendCommand,
)
from app.application.security import (
    PERMISSION_MANAGE_PLANNING,
    ROLE_ADMIN,
    permissions_for_roles,
)
from app.domain.approval_envelope import EnvelopeEntryIdentity
from app.infrastructure.sql import (
    Base,
    PLANNING_WINDOW_OVERRIDE_ACTIVE,
    PLANNING_WINDOW_OVERRIDE_SUPERSEDED,
    PlanningChangeHistory,
    PlanningWindowOverride,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from app.infrastructure.sql.base import utc_now
from app.infrastructure.sql.composite_allocation import (
    SqlCompositeAllocationCommandAdapter,
)
from app.infrastructure.sql.demand_repository import SqlDemandRepository
from app.infrastructure.sql.idempotency import CommandIdempotencyReceipt
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.planning_audit import SqlPlanningAuditJournal
from app.infrastructure.sql.planning_authorization_repository import (
    SqlRequestPlanningAuthorizationRepository,
)
from app.infrastructure.sql.planning_version import (
    SqlPlanningMutationVersionRepository,
)


DAY = date(2026, 10, 6)
NEXT_DAY = DAY + timedelta(days=1)
PROJECT_ID = "P-613C"
REQUEST_ID = "DMO-613C"
REQUEST_NUMBER = "DMO-2026-613C"
REVISION_ID = "REV-613C"
REQUIREMENT_ID = "REQ-613C"
SEGMENT_ID = "SEG-613C"
SHIFT_ID = "SHIFT-613C"
ALLOCATION_ID = "ALLOC-613C"
RESOURCE_A = "R-613C-A"
RESOURCE_B = "R-613C-B"
ADMIN_ID = "ADMIN-613C"
ENTRY_KEY = EnvelopeEntryIdentity(line_id="LINE-613C").stable_key


def _snapshot_payload() -> str:
    return json.dumps(
        {
            "authorization": {
                "entries": [
                    {
                        "identity": ENTRY_KEY,
                        "start_date": DAY.isoformat(),
                        "end_date": DAY.isoformat(),
                        "hours": "8.00",
                    }
                ]
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class _PlanningStub:
    def __init__(self) -> None:
        self.calls = 0

    def rebuild(self):
        self.calls += 1
        return {}


class _FailingAuditJournal(SqlPlanningAuditJournal):
    def append(self, **_kwargs):
        raise RuntimeError("613C injected audit failure")


class PlanningWindowOverrideCommandTests(unittest.TestCase):
    @staticmethod
    def _database(directory: str) -> str:
        path = Path(directory) / "planning-window-613c.db"
        url = f"sqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add(
                Project(
                    id=PROJECT_ID,
                    number="P-613C",
                    name="Projet 613C",
                )
            )
            session.add_all(
                [
                    Resource(id=RESOURCE_A, name="Alice 613C", active=True),
                    Resource(id=RESOURCE_B, name="Bob 613C", active=True),
                ]
            )
            session.add(
                AppUser(
                    id=ADMIN_ID,
                    issuer="urn:resourceplanner:test",
                    subject="admin-613c",
                    display_name="Admin 613C",
                    roles_json=json.dumps([ROLE_ADMIN]),
                    active=True,
                )
            )
            session.flush()
            for resource_id in (RESOURCE_A, RESOURCE_B):
                session.add(
                    ResourceAvailabilityRule(
                        id=f"STD-{resource_id}",
                        resource_id=resource_id,
                        availability_type="Horaire standard",
                        weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                        start_time=time(8, 0),
                        end_time=time(20, 0),
                        active=True,
                    )
                )
            request = WorkforceRequest(
                id=REQUEST_ID,
                legacy_demand_number=REQUEST_NUMBER,
                project_id=PROJECT_ID,
                requester_user_id=ADMIN_ID,
                requester_name="Admin 613C",
                desired_start=DAY,
                desired_end=DAY,
                resource_count=1,
                estimated_hours=Decimal("8.00"),
                proposed_resource_id=RESOURCE_A,
                status="En planification",
                approved_by_name="Admin 613C",
                approved_at=utc_now(),
                aggregate_version=1,
            )
            session.add(request)
            session.flush()
            revision = RequestApprovalRevision(
                id=REVISION_ID,
                workforce_request_id=REQUEST_ID,
                request_version=1,
                approved_by_name="Admin 613C",
                approved_at=utc_now(),
                provenance="APPROVAL",
                payload_format_version=1,
                payload_text=_snapshot_payload(),
                authorization_fingerprint="6" * 64,
            )
            session.add(revision)
            session.flush()
            session.add(
                RequestApprovalReference(
                    workforce_request_id=REQUEST_ID,
                    active_revision_id=REVISION_ID,
                    status=APPROVAL_REFERENCE_CAPTURED,
                )
            )
            session.add(
                ResourceRequirement(
                    id=REQUIREMENT_ID,
                    legacy_segment_id=SEGMENT_ID,
                    project_id=PROJECT_ID,
                    workforce_request_id=REQUEST_ID,
                    approval_revision_id=REVISION_ID,
                    approved_entry_key=ENTRY_KEY,
                    approval_reference_status=APPROVAL_REFERENCE_CAPTURED,
                    assigned_resource_id=RESOURCE_A,
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("8.00"),
                    status="Planifié",
                    planning_type="Flexible",
                    confirmation="Confirmée",
                    origin="REQUEST",
                )
            )
            session.flush()
            session.add(
                Shift(
                    id=SHIFT_ID,
                    legacy_allocation_id=ALLOCATION_ID,
                    resource_requirement_id=REQUIREMENT_ID,
                    resource_id=RESOURCE_A,
                    work_date=DAY,
                    hours=Decimal("8.00"),
                    allocation_type="Flexible",
                    source="AUTO",
                    locked=False,
                    outside_standard_hours=False,
                    confirmation="Confirmée",
                )
            )
        engine.dispose()
        return url

    @staticmethod
    def _adapter(
        session,
        planning,
        *,
        permissions=None,
        actor_user_id: str | None = ADMIN_ID,
        journal=None,
    ) -> SqlCompositeAllocationCommandAdapter:
        versioning = SqlPlanningMutationVersionRepository(session)
        return SqlCompositeAllocationCommandAdapter(
            session,
            planning=planning,
            authorization=SqlRequestPlanningAuthorizationRepository(
                session,
                actor_name="Admin 613C",
                roles=(ROLE_ADMIN,),
            ),
            versioning=versioning,
            journal=journal or SqlPlanningAuditJournal(
                session,
                actor_name="Admin 613C",
            ),
            actor_user_id=actor_user_id,
            permissions=(
                permissions_for_roles((ROLE_ADMIN,))
                if permissions is None
                else permissions
            ),
        )

    @staticmethod
    def _version(session) -> int:
        return SqlPlanningMutationVersionRepository(session).current_version()

    def test_pre_evaluation_distinguishes_override_from_reapproval_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    privileged = self._adapter(session, _PlanningStub())
                    result = privileged.evaluate_drop(
                        AllocationDropEvaluateCommand(
                            allocation_id=ALLOCATION_ID,
                            resource_id=RESOURCE_B,
                            day=NEXT_DAY,
                        )
                    )
                    self.assertEqual(
                        result["authorization_decision"],
                        "PLANNING_WINDOW_OVERRIDE_AVAILABLE",
                    )
                    self.assertEqual(
                        [row["code"] for row in result["actions"]],
                        [
                            "OVERRIDE_WINDOW_AND_MOVE",
                            "PROPOSE_WINDOW_EXTENSION",
                            "CANCEL",
                        ],
                    )

                    ordinary = self._adapter(
                        session,
                        _PlanningStub(),
                        permissions=(PERMISSION_MANAGE_PLANNING,),
                    )
                    fallback = ordinary.evaluate_drop(
                        AllocationDropEvaluateCommand(
                            allocation_id=ALLOCATION_ID,
                            resource_id=RESOURCE_B,
                            day=NEXT_DAY,
                        )
                    )
                    self.assertEqual(
                        fallback["authorization_decision"],
                        "WINDOW_EXTENSION_REAPPROVAL_REQUIRED",
                    )
                    self.assertEqual(
                        [row["code"] for row in fallback["actions"]],
                        ["PROPOSE_WINDOW_EXTENSION", "CANCEL"],
                    )
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count()).select_from(
                                    PlanningWindowOverride
                                )
                            )
                            or 0
                        ),
                        0,
                    )
                    self.assertEqual(self._version(session), 1)
            finally:
                engine.dispose()

    def test_segment_extension_is_atomic_idempotent_and_keeps_approval_immutable(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            command = PlanningWindowOverrideExtendCommand(
                segment_id=SEGMENT_ID,
                start_date=DAY,
                end_date=NEXT_DAY,
                reason="Intervention planifiée hors fenêtre approuvée",
                expected_planning_version=1,
                expected_approval_revision_id=REVISION_ID,
                idempotency_key="613c-extend",
            )
            try:
                first_planning = _PlanningStub()
                with factory.begin() as session:
                    first = self._adapter(
                        session,
                        first_planning,
                    ).extend_planning_window(command)
                self.assertEqual(first_planning.calls, 1)
                self.assertEqual(first["operation"], "OVERRIDE_WINDOW")
                self.assertEqual(first["planning_version"], 2)

                replay_planning = _PlanningStub()
                with factory.begin() as session:
                    replay = self._adapter(
                        session,
                        replay_planning,
                    ).extend_planning_window(command)
                self.assertEqual(replay, first)
                self.assertEqual(replay_planning.calls, 0)

                with factory() as session:
                    request = session.get(WorkforceRequest, REQUEST_ID)
                    requirement = session.get(ResourceRequirement, REQUIREMENT_ID)
                    revision = session.get(RequestApprovalRevision, REVISION_ID)
                    override = session.scalar(
                        select(PlanningWindowOverride).where(
                            PlanningWindowOverride.resource_requirement_id
                            == REQUIREMENT_ID,
                            PlanningWindowOverride.status
                            == PLANNING_WINDOW_OVERRIDE_ACTIVE,
                        )
                    )
                    self.assertIsNotNone(request)
                    self.assertIsNotNone(requirement)
                    self.assertIsNotNone(revision)
                    self.assertIsNotNone(override)
                    assert request is not None
                    assert requirement is not None
                    assert revision is not None
                    assert override is not None
                    self.assertEqual(request.desired_end, DAY)
                    self.assertEqual(revision.payload_text, _snapshot_payload())
                    self.assertEqual(requirement.end_date, NEXT_DAY)
                    self.assertEqual(override.approved_end_date, DAY)
                    self.assertEqual(override.effective_end_date, NEXT_DAY)
                    self.assertEqual(override.actor_user_id, ADMIN_ID)
                    self.assertEqual(
                        override.reason,
                        "Intervention planifiée hors fenêtre approuvée",
                    )
                    self.assertEqual(override.correlation_id, "613c-extend")
                    self.assertEqual(self._version(session), 2)
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count())
                                .select_from(CommandIdempotencyReceipt)
                                .where(
                                    CommandIdempotencyReceipt.actor_name
                                    == ADMIN_ID,
                                    CommandIdempotencyReceipt.command_scope
                                    == "planning_window_override.extend_segment",
                                )
                            )
                            or 0
                        ),
                        1,
                    )
            finally:
                engine.dispose()

    def test_override_and_move_converts_auto_to_manual_locked_and_replays_stale_version(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            command = AllocationWindowOverrideMoveCommand(
                allocation_id=ALLOCATION_ID,
                resource_id=RESOURCE_B,
                day=NEXT_DAY,
                reason="Déplacement opérationnel confirmé",
                expected_planning_version=1,
                expected_approval_revision_id=REVISION_ID,
                idempotency_key="613c-move",
            )
            try:
                first_planning = _PlanningStub()
                with factory.begin() as session:
                    first = self._adapter(
                        session,
                        first_planning,
                    ).override_and_move(command)
                self.assertEqual(first_planning.calls, 1)
                self.assertEqual(first["operation"], "OVERRIDE_WINDOW_AND_MOVE")
                self.assertEqual(first["planning_version"], 2)
                self.assertTrue(first["auto_source_converted"])

                replay_planning = _PlanningStub()
                with factory.begin() as session:
                    replay = self._adapter(
                        session,
                        replay_planning,
                    ).override_and_move(command)
                self.assertEqual(replay, first)
                self.assertEqual(replay_planning.calls, 0)

                with factory() as session:
                    request = session.get(WorkforceRequest, REQUEST_ID)
                    requirement = session.get(ResourceRequirement, REQUIREMENT_ID)
                    shift = session.get(Shift, SHIFT_ID)
                    override = session.scalar(
                        select(PlanningWindowOverride).where(
                            PlanningWindowOverride.resource_requirement_id
                            == REQUIREMENT_ID,
                            PlanningWindowOverride.status
                            == PLANNING_WINDOW_OVERRIDE_ACTIVE,
                        )
                    )
                    assert request is not None
                    assert requirement is not None
                    assert shift is not None
                    assert override is not None
                    self.assertEqual(request.desired_end, DAY)
                    self.assertEqual(requirement.end_date, NEXT_DAY)
                    self.assertEqual(shift.resource_id, RESOURCE_B)
                    self.assertEqual(shift.work_date, NEXT_DAY)
                    self.assertEqual(shift.source, "MANUAL")
                    self.assertTrue(shift.locked)
                    self.assertFalse(shift.outside_standard_hours)
                    self.assertEqual(override.effective_end_date, NEXT_DAY)
                    self.assertEqual(override.correlation_id, "613c-move")
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count())
                                .select_from(PlanningChangeHistory)
                                .where(
                                    PlanningChangeHistory.action
                                    == "Déplacement atomique avec dérogation"
                                )
                            )
                            or 0
                        ),
                        1,
                    )
            finally:
                engine.dispose()

    def test_override_requires_permission_and_active_stable_app_user(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            command = PlanningWindowOverrideExtendCommand(
                segment_id=SEGMENT_ID,
                start_date=DAY,
                end_date=NEXT_DAY,
                reason="Test permission",
                expected_planning_version=1,
                expected_approval_revision_id=REVISION_ID,
                idempotency_key="613c-permission",
            )
            try:
                with factory() as session:
                    with self.assertRaises(ApplicationAuthorizationError) as denied:
                        self._adapter(
                            session,
                            _PlanningStub(),
                            permissions=(PERMISSION_MANAGE_PLANNING,),
                        ).extend_planning_window(command)
                    self.assertEqual(
                        denied.exception.code,
                        "planning_window_override_permission_denied",
                    )
                    self.assertEqual(self._version(session), 1)

                with factory.begin() as session:
                    actor = session.get(AppUser, ADMIN_ID)
                    assert actor is not None
                    actor.active = False

                with factory() as session:
                    with self.assertRaises(ApplicationAuthorizationError) as inactive:
                        self._adapter(
                            session,
                            _PlanningStub(),
                        ).extend_planning_window(command)
                    self.assertEqual(
                        inactive.exception.code,
                        "planning_window_override_actor_required",
                    )
                    self.assertEqual(self._version(session), 1)
            finally:
                engine.dispose()

    def test_failure_after_rebuild_rolls_back_window_shift_receipt_and_planning_version(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            command = AllocationWindowOverrideMoveCommand(
                allocation_id=ALLOCATION_ID,
                resource_id=RESOURCE_B,
                day=NEXT_DAY,
                reason="Rollback audit 613C",
                expected_planning_version=1,
                expected_approval_revision_id=REVISION_ID,
                idempotency_key="613c-rollback",
            )
            planning = _PlanningStub()
            try:
                with self.assertRaisesRegex(RuntimeError, "audit failure"):
                    with factory.begin() as session:
                        self._adapter(
                            session,
                            planning,
                            journal=_FailingAuditJournal(
                                session,
                                actor_name="Admin 613C",
                            ),
                        ).override_and_move(command)
                self.assertEqual(planning.calls, 1)

                with factory() as session:
                    requirement = session.get(ResourceRequirement, REQUIREMENT_ID)
                    shift = session.get(Shift, SHIFT_ID)
                    assert requirement is not None
                    assert shift is not None
                    self.assertEqual(requirement.start_date, DAY)
                    self.assertEqual(requirement.end_date, DAY)
                    self.assertEqual(shift.resource_id, RESOURCE_A)
                    self.assertEqual(shift.work_date, DAY)
                    self.assertEqual(shift.source, "AUTO")
                    self.assertFalse(shift.locked)
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count()).select_from(
                                    PlanningWindowOverride
                                )
                            )
                            or 0
                        ),
                        0,
                    )
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count()).select_from(
                                    CommandIdempotencyReceipt
                                )
                            )
                            or 0
                        ),
                        0,
                    )
                    self.assertEqual(self._version(session), 1)
            finally:
                engine.dispose()

    def test_accepted_cancellation_supersedes_active_override(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            command = PlanningWindowOverrideExtendCommand(
                segment_id=SEGMENT_ID,
                start_date=DAY,
                end_date=NEXT_DAY,
                reason="Dérogation avant annulation",
                expected_planning_version=1,
                expected_approval_revision_id=REVISION_ID,
                idempotency_key="613c-before-cancel",
            )
            try:
                with factory.begin() as session:
                    self._adapter(
                        session,
                        _PlanningStub(),
                    ).extend_planning_window(command)

                with factory.begin() as session:
                    request = session.get(WorkforceRequest, REQUEST_ID)
                    assert request is not None
                    request.cancellation_request_id = "CANCEL-613C"
                    request.cancellation_state = "PENDING"
                    request.cancellation_requested_by_user_id = ADMIN_ID
                    request.cancellation_requested_at = utc_now()
                    request.cancellation_reason = "Annuler le plan"

                with factory.begin() as session:
                    SqlDemandRepository(
                        session,
                        actor_name="Admin 613C",
                        actor_user_id=ADMIN_ID,
                    ).accept_cancellation(
                        REQUEST_NUMBER,
                        cancellation_request_id="CANCEL-613C",
                        comment="Annulation coordonnée",
                        expected_version=1,
                        planning_version=2,
                        correlation_id="cancel-613c",
                    )

                with factory() as session:
                    request = session.get(WorkforceRequest, REQUEST_ID)
                    override = session.scalar(
                        select(PlanningWindowOverride).where(
                            PlanningWindowOverride.resource_requirement_id
                            == REQUIREMENT_ID
                        )
                    )
                    assert request is not None
                    assert override is not None
                    self.assertEqual(request.status, "Annulée")
                    self.assertEqual(request.cancellation_state, "ACCEPTED")
                    self.assertEqual(
                        override.status,
                        PLANNING_WINDOW_OVERRIDE_SUPERSEDED,
                    )
                    self.assertEqual(
                        override.resolution_reason,
                        "CANCELLATION_ACCEPTED",
                    )
                    self.assertIsNotNone(override.resolved_at)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
