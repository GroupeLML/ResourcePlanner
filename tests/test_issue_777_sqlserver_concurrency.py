from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta
from threading import Barrier, Lock
from decimal import Decimal
import json
import os
from tempfile import TemporaryDirectory
from uuid import uuid4
import unittest

from sqlalchemy import delete, event, func, or_, select, text

from app.application.errors import ApplicationConflictError
from app.application.request_work_queue import (
    PermanentlyReduceShiftHoursCommand,
    PlaceSegmentFromDropCommand,
    ReturnShiftToWorkQueueCommand,
)
from app.application.security import ROLE_ADMIN, permissions_for_roles
from app.domain.approval_envelope import EnvelopeEntryIdentity
from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.infrastructure.sql import (
    Base,
    PlanningChangeHistory,
    PlanningWorkQueueRelease,
    PlanningWorkQueueRestoration,
    Project,
    RequestCompletionReduction,
    RequestLine,
    RequestOperationalState,
    RequestRequirementPlanningIntent,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.approval_scope_models import (
    ApprovalScope,
    ApprovalScopeApprover,
    ResourceClassApprovalScopeMapping,
)
from app.infrastructure.sql.approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from app.infrastructure.sql.base import utc_now
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.planning_audit import SqlPlanningAuditJournal
from app.infrastructure.sql.planning_intent_repository import (
    SqlRequestRequirementPlanningIntentRepository,
)
from app.infrastructure.sql.planning_version import (
    PLANNING_STATE_ID,
    PlanningMutationState,
    SqlPlanningMutationVersionRepository,
)
from app.infrastructure.sql.request_version import acquire_request_aggregate_version
from app.infrastructure.sql.request_work_queue_commands import (
    SqlRequestWorkQueueCommandAdapter,
)
from app.infrastructure.sql.resource_class_models import ResourceClassConfig


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")
DAY = date(2026, 11, 2)


@unittest.skipUnless(
    IS_MSSQL,
    "Validation #777 réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class Issue777SqlServerConcurrencyTests(unittest.TestCase):
    def _fixture(self) -> dict[str, object]:
        marker = uuid4().hex[:10]
        project_id = f"P777-{marker}"
        request_id = f"D777-{marker}"
        request_number = f"DMO-777-{marker}"
        line_id = f"L777-{marker}"
        revision_id = f"REV777-{marker}"
        requirement_id = f"REQ777-{marker}"
        segment_id = f"SEG777-{marker}"
        intent_id = f"INT777-{marker}"
        actor_id = f"USR777-{marker}"
        scope_id = f"SCOPE777-{marker}"
        class_code = f"CLASS777{marker}".upper()
        resources = {
            "A": f"R777A-{marker}",
            "B": f"R777B-{marker}",
        }
        availability_ids = {
            key: f"AV777{key}-{marker}" for key in resources
        }
        shift_ids = tuple(f"S777-{marker}-{index}" for index in range(4))
        allocation_ids = tuple(f"A777-{marker}-{index}" for index in range(4))
        release_id = f"REL777-{marker}"
        actor_name = f"Admin 777 {marker}"
        entry_key = EnvelopeEntryIdentity(line_id=line_id).stable_key
        payload = json.dumps(
            {
                "format_version": 1,
                "request": {
                    "request_id": request_id,
                    "request_version": 1,
                    "project_id": project_id,
                    "priority": None,
                    "site_client": None,
                    "location": None,
                    "line_mode": True,
                },
                "authorization": {
                    "format_version": 1,
                    "authorization_fingerprint": marker.ljust(64, "7"),
                    "entries": [
                        {
                            "identity": entry_key,
                            "project_id": project_id,
                            "site_id": None,
                            "location": None,
                            "slot_count": 1,
                            "line_kind": "WORKFORCE",
                            "required_resource_class": class_code,
                            "competency_ids": [],
                            "task_ref": None,
                            "work_package_ref": None,
                            "start_date": DAY.isoformat(),
                            "end_date": (DAY + timedelta(days=4)).isoformat(),
                            "hours": "40.00",
                            "kind": "CUMULATIVE",
                            "group": None,
                            "confirmation": "Confirmée",
                        }
                    ],
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        fixture: dict[str, object] = {
            "marker": marker,
            "engine": engine,
            "factory": factory,
            "project_id": project_id,
            "request_id": request_id,
            "request_number": request_number,
            "line_id": line_id,
            "revision_id": revision_id,
            "requirement_id": requirement_id,
            "segment_id": segment_id,
            "intent_id": intent_id,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "scope_id": scope_id,
            "class_code": class_code,
            "resources": resources,
            "availability_ids": availability_ids,
            "shift_ids": shift_ids,
            "allocation_ids": allocation_ids,
            "release_id": release_id,
            "planning_version": None,
            "race_spids": {},
            "race_spid_lock": Lock(),
        }
        # unittest cleanups are LIFO: row teardown executes before engine disposal.
        self.addCleanup(engine.dispose)
        self.addCleanup(self._cleanup, fixture)

        with factory.begin() as session:
            session.add(
                Project(
                    id=project_id,
                    number=f"P-777-{marker}",
                    name=f"Projet SQL #777 {marker}",
                )
            )
            session.add(
                ResourceClassConfig(
                    code=class_code,
                    label=f"Classe #777 {marker}",
                    average_hourly_cost_cad=Decimal("100.00"),
                    active=True,
                    version=1,
                )
            )
            session.add(
                ApprovalScope(
                    id=scope_id,
                    code=f"SCOPE_777_{marker}".upper(),
                    label=f"Scope #777 {marker}",
                    active=True,
                    version=1,
                )
            )
            session.add(
                AppUser(
                    id=actor_id,
                    issuer="urn:resourceplanner:test",
                    subject=actor_id,
                    display_name=actor_name,
                    roles_json=json.dumps([ROLE_ADMIN]),
                    active=True,
                )
            )
            for key, resource_id in resources.items():
                session.add(
                    Resource(
                        id=resource_id,
                        name=f"Ressource 777 {key} {marker}",
                        resource_class=class_code,
                        active=True,
                        erp_active=True,
                    )
                )
            session.flush()
            session.add(
                ApprovalScopeApprover(
                    approval_scope_id=scope_id,
                    app_user_id=actor_id,
                )
            )
            session.add(
                ResourceClassApprovalScopeMapping(
                    resource_class_code=class_code,
                    approval_scope_id=scope_id,
                )
            )
            for key, resource_id in resources.items():
                session.add(
                    ResourceAvailabilityRule(
                        id=availability_ids[key],
                        resource_id=resource_id,
                        availability_type="Horaire standard",
                        weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                        start_time=time(8, 0),
                        end_time=time(16, 0),
                        active=True,
                    )
                )
            session.add(
                WorkforceRequest(
                    id=request_id,
                    legacy_demand_number=request_number,
                    project_id=project_id,
                    requester_user_id=actor_id,
                    requester_name=actor_name,
                    desired_start=DAY,
                    desired_end=DAY + timedelta(days=4),
                    resource_count=1,
                    estimated_hours=Decimal("40.00"),
                    status="En planification",
                    aggregate_version=1,
                    line_mode=True,
                    confirmation="Confirmée",
                )
            )
            session.flush()
            session.add(
                RequestLine(
                    id=line_id,
                    workforce_request_id=request_id,
                    position=0,
                    kind="WORKFORCE",
                    slot_count=1,
                    desired_start=DAY,
                    desired_end=DAY + timedelta(days=4),
                    estimated_hours=Decimal("40.00"),
                    required_resource_class=class_code,
                    confirmation="Confirmée",
                    active=True,
                )
            )
            session.add(
                RequestApprovalRevision(
                    id=revision_id,
                    workforce_request_id=request_id,
                    request_version=1,
                    approved_by_name=actor_name,
                    approved_at=utc_now(),
                    provenance="APPROVAL",
                    payload_format_version=1,
                    payload_text=payload,
                    authorization_fingerprint=marker.ljust(64, "7"),
                )
            )
            session.flush()
            session.add(
                RequestApprovalReference(
                    workforce_request_id=request_id,
                    active_revision_id=revision_id,
                    status=APPROVAL_REFERENCE_CAPTURED,
                )
            )
            session.add(
                RequestOperationalState(
                    workforce_request_id=request_id,
                    approval_revision_id=revision_id,
                    version=1,
                    selections_text="{}",
                    confirmations_text="{}",
                    budget_overrides_text="{}",
                )
            )
            session.add(
                ResourceRequirement(
                    id=requirement_id,
                    legacy_segment_id=segment_id,
                    project_id=project_id,
                    workforce_request_id=request_id,
                    source_request_line_id=line_id,
                    approval_revision_id=revision_id,
                    approved_entry_key=entry_key,
                    approval_reference_status=APPROVAL_REFERENCE_CAPTURED,
                    start_date=DAY,
                    end_date=DAY + timedelta(days=4),
                    planned_hours=Decimal("40.00"),
                    status="Planifié",
                    planning_type="Flexible",
                    confirmation="Confirmée",
                    required_resource_class=class_code,
                    origin="REQUEST",
                    assigned_resource_id=resources["A"],
                )
            )
            session.flush()
            session.add(
                RequestRequirementPlanningIntent(
                    id=intent_id,
                    slot_identity_key=json.dumps(
                        [request_id, line_id, entry_key, "", 0],
                        separators=(",", ":"),
                    ),
                    workforce_request_id=request_id,
                    source_request_line_id=line_id,
                    approval_revision_id=revision_id,
                    approved_entry_key=entry_key,
                    slot_index=0,
                    current_resource_requirement_id=requirement_id,
                    intent_mode="PRESERVE_EXISTING",
                    target_mode="EXPLICIT_RESOURCE",
                    target_resource_id=resources["A"],
                )
            )
            session.flush()
            for index, (shift_id, allocation_id) in enumerate(
                zip(shift_ids, allocation_ids, strict=True),
                start=1,
            ):
                session.add(
                    Shift(
                        id=shift_id,
                        legacy_allocation_id=allocation_id,
                        resource_requirement_id=requirement_id,
                        resource_id=resources["A"],
                        work_date=DAY + timedelta(days=index),
                        hours=Decimal("8.00"),
                        allocation_type="Flexible",
                        source="AUTO",
                        locked=False,
                    )
                )
            session.add(
                PlanningWorkQueueRelease(
                    id=release_id,
                    planning_intent_id=intent_id,
                    resource_requirement_id=requirement_id,
                    original_shift_id=f"RETURNED-{marker}",
                    workforce_request_id=request_id,
                    approval_revision_id=revision_id,
                    approved_entry_key=entry_key,
                    resource_id=resources["A"],
                    work_date=DAY,
                    hours=Decimal("8.00"),
                    shift_snapshot_text="{}",
                    actor_user_id=actor_id,
                    command_id=f"seed-release-{marker}",
                    active=True,
                )
            )

        with factory() as session:
            persisted_request = session.get(WorkforceRequest, request_id)
            persisted_line = session.get(RequestLine, line_id)
            persisted_actor = session.get(AppUser, actor_id)
            persisted_resources = {
                key: session.get(Resource, resource_id)
                for key, resource_id in resources.items()
            }
            persisted_schedules = tuple(
                session.scalars(
                    select(ResourceAvailabilityRule)
                    .where(ResourceAvailabilityRule.id.in_(tuple(availability_ids.values())))
                    .order_by(ResourceAvailabilityRule.id)
                ).all()
            )
            persisted_reference = session.get(RequestApprovalReference, request_id)
            persisted_operational = session.get(RequestOperationalState, request_id)
            persisted_requirement = session.get(ResourceRequirement, requirement_id)
            persisted_intent = session.get(RequestRequirementPlanningIntent, intent_id)
            persisted_revision = session.get(RequestApprovalRevision, revision_id)
            persisted_release = session.get(PlanningWorkQueueRelease, release_id)
            persisted_shifts = tuple(
                session.scalars(
                    select(Shift)
                    .where(Shift.id.in_(shift_ids))
                    .order_by(Shift.work_date, Shift.id)
                ).all()
            )
            persisted_reductions = tuple(
                session.scalars(
                    select(RequestCompletionReduction).where(
                        RequestCompletionReduction.workforce_request_id == request_id
                    )
                ).all()
            )
            release_ids = tuple(
                session.scalars(
                    select(PlanningWorkQueueRelease.id).where(
                        PlanningWorkQueueRelease.workforce_request_id == request_id
                    )
                ).all()
            )
            persisted_restorations = tuple(
                session.scalars(
                    select(PlanningWorkQueueRestoration).where(
                        PlanningWorkQueueRestoration.release_id.in_(release_ids)
                    )
                ).all()
            ) if release_ids else ()
            global_state = session.get(PlanningMutationState, PLANNING_STATE_ID)

            self.assertIsNotNone(persisted_request)
            self.assertIsNotNone(persisted_line)
            self.assertIsNotNone(persisted_actor)
            self.assertTrue(all(resource is not None for resource in persisted_resources.values()))
            self.assertEqual(len(persisted_schedules), 2)
            self.assertIsNotNone(persisted_reference)
            self.assertEqual(persisted_reference.active_revision_id, revision_id)
            self.assertIsNotNone(persisted_operational)
            self.assertEqual(persisted_operational.approval_revision_id, revision_id)
            self.assertEqual(int(persisted_operational.version), 1)
            self.assertIsNotNone(persisted_requirement)
            self.assertEqual(persisted_requirement.approval_revision_id, revision_id)
            self.assertIsNotNone(persisted_intent)
            self.assertEqual(
                persisted_intent.current_resource_requirement_id,
                persisted_requirement.id,
            )
            self.assertEqual(persisted_intent.approval_revision_id, revision_id)
            self.assertEqual(persisted_intent.approved_entry_key, entry_key)
            self.assertEqual(int(persisted_intent.slot_index), 0)
            self.assertIsNotNone(persisted_revision)
            self.assertEqual(persisted_revision.workforce_request_id, request_id)
            self.assertEqual(int(persisted_revision.request_version), 1)
            self.assertEqual(len(persisted_shifts), 4)
            for index, persisted_shift in enumerate(persisted_shifts, start=1):
                self.assertEqual(persisted_shift.resource_requirement_id, requirement_id)
                self.assertEqual(persisted_shift.resource_id, resources["A"])
                self.assertEqual(persisted_shift.work_date, DAY + timedelta(days=index))
                self.assertEqual(persisted_shift.hours, Decimal("8.00"))
                self.assertFalse(persisted_shift.locked)
                self.assertEqual(persisted_shift.source, "AUTO")
            self.assertIsNotNone(persisted_release)
            self.assertEqual(persisted_release.planning_intent_id, persisted_intent.id)
            self.assertEqual(persisted_release.resource_id, resources["A"])
            self.assertEqual(persisted_release.work_date, DAY)
            self.assertEqual(persisted_release.hours, Decimal("8.00"))
            self.assertTrue(persisted_release.active)
            self.assertEqual(persisted_reductions, ())
            self.assertEqual(persisted_restorations, ())
            self.assertIsNotNone(global_state)
            planning_version = int(global_state.version)

            effective_need = SqlRequestRequirementPlanningIntentRepository(
                session
            ).current_effective_need_for_requirement(
                requirement_id,
                fallback_hours=persisted_requirement.planned_hours,
            )
            counted_coverage = Decimal(
                str(
                    session.scalar(
                        select(func.coalesce(func.sum(Shift.hours), 0)).where(
                            Shift.resource_requirement_id == requirement_id,
                            or_(
                                Shift.allocation_type.is_(None),
                                Shift.allocation_type != MISSING_ALLOCATION_TYPE,
                            ),
                        )
                    )
                    or 0
                )
            )
            self.assertEqual(effective_need, Decimal("40.00"))
            self.assertEqual(counted_coverage, Decimal("32.00"))
            self.assertEqual(effective_need - counted_coverage, Decimal("8.00"))

        fixture["planning_version"] = planning_version
        return fixture

    @staticmethod
    def _adapter(session, fixture: dict[str, object]) -> SqlRequestWorkQueueCommandAdapter:
        return SqlRequestWorkQueueCommandAdapter(
            session,
            versioning=SqlPlanningMutationVersionRepository(session),
            journal=SqlPlanningAuditJournal(
                session,
                actor_name=str(fixture["actor_name"]),
            ),
            actor_user_id=str(fixture["actor_id"]),
            actor_name=str(fixture["actor_name"]),
            permissions=permissions_for_roles((ROLE_ADMIN,)),
            roles=(ROLE_ADMIN,),
        )

    @staticmethod
    def _prepare_race_session(
        session,
        fixture: dict[str, object],
        participant: str,
    ) -> int:
        connection = session.connection()
        connection.exec_driver_sql("SET LOCK_TIMEOUT 15000")
        spid = int(connection.exec_driver_sql("SELECT @@SPID").scalar_one())
        connection.info["issue777_race_participant"] = participant
        lock = fixture["race_spid_lock"]
        with lock:
            fixture["race_spids"][participant] = spid
        return spid

    @staticmethod
    def _install_cas_rendezvous(
        fixture: dict[str, object],
        participants: tuple[str, str],
    ):
        barrier = Barrier(2, timeout=10)
        seen: set[str] = set()
        seen_lock = Lock()
        participant_set = set(participants)

        def before_cursor_execute(
            conn,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ) -> None:
            participant = conn.info.get("issue777_race_participant")
            normalized = " ".join(str(statement).lower().split())
            if (
                participant not in participant_set
                or not normalized.startswith("update")
                or "planning_mutation_state" not in normalized
            ):
                return
            with seen_lock:
                if participant in seen:
                    return
                seen.add(participant)
            barrier.wait()

        event.listen(
            fixture["engine"],
            "before_cursor_execute",
            before_cursor_execute,
        )
        return before_cursor_execute

    @staticmethod
    def _remove_cas_rendezvous(
        fixture: dict[str, object],
        listener,
    ) -> None:
        event.remove(
            fixture["engine"],
            "before_cursor_execute",
            listener,
        )

    @staticmethod
    def _expected_race_conflict(exc: ApplicationConflictError) -> tuple[str, str]:
        if exc.code != "planning_version_conflict":
            raise exc
        return "conflict", exc.code

    def _drop(
        self,
        fixture: dict[str, object],
        *,
        participant: str,
        resource_id: str,
        dropped_date: date,
        command_id: str,
    ) -> tuple[str, str | None]:
        factory = fixture["factory"]
        try:
            with factory.begin() as session:
                self._prepare_race_session(session, fixture, participant)
                result = self._adapter(session, fixture).place_segment_from_drop(
                    PlaceSegmentFromDropCommand(
                        segment_id=str(fixture["segment_id"]),
                        resource_id=resource_id,
                        dropped_date=dropped_date,
                        expected_planning_version=int(fixture["planning_version"]),
                        expected_approval_revision_id=str(fixture["revision_id"]),
                        expected_operational_version=1,
                        command_id=command_id,
                    )
                )
            return "success", str(result["placement_not_before_date"])
        except ApplicationConflictError as exc:
            return self._expected_race_conflict(exc)

    def _cleanup(self, fixture: dict[str, object]) -> None:
        factory = fixture["factory"]
        request_id = str(fixture["request_id"])
        with factory.begin() as session:
            # Collect the canonical ownership graph before deleting any parent row.
            line_ids = tuple(
                session.scalars(
                    select(RequestLine.id).where(
                        RequestLine.workforce_request_id == request_id
                    )
                ).all()
            )
            revision_rows = tuple(
                session.execute(
                    select(
                        RequestApprovalRevision.id,
                        RequestApprovalRevision.previous_revision_id,
                        RequestApprovalRevision.request_version,
                    ).where(
                        RequestApprovalRevision.workforce_request_id == request_id
                    )
                ).all()
            )
            requirement_ids = tuple(
                session.scalars(
                    select(ResourceRequirement.id).where(
                        ResourceRequirement.workforce_request_id == request_id
                    )
                ).all()
            )
            intent_ids = tuple(
                session.scalars(
                    select(RequestRequirementPlanningIntent.id).where(
                        RequestRequirementPlanningIntent.workforce_request_id
                        == request_id
                    )
                ).all()
            )
            release_ids = tuple(
                session.scalars(
                    select(PlanningWorkQueueRelease.id).where(
                        PlanningWorkQueueRelease.workforce_request_id == request_id
                    )
                ).all()
            )
            shift_ids = (
                tuple(
                    session.scalars(
                        select(Shift.id).where(
                            Shift.resource_requirement_id.in_(requirement_ids)
                        )
                    ).all()
                )
                if requirement_ids
                else ()
            )

            session.execute(
                delete(PlanningChangeHistory).where(
                    PlanningChangeHistory.actor_name == str(fixture["actor_name"])
                )
            )
            if release_ids:
                session.execute(
                    delete(PlanningWorkQueueRestoration).where(
                        PlanningWorkQueueRestoration.release_id.in_(release_ids)
                    )
                )
                session.execute(
                    delete(PlanningWorkQueueRelease).where(
                        PlanningWorkQueueRelease.id.in_(release_ids)
                    )
                )
            if intent_ids:
                session.execute(
                    delete(RequestCompletionReduction).where(
                        RequestCompletionReduction.planning_intent_id.in_(intent_ids)
                    )
                )
            else:
                session.execute(
                    delete(RequestCompletionReduction).where(
                        RequestCompletionReduction.workforce_request_id == request_id
                    )
                )
            if shift_ids:
                session.execute(delete(Shift).where(Shift.id.in_(shift_ids)))
            if intent_ids:
                session.execute(
                    delete(RequestRequirementPlanningIntent).where(
                        RequestRequirementPlanningIntent.id.in_(intent_ids)
                    )
                )
            if requirement_ids:
                session.execute(
                    delete(ResourceRequirement).where(
                        ResourceRequirement.id.in_(requirement_ids)
                    )
                )

            session.execute(
                delete(RequestOperationalState).where(
                    RequestOperationalState.workforce_request_id == request_id
                )
            )
            session.execute(
                delete(RequestApprovalReference).where(
                    RequestApprovalReference.workforce_request_id == request_id
                )
            )

            # Delete revision descendants before their previous_revision_id parents.
            remaining = {
                str(row.id): (
                    str(row.previous_revision_id)
                    if row.previous_revision_id is not None
                    else None
                )
                for row in revision_rows
            }
            while remaining:
                parents = {
                    previous
                    for previous in remaining.values()
                    if previous in remaining
                }
                leaves = tuple(
                    revision_id
                    for revision_id in remaining
                    if revision_id not in parents
                )
                if not leaves:
                    raise AssertionError(
                        "Cycle inattendu dans la chaîne de révisions #777."
                    )
                session.execute(
                    delete(RequestApprovalRevision).where(
                        RequestApprovalRevision.id.in_(leaves)
                    )
                )
                for revision_id in leaves:
                    remaining.pop(revision_id, None)

            if line_ids:
                session.execute(
                    delete(RequestLine).where(RequestLine.id.in_(line_ids))
                )
            session.execute(
                delete(WorkforceRequestHistory).where(
                    WorkforceRequestHistory.workforce_request_id == request_id
                )
            )
            session.execute(
                delete(WorkforceRequest).where(WorkforceRequest.id == request_id)
            )

            availability_ids = tuple(fixture["availability_ids"].values())
            if availability_ids:
                session.execute(
                    delete(ResourceAvailabilityRule).where(
                        ResourceAvailabilityRule.id.in_(availability_ids)
                    )
                )
            session.execute(
                delete(ResourceClassApprovalScopeMapping).where(
                    ResourceClassApprovalScopeMapping.resource_class_code
                    == str(fixture["class_code"])
                )
            )
            session.execute(
                delete(ApprovalScopeApprover).where(
                    ApprovalScopeApprover.approval_scope_id
                    == str(fixture["scope_id"])
                )
            )
            session.execute(
                delete(ApprovalScope).where(
                    ApprovalScope.id == str(fixture["scope_id"])
                )
            )
            session.execute(
                delete(ResourceClassConfig).where(
                    ResourceClassConfig.code == str(fixture["class_code"])
                )
            )
            session.execute(
                delete(Resource).where(
                    Resource.id.in_(tuple(fixture["resources"].values()))
                )
            )
            session.execute(
                delete(AppUser).where(AppUser.id == str(fixture["actor_id"]))
            )
            session.execute(
                delete(Project).where(Project.id == str(fixture["project_id"]))
            )

    @staticmethod
    def _counted_coverage(session, requirement_id: str) -> Decimal:
        value = session.scalar(
            select(func.coalesce(func.sum(Shift.hours), 0)).where(
                Shift.resource_requirement_id == requirement_id,
                or_(
                    Shift.allocation_type.is_(None),
                    Shift.allocation_type != MISSING_ALLOCATION_TYPE,
                ),
            )
        )
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))

    def _run_race(
        self,
        fixture: dict[str, object],
        participants: tuple[str, str],
        first_worker,
        second_worker,
    ) -> tuple[tuple[str, str | None], tuple[str, str | None]]:
        listener = self._install_cas_rendezvous(fixture, participants)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(first_worker)
                second = executor.submit(second_worker)
                outcomes = (
                    first.result(timeout=30),
                    second.result(timeout=30),
                )
        finally:
            self._remove_cas_rendezvous(fixture, listener)

        spids = fixture["race_spids"]
        self.assertEqual(set(spids), set(participants))
        self.assertNotEqual(spids[participants[0]], spids[participants[1]])
        self.assertEqual(
            sorted(row[0] for row in outcomes),
            ["conflict", "success"],
        )
        self.assertEqual(
            next(row[1] for row in outcomes if row[0] == "conflict"),
            "planning_version_conflict",
        )
        with fixture["factory"]() as session:
            global_state = session.get(PlanningMutationState, PLANNING_STATE_ID)
            self.assertIsNotNone(global_state)
            self.assertEqual(
                int(global_state.version),
                int(fixture["planning_version"]) + 1,
            )
        return outcomes

    def _assert_seed_survivors(
        self,
        session,
        fixture: dict[str, object],
        *,
        missing_allocation_ids: tuple[str, ...] = (),
    ) -> None:
        by_allocation = {
            shift.legacy_allocation_id: shift
            for shift in session.scalars(
                select(Shift).where(
                    Shift.resource_requirement_id == fixture["requirement_id"]
                )
            ).all()
            if shift.legacy_allocation_id in fixture["allocation_ids"]
        }
        for index, allocation_id in enumerate(fixture["allocation_ids"], start=1):
            if allocation_id in missing_allocation_ids:
                self.assertNotIn(allocation_id, by_allocation)
                continue
            shift = by_allocation.get(allocation_id)
            self.assertIsNotNone(shift)
            self.assertEqual(shift.resource_id, fixture["resources"]["A"])
            self.assertEqual(shift.work_date, DAY + timedelta(days=index))
            self.assertEqual(shift.hours, Decimal("8.00"))
            self.assertFalse(shift.locked)
            self.assertEqual(shift.source, "AUTO")

    def _assert_common_final_state(
        self,
        session,
        fixture: dict[str, object],
        *,
        expected_revision_id: str,
        expected_request_version: int,
        expected_operational_version: int,
        expected_effective_need: Decimal,
        expected_coverage: Decimal,
    ) -> tuple[
        WorkforceRequest,
        RequestOperationalState,
        ResourceRequirement,
        RequestRequirementPlanningIntent,
    ]:
        request = session.get(WorkforceRequest, fixture["request_id"])
        operational = session.get(RequestOperationalState, fixture["request_id"])
        requirement = session.get(ResourceRequirement, fixture["requirement_id"])
        intent = session.get(RequestRequirementPlanningIntent, fixture["intent_id"])
        reference = session.get(RequestApprovalReference, fixture["request_id"])
        self.assertIsNotNone(request)
        self.assertIsNotNone(operational)
        self.assertIsNotNone(requirement)
        self.assertIsNotNone(intent)
        self.assertIsNotNone(reference)
        self.assertEqual(int(request.aggregate_version), expected_request_version)
        self.assertEqual(int(operational.version), expected_operational_version)
        self.assertEqual(operational.approval_revision_id, expected_revision_id)
        self.assertEqual(reference.active_revision_id, expected_revision_id)
        self.assertEqual(requirement.approval_revision_id, expected_revision_id)
        self.assertEqual(intent.approval_revision_id, expected_revision_id)
        self.assertEqual(intent.intent_mode, "PRESERVE_EXISTING")
        effective_need = SqlRequestRequirementPlanningIntentRepository(
            session
        ).current_effective_need_for_requirement(
            requirement.id,
            fallback_hours=requirement.planned_hours,
        )
        coverage = self._counted_coverage(session, requirement.id)
        self.assertEqual(effective_need, expected_effective_need)
        self.assertEqual(coverage, expected_coverage)
        self.assertGreaterEqual(effective_need - coverage, Decimal("0.00"))

        seeded_release = session.get(
            PlanningWorkQueueRelease,
            fixture["release_id"],
        )
        self.assertIsNotNone(seeded_release)
        self.assertTrue(seeded_release.active)
        self.assertEqual(seeded_release.hours, Decimal("8.00"))
        self.assertEqual(seeded_release.resource_id, fixture["resources"]["A"])
        self.assertEqual(seeded_release.work_date, DAY)
        release_ids = tuple(
            session.scalars(
                select(PlanningWorkQueueRelease.id).where(
                    PlanningWorkQueueRelease.workforce_request_id
                    == fixture["request_id"]
                )
            ).all()
        )
        restorations = (
            tuple(
                session.scalars(
                    select(PlanningWorkQueueRestoration).where(
                        PlanningWorkQueueRestoration.release_id.in_(release_ids)
                    )
                ).all()
            )
            if release_ids
            else ()
        )
        self.assertEqual(restorations, ())
        return request, operational, requirement, intent

    def test_drop_vs_drop_has_one_authoritative_winner(self) -> None:
        fixture = self._fixture()
        participants = ("drop-a", "drop-b")
        outcomes = self._run_race(
            fixture,
            participants,
            lambda: self._drop(
                fixture,
                participant=participants[0],
                resource_id=fixture["resources"]["A"],
                dropped_date=DAY + timedelta(days=2),
                command_id=f"drop-a-{fixture['marker']}",
            ),
            lambda: self._drop(
                fixture,
                participant=participants[1],
                resource_id=fixture["resources"]["B"],
                dropped_date=DAY + timedelta(days=3),
                command_id=f"drop-b-{fixture['marker']}",
            ),
        )

        with fixture["factory"]() as session:
            drop_a_won = outcomes[0][0] == "success"
            expected_target = (
                fixture["resources"]["A"]
                if drop_a_won
                else fixture["resources"]["B"]
            )
            expected_cutoff = (
                DAY + timedelta(days=2)
                if drop_a_won
                else DAY + timedelta(days=3)
            )
            expected_coverage = (
                Decimal("32.00") if drop_a_won else Decimal("40.00")
            )
            _, _, requirement, intent = self._assert_common_final_state(
                session,
                fixture,
                expected_revision_id=str(fixture["revision_id"]),
                expected_request_version=1,
                expected_operational_version=1,
                expected_effective_need=Decimal("40.00"),
                expected_coverage=expected_coverage,
            )
            self.assertEqual(requirement.assigned_resource_id, expected_target)
            self.assertEqual(intent.target_resource_id, expected_target)
            self.assertEqual(intent.placement_not_before_date, expected_cutoff)
            self._assert_seed_survivors(session, fixture)

            counted = list(
                session.scalars(
                    select(Shift).where(
                        Shift.resource_requirement_id == fixture["requirement_id"],
                        or_(
                            Shift.allocation_type.is_(None),
                            Shift.allocation_type != MISSING_ALLOCATION_TYPE,
                        ),
                    )
                ).all()
            )
            generated = [
                row for row in counted if row.id not in set(fixture["shift_ids"])
            ]
            if drop_a_won:
                self.assertEqual(generated, [])
                self.assertEqual(Decimal("40.00") - expected_coverage, Decimal("8.00"))
            else:
                self.assertEqual(
                    sum((Decimal(str(row.hours)) for row in generated), Decimal("0")),
                    Decimal("8.00"),
                )
                self.assertTrue(
                    all(row.resource_id == fixture["resources"]["B"] for row in generated)
                )
                self.assertTrue(
                    all(row.work_date >= DAY + timedelta(days=3) for row in generated)
                )
                self.assertEqual(Decimal("40.00") - expected_coverage, Decimal("0.00"))

            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(RequestCompletionReduction.id)).where(
                            RequestCompletionReduction.workforce_request_id
                            == fixture["request_id"]
                        )
                    )
                    or 0
                ),
                0,
            )

    def test_drop_vs_return_shift_has_one_authoritative_winner(self) -> None:
        fixture = self._fixture()
        participants = ("drop", "return")

        def return_shift() -> tuple[str, str | None]:
            try:
                with fixture["factory"].begin() as session:
                    self._prepare_race_session(session, fixture, participants[1])
                    result = self._adapter(session, fixture).return_shift(
                        ReturnShiftToWorkQueueCommand(
                            allocation_id=fixture["allocation_ids"][0],
                            expected_planning_version=int(fixture["planning_version"]),
                            expected_approval_revision_id=str(fixture["revision_id"]),
                            expected_operational_version=1,
                            command_id=f"return-race-{fixture['marker']}",
                        )
                    )
                return "success", str(result["release_id"])
            except ApplicationConflictError as exc:
                return self._expected_race_conflict(exc)

        outcomes = self._run_race(
            fixture,
            participants,
            lambda: self._drop(
                fixture,
                participant=participants[0],
                resource_id=fixture["resources"]["B"],
                dropped_date=DAY + timedelta(days=3),
                command_id=f"drop-return-race-{fixture['marker']}",
            ),
            return_shift,
        )

        with fixture["factory"]() as session:
            drop_won = outcomes[0][0] == "success"
            expected_coverage = Decimal("40.00") if drop_won else Decimal("24.00")
            _, _, requirement, intent = self._assert_common_final_state(
                session,
                fixture,
                expected_revision_id=str(fixture["revision_id"]),
                expected_request_version=1,
                expected_operational_version=1,
                expected_effective_need=Decimal("40.00"),
                expected_coverage=expected_coverage,
            )
            if drop_won:
                self.assertEqual(requirement.assigned_resource_id, fixture["resources"]["B"])
                self.assertEqual(intent.target_resource_id, fixture["resources"]["B"])
                self.assertEqual(
                    intent.placement_not_before_date,
                    DAY + timedelta(days=3),
                )
                self._assert_seed_survivors(session, fixture)
                active_releases = tuple(
                    session.scalars(
                        select(PlanningWorkQueueRelease).where(
                            PlanningWorkQueueRelease.workforce_request_id
                            == fixture["request_id"],
                            PlanningWorkQueueRelease.active == True,
                        )
                    ).all()
                )
                self.assertEqual({row.id for row in active_releases}, {fixture["release_id"]})
            else:
                self.assertEqual(requirement.assigned_resource_id, fixture["resources"]["A"])
                self.assertEqual(intent.target_resource_id, fixture["resources"]["A"])
                self.assertIsNone(intent.placement_not_before_date)
                self._assert_seed_survivors(
                    session,
                    fixture,
                    missing_allocation_ids=(fixture["allocation_ids"][0],),
                )
                active_releases = tuple(
                    session.scalars(
                        select(PlanningWorkQueueRelease).where(
                            PlanningWorkQueueRelease.workforce_request_id
                            == fixture["request_id"],
                            PlanningWorkQueueRelease.active == True,
                        )
                    ).all()
                )
                self.assertEqual(len(active_releases), 2)
                new_releases = [
                    row for row in active_releases if row.id != fixture["release_id"]
                ]
                self.assertEqual(len(new_releases), 1)
                self.assertEqual(new_releases[0].hours, Decimal("8.00"))
                self.assertEqual(new_releases[0].work_date, DAY + timedelta(days=1))
                self.assertEqual(new_releases[0].resource_id, fixture["resources"]["A"])
                self.assertEqual(Decimal("40.00") - expected_coverage, Decimal("16.00"))

    def test_drop_vs_reapproval_has_one_authoritative_winner(self) -> None:
        fixture = self._fixture()
        participants = ("drop", "reapprove")

        def reapprove() -> tuple[str, str | None]:
            try:
                with fixture["factory"].begin() as session:
                    self._prepare_race_session(session, fixture, participants[1])
                    versioning = SqlPlanningMutationVersionRepository(session)
                    versioning.acquire(int(fixture["planning_version"]))
                    request = session.get(WorkforceRequest, fixture["request_id"])
                    acquire_request_aggregate_version(session, request, 1)
                    SqlPeriodAwareApprovedDemandSyncAdapter(session).sync_approved(
                        str(fixture["request_number"]),
                        approved_request_version=2,
                    )
                return "success", "reapproved"
            except ApplicationConflictError as exc:
                return self._expected_race_conflict(exc)

        outcomes = self._run_race(
            fixture,
            participants,
            lambda: self._drop(
                fixture,
                participant=participants[0],
                resource_id=fixture["resources"]["B"],
                dropped_date=DAY + timedelta(days=3),
                command_id=f"drop-reapproval-race-{fixture['marker']}",
            ),
            reapprove,
        )

        with fixture["factory"]() as session:
            drop_won = outcomes[0][0] == "success"
            revisions = tuple(
                session.scalars(
                    select(RequestApprovalRevision)
                    .where(
                        RequestApprovalRevision.workforce_request_id
                        == fixture["request_id"]
                    )
                    .order_by(RequestApprovalRevision.request_version)
                ).all()
            )
            if drop_won:
                self.assertEqual(len(revisions), 1)
                _, _, requirement, intent = self._assert_common_final_state(
                    session,
                    fixture,
                    expected_revision_id=str(fixture["revision_id"]),
                    expected_request_version=1,
                    expected_operational_version=1,
                    expected_effective_need=Decimal("40.00"),
                    expected_coverage=Decimal("40.00"),
                )
                self.assertEqual(requirement.assigned_resource_id, fixture["resources"]["B"])
                self.assertEqual(intent.target_resource_id, fixture["resources"]["B"])
                self.assertEqual(
                    intent.placement_not_before_date,
                    DAY + timedelta(days=3),
                )
            else:
                self.assertEqual(len(revisions), 2)
                new_revision = revisions[-1]
                self.assertEqual(new_revision.previous_revision_id, fixture["revision_id"])
                self.assertEqual(int(new_revision.request_version), 2)
                _, operational, requirement, intent = self._assert_common_final_state(
                    session,
                    fixture,
                    expected_revision_id=new_revision.id,
                    expected_request_version=2,
                    expected_operational_version=2,
                    expected_effective_need=Decimal("40.00"),
                    expected_coverage=Decimal("32.00"),
                )
                self.assertEqual(requirement.assigned_resource_id, fixture["resources"]["A"])
                self.assertEqual(intent.target_resource_id, fixture["resources"]["A"])
                self.assertIsNone(intent.placement_not_before_date)
                self.assertEqual(operational.approval_revision_id, new_revision.id)
            self._assert_seed_survivors(session, fixture)
            seeded_release = session.get(PlanningWorkQueueRelease, fixture["release_id"])
            self.assertEqual(
                seeded_release.approval_revision_id,
                fixture["revision_id"],
            )

    def test_drop_vs_permanent_reduction_has_one_authoritative_winner(self) -> None:
        fixture = self._fixture()
        participants = ("drop", "reduce")

        def reduce() -> tuple[str, str | None]:
            try:
                with fixture["factory"].begin() as session:
                    self._prepare_race_session(session, fixture, participants[1])
                    result = self._adapter(session, fixture).permanently_reduce_shift(
                        PermanentlyReduceShiftHoursCommand(
                            allocation_id=fixture["allocation_ids"][0],
                            expected_planning_version=int(fixture["planning_version"]),
                            expected_request_version=1,
                            expected_approval_revision_id=str(fixture["revision_id"]),
                            expected_operational_version=1,
                            command_id=f"reduce-race-{fixture['marker']}",
                        )
                    )
                return "success", str(result["reduction_id"])
            except ApplicationConflictError as exc:
                return self._expected_race_conflict(exc)

        outcomes = self._run_race(
            fixture,
            participants,
            lambda: self._drop(
                fixture,
                participant=participants[0],
                resource_id=fixture["resources"]["B"],
                dropped_date=DAY + timedelta(days=3),
                command_id=f"drop-reduction-race-{fixture['marker']}",
            ),
            reduce,
        )

        with fixture["factory"]() as session:
            drop_won = outcomes[0][0] == "success"
            if drop_won:
                _, _, requirement, intent = self._assert_common_final_state(
                    session,
                    fixture,
                    expected_revision_id=str(fixture["revision_id"]),
                    expected_request_version=1,
                    expected_operational_version=1,
                    expected_effective_need=Decimal("40.00"),
                    expected_coverage=Decimal("40.00"),
                )
                self.assertEqual(requirement.assigned_resource_id, fixture["resources"]["B"])
                self.assertEqual(intent.target_resource_id, fixture["resources"]["B"])
                self.assertEqual(
                    intent.placement_not_before_date,
                    DAY + timedelta(days=3),
                )
                self._assert_seed_survivors(session, fixture)
                self.assertEqual(
                    int(
                        session.scalar(
                            select(func.count(RequestCompletionReduction.id)).where(
                                RequestCompletionReduction.workforce_request_id
                                == fixture["request_id"]
                            )
                        )
                        or 0
                    ),
                    0,
                )
            else:
                _, _, requirement, intent = self._assert_common_final_state(
                    session,
                    fixture,
                    expected_revision_id=str(fixture["revision_id"]),
                    expected_request_version=2,
                    expected_operational_version=1,
                    expected_effective_need=Decimal("32.00"),
                    expected_coverage=Decimal("24.00"),
                )
                self.assertEqual(requirement.assigned_resource_id, fixture["resources"]["A"])
                self.assertEqual(intent.target_resource_id, fixture["resources"]["A"])
                self.assertIsNone(intent.placement_not_before_date)
                self._assert_seed_survivors(
                    session,
                    fixture,
                    missing_allocation_ids=(fixture["allocation_ids"][0],),
                )
                reductions = tuple(
                    session.scalars(
                        select(RequestCompletionReduction).where(
                            RequestCompletionReduction.workforce_request_id
                            == fixture["request_id"]
                        )
                    ).all()
                )
                self.assertEqual(len(reductions), 1)
                reduction = reductions[0]
                self.assertEqual(reduction.reconciliation_state, "ACTIVE")
                self.assertEqual(reduction.reduction_hours, Decimal("8.00"))
                self.assertEqual(reduction.effective_need_before, Decimal("40.00"))
                self.assertEqual(reduction.effective_need_after, Decimal("32.00"))
                self.assertEqual(Decimal("32.00") - Decimal("24.00"), Decimal("8.00"))


class Issue777SqlServerHarnessCleanupContractTests(unittest.TestCase):
    def _fixture_descriptor(self, factory, engine, marker: str) -> dict[str, object]:
        return {
            "marker": marker,
            "engine": engine,
            "factory": factory,
            "project_id": f"P-CLEAN-{marker}",
            "request_id": f"D-CLEAN-{marker}",
            "request_number": f"DMO-CLEAN-{marker}",
            "line_id": f"L-CLEAN-{marker}",
            "revision_id": f"REV-CLEAN-1-{marker}",
            "requirement_id": f"REQ-CLEAN-{marker}",
            "segment_id": f"SEG-CLEAN-{marker}",
            "intent_id": f"INT-CLEAN-{marker}",
            "actor_id": f"USR-CLEAN-{marker}",
            "actor_name": f"Admin cleanup {marker}",
            "scope_id": f"SCOPE-CLEAN-{marker}",
            "class_code": f"CLASSCLEAN{marker}".upper(),
            "resources": {
                "A": f"R-CLEAN-A-{marker}",
                "B": f"R-CLEAN-B-{marker}",
            },
            "availability_ids": {},
            "shift_ids": (f"S-CLEAN-{marker}",),
            "allocation_ids": (f"A-CLEAN-{marker}",),
            "release_id": f"REL-CLEAN-A-{marker}",
            "planning_version": None,
            "race_spids": {},
            "race_spid_lock": Lock(),
        }

    def _database(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "issue777-cleanup.db")
        engine = create_sql_engine(f"sqlite+pysqlite:///{path}")

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        return engine, create_session_factory(engine)

    def _seed_graph(
        self,
        fixture: dict[str, object],
        *,
        include_restorations: bool = True,
        include_revision_child: bool = True,
        include_reduction: bool = True,
    ) -> dict[str, str]:
        factory = fixture["factory"]
        request_id = str(fixture["request_id"])
        revision_id = str(fixture["revision_id"])
        child_revision_id = f"REV-CLEAN-2-{fixture['marker']}"
        second_release_id = f"REL-CLEAN-B-{fixture['marker']}"
        with factory.begin() as session:
            session.add_all(
                [
                    Project(
                        id=fixture["project_id"],
                        number=f"P-CLEAN-{fixture['marker']}",
                        name="Cleanup contract",
                    ),
                    AppUser(
                        id=fixture["actor_id"],
                        issuer="urn:resourceplanner:test",
                        subject=str(fixture["actor_id"]),
                        display_name=str(fixture["actor_name"]),
                        roles_json=json.dumps([ROLE_ADMIN]),
                        active=True,
                    ),
                    Resource(
                        id=fixture["resources"]["A"],
                        name=f"Cleanup A {fixture['marker']}",
                        active=True,
                        erp_active=True,
                    ),
                    Resource(
                        id=fixture["resources"]["B"],
                        name=f"Cleanup B {fixture['marker']}",
                        active=True,
                        erp_active=True,
                    ),
                ]
            )
            session.flush()
            session.add(
                WorkforceRequest(
                    id=request_id,
                    legacy_demand_number=fixture["request_number"],
                    project_id=fixture["project_id"],
                    requester_user_id=fixture["actor_id"],
                    requester_name=fixture["actor_name"],
                    desired_start=DAY,
                    desired_end=DAY,
                    resource_count=1,
                    estimated_hours=Decimal("8.00"),
                    status="En planification",
                    aggregate_version=2 if include_revision_child else 1,
                    line_mode=True,
                )
            )
            session.flush()
            session.add(
                RequestLine(
                    id=fixture["line_id"],
                    workforce_request_id=request_id,
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
                    id=revision_id,
                    workforce_request_id=request_id,
                    request_version=1,
                    approved_by_name=fixture["actor_name"],
                    approved_at=utc_now(),
                    provenance="APPROVAL",
                    payload_format_version=1,
                    payload_text="{}",
                    authorization_fingerprint=("1" * 64),
                )
            )
            session.flush()
            active_revision_id = revision_id
            if include_revision_child:
                session.add(
                    RequestApprovalRevision(
                        id=child_revision_id,
                        workforce_request_id=request_id,
                        previous_revision_id=revision_id,
                        request_version=2,
                        approved_by_name=fixture["actor_name"],
                        approved_at=utc_now(),
                        provenance="APPROVAL",
                        payload_format_version=1,
                        payload_text="{}",
                        authorization_fingerprint=("2" * 64),
                    )
                )
                session.flush()
                active_revision_id = child_revision_id
            session.add_all(
                [
                    RequestApprovalReference(
                        workforce_request_id=request_id,
                        active_revision_id=active_revision_id,
                        status=APPROVAL_REFERENCE_CAPTURED,
                    ),
                    RequestOperationalState(
                        workforce_request_id=request_id,
                        approval_revision_id=active_revision_id,
                        version=2 if include_revision_child else 1,
                        selections_text="{}",
                        confirmations_text="{}",
                        budget_overrides_text="{}",
                    ),
                    ResourceRequirement(
                        id=fixture["requirement_id"],
                        legacy_segment_id=fixture["segment_id"],
                        project_id=fixture["project_id"],
                        workforce_request_id=request_id,
                        source_request_line_id=fixture["line_id"],
                        approval_revision_id=active_revision_id,
                        approved_entry_key=EnvelopeEntryIdentity(
                            line_id=str(fixture["line_id"])
                        ).stable_key,
                        approval_reference_status=APPROVAL_REFERENCE_CAPTURED,
                        start_date=DAY,
                        end_date=DAY,
                        planned_hours=Decimal("8.00"),
                        status="Planifié",
                        planning_type="Flexible",
                        origin="REQUEST",
                        assigned_resource_id=fixture["resources"]["A"],
                    ),
                ]
            )
            session.flush()
            session.add(
                RequestRequirementPlanningIntent(
                    id=fixture["intent_id"],
                    slot_identity_key=f"cleanup-{fixture['marker']}",
                    workforce_request_id=request_id,
                    source_request_line_id=fixture["line_id"],
                    approval_revision_id=active_revision_id,
                    approved_entry_key=EnvelopeEntryIdentity(
                        line_id=str(fixture["line_id"])
                    ).stable_key,
                    slot_index=0,
                    current_resource_requirement_id=fixture["requirement_id"],
                    intent_mode="PRESERVE_EXISTING",
                    target_mode="EXPLICIT_RESOURCE",
                    target_resource_id=fixture["resources"]["A"],
                )
            )
            session.flush()
            session.add(
                Shift(
                    id=fixture["shift_ids"][0],
                    legacy_allocation_id=fixture["allocation_ids"][0],
                    resource_requirement_id=fixture["requirement_id"],
                    resource_id=fixture["resources"]["A"],
                    work_date=DAY,
                    hours=Decimal("8.00"),
                    allocation_type="Flexible",
                    source="AUTO",
                    locked=False,
                )
            )
            session.add_all(
                [
                    PlanningWorkQueueRelease(
                        id=fixture["release_id"],
                        planning_intent_id=fixture["intent_id"],
                        resource_requirement_id=fixture["requirement_id"],
                        original_shift_id=f"RETURN-A-{fixture['marker']}",
                        workforce_request_id=request_id,
                        approval_revision_id=revision_id,
                        approved_entry_key=EnvelopeEntryIdentity(
                            line_id=str(fixture["line_id"])
                        ).stable_key,
                        resource_id=fixture["resources"]["A"],
                        work_date=DAY,
                        hours=Decimal("4.00"),
                        shift_snapshot_text="{}",
                        actor_user_id=fixture["actor_id"],
                        command_id=f"release-a-{fixture['marker']}",
                        active=True,
                    ),
                    PlanningWorkQueueRelease(
                        id=second_release_id,
                        planning_intent_id=fixture["intent_id"],
                        resource_requirement_id=fixture["requirement_id"],
                        original_shift_id=f"RETURN-B-{fixture['marker']}",
                        workforce_request_id=request_id,
                        approval_revision_id=revision_id,
                        approved_entry_key=EnvelopeEntryIdentity(
                            line_id=str(fixture["line_id"])
                        ).stable_key,
                        resource_id=fixture["resources"]["A"],
                        work_date=DAY,
                        hours=Decimal("4.00"),
                        shift_snapshot_text="{}",
                        actor_user_id=fixture["actor_id"],
                        command_id=f"release-b-{fixture['marker']}",
                        active=False,
                    ),
                ]
            )
            session.flush()
            if include_restorations:
                session.add_all(
                    [
                        PlanningWorkQueueRestoration(
                            release_id=fixture["release_id"],
                            shift_id=f"RESTORED-A-{fixture['marker']}",
                            restored_hours=Decimal("2.00"),
                        ),
                        PlanningWorkQueueRestoration(
                            release_id=second_release_id,
                            shift_id=f"RESTORED-B-{fixture['marker']}",
                            restored_hours=Decimal("2.00"),
                        ),
                    ]
                )
            if include_reduction:
                session.add(
                    RequestCompletionReduction(
                        planning_intent_id=fixture["intent_id"],
                        workforce_request_id=request_id,
                        historical_approval_revision_id=revision_id,
                        source_request_line_id=fixture["line_id"],
                        approved_entry_key=EnvelopeEntryIdentity(
                            line_id=str(fixture["line_id"])
                        ).stable_key,
                        resource_requirement_id=fixture["requirement_id"],
                        original_shift_id=fixture["shift_ids"][0],
                        shift_snapshot_text="{}",
                        reduction_hours=Decimal("2.00"),
                        effective_need_before=Decimal("8.00"),
                        effective_need_after=Decimal("6.00"),
                        actor_user_id=fixture["actor_id"],
                        command_id=f"reduce-{fixture['marker']}",
                        reconciliation_state="ACTIVE",
                    )
                )
        return {
            "child_revision_id": child_revision_id,
            "second_release_id": second_release_id,
        }

    def _assert_fixture_gone_and_sentinel_present(
        self,
        fixture: dict[str, object],
        sentinel_project_id: str,
    ) -> None:
        with fixture["factory"]() as session:
            self.assertIsNone(session.get(WorkforceRequest, fixture["request_id"]))
            self.assertEqual(
                session.scalar(
                    select(func.count(RequestApprovalRevision.id)).where(
                        RequestApprovalRevision.workforce_request_id
                        == fixture["request_id"]
                    )
                ),
                0,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count(PlanningWorkQueueRelease.id)).where(
                        PlanningWorkQueueRelease.workforce_request_id
                        == fixture["request_id"]
                    )
                ),
                0,
            )
            self.assertIsNotNone(session.get(Project, sentinel_project_id))

    def _seed_sentinel(self, factory, marker: str) -> str:
        sentinel_project_id = f"P-SENTINEL-{marker}"
        with factory.begin() as session:
            session.add(
                Project(
                    id=sentinel_project_id,
                    number=f"P-SENTINEL-{marker}",
                    name="Unrelated sentinel",
                )
            )
        return sentinel_project_id

    def test_cleanup_handles_empty_restoration_table(self) -> None:
        engine, factory = self._database()
        fixture = self._fixture_descriptor(factory, engine, uuid4().hex[:8])
        sentinel = self._seed_sentinel(factory, str(fixture["marker"]))
        self._seed_graph(fixture, include_restorations=False)
        Issue777SqlServerConcurrencyTests._cleanup(self, fixture)
        self._assert_fixture_gone_and_sentinel_present(fixture, sentinel)

    def test_cleanup_handles_multiple_releases_restorations_and_reduction(self) -> None:
        engine, factory = self._database()
        fixture = self._fixture_descriptor(factory, engine, uuid4().hex[:8])
        sentinel = self._seed_sentinel(factory, str(fixture["marker"]))
        self._seed_graph(fixture)
        Issue777SqlServerConcurrencyTests._cleanup(self, fixture)
        self._assert_fixture_gone_and_sentinel_present(fixture, sentinel)

    def test_cleanup_deletes_revision_descendants_before_parent(self) -> None:
        engine, factory = self._database()
        fixture = self._fixture_descriptor(factory, engine, uuid4().hex[:8])
        sentinel = self._seed_sentinel(factory, str(fixture["marker"]))
        self._seed_graph(fixture, include_revision_child=True)
        Issue777SqlServerConcurrencyTests._cleanup(self, fixture)
        self._assert_fixture_gone_and_sentinel_present(fixture, sentinel)

    def test_cleanup_removes_reduction_evidence(self) -> None:
        engine, factory = self._database()
        fixture = self._fixture_descriptor(factory, engine, uuid4().hex[:8])
        sentinel = self._seed_sentinel(factory, str(fixture["marker"]))
        self._seed_graph(
            fixture,
            include_restorations=False,
            include_revision_child=False,
            include_reduction=True,
        )
        with factory() as session:
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(RequestCompletionReduction.id)).where(
                            RequestCompletionReduction.workforce_request_id
                            == fixture["request_id"]
                        )
                    )
                    or 0
                ),
                1,
            )
        Issue777SqlServerConcurrencyTests._cleanup(self, fixture)
        self._assert_fixture_gone_and_sentinel_present(fixture, sentinel)

    def test_cleanup_is_safe_before_fixture_commit(self) -> None:
        engine, factory = self._database()
        fixture = self._fixture_descriptor(factory, engine, uuid4().hex[:8])
        sentinel = self._seed_sentinel(factory, str(fixture["marker"]))
        Issue777SqlServerConcurrencyTests._cleanup(self, fixture)
        self._assert_fixture_gone_and_sentinel_present(fixture, sentinel)

    def test_cleanup_is_safe_after_post_commit_verification_failure(self) -> None:
        engine, factory = self._database()
        fixture = self._fixture_descriptor(factory, engine, uuid4().hex[:8])
        sentinel = self._seed_sentinel(factory, str(fixture["marker"]))
        self._seed_graph(fixture)
        try:
            raise AssertionError("verification failure simulée")
        except AssertionError:
            Issue777SqlServerConcurrencyTests._cleanup(self, fixture)
        self._assert_fixture_gone_and_sentinel_present(fixture, sentinel)

    def test_cleanup_is_idempotent(self) -> None:
        engine, factory = self._database()
        fixture = self._fixture_descriptor(factory, engine, uuid4().hex[:8])
        sentinel = self._seed_sentinel(factory, str(fixture["marker"]))
        self._seed_graph(fixture)
        Issue777SqlServerConcurrencyTests._cleanup(self, fixture)
        Issue777SqlServerConcurrencyTests._cleanup(self, fixture)
        self._assert_fixture_gone_and_sentinel_present(fixture, sentinel)

    def test_cleanup_preserves_unrelated_sentinel_data(self) -> None:
        engine, factory = self._database()
        fixture = self._fixture_descriptor(factory, engine, uuid4().hex[:8])
        sentinel = self._seed_sentinel(factory, str(fixture["marker"]))
        self._seed_graph(fixture)
        Issue777SqlServerConcurrencyTests._cleanup(self, fixture)
        self._assert_fixture_gone_and_sentinel_present(fixture, sentinel)



if __name__ == "__main__":
    unittest.main()
