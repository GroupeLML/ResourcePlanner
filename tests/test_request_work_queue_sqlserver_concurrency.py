from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta
from decimal import Decimal
import json
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.application.errors import ApplicationConflictError
from app.application.request_work_queue import (
    PermanentlyReduceShiftHoursCommand,
    ReturnShiftToWorkQueueCommand,
)
from app.application.security import ROLE_ADMIN, permissions_for_roles
from app.infrastructure.sql import (
    PlanningChangeHistory,
    PlanningWorkQueueRelease,
    RequestCompletionReduction,
    SqlPeriodAwareApprovedDemandSyncAdapter,
    Project,
    RequestOperationalState,
    RequestRequirementPlanningIntent,
    RequestLine,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.approval_cycle_models import (
    ApprovalRequirement,
    ApprovalRequirementApprover,
    RequestApprovalCycle,
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
from app.infrastructure.sql.command_adapters import SqlPlanningCommandAdapter
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.planning_audit import SqlPlanningAuditJournal
from app.infrastructure.sql.planning_version import SqlPlanningMutationVersionRepository
from app.infrastructure.sql.planning_intent_repository import SqlRequestRequirementPlanningIntentRepository
from app.infrastructure.sql.query_repository import SqlPlannerQueryRepositoryWithLoadProfiles
from app.infrastructure.sql.resource_class_models import ResourceClassConfig
from app.infrastructure.sql.request_work_queue_commands import SqlRequestWorkQueueCommandAdapter
from app.infrastructure.sql.request_version import acquire_request_aggregate_version


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")
DAY = date(2026, 10, 29)


class _NoopPlanning:
    def rebuild(self, *, preserve_shift_ids=()):
        return {
            "segments": 1,
            "allocations": len(tuple(preserve_shift_ids)),
            "locked_allocations": 0,
            "requested_hours": 8,
            "allocated_hours": 0,
            "unallocated_hours": 8,
            "planning_engine": "sqlserver-concurrency",
        }


@unittest.skipUnless(
    IS_MSSQL,
    "Validation #778 réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class RequestWorkQueueSqlServerConcurrencyTests(unittest.TestCase):
    def test_same_planning_version_allows_only_one_return_shift_command(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P778-{marker}"
        request_id = f"D778-{marker}"
        request_number = f"DMO-778-{marker}"
        line_id = f"L778-{marker}"
        revision_id = f"REV778-{marker}"
        requirement_id = f"REQ778-{marker}"
        segment_id = f"SEG778-{marker}"
        resource_id = f"R778-{marker}"
        shift_id = f"S778-{marker}"
        allocation_id = f"A778-{marker}"
        intent_id = f"INT778-{marker}"
        actor_id = f"USR778-{marker}"
        entry_key = json.dumps(["LINE", line_id, ""], separators=(",", ":"))
        payload = json.dumps(
            {
                "authorization": {
                    "entries": [
                        {
                            "identity": entry_key,
                            "hours": "8.00",
                            "slot_count": 1,
                            "start_date": DAY.isoformat(),
                            "end_date": DAY.isoformat(),
                        }
                    ]
                }
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
                        Project(id=project_id, number=f"P-{marker}", name="778 SQL Server"),
                        Resource(
                            id=resource_id,
                            name=f"Ressource 778 {marker}",
                            active=True,
                            erp_active=True,
                        ),
                        AppUser(
                            id=actor_id,
                            issuer="urn:resourceplanner:test",
                            subject=actor_id,
                            display_name=f"Admin 778 {marker}",
                            roles_json=json.dumps([ROLE_ADMIN]),
                            active=True,
                        ),
                    ]
                )
                session.flush()
                session.add(
                    WorkforceRequest(
                        id=request_id,
                        legacy_demand_number=request_number,
                        project_id=project_id,
                        requester_user_id=actor_id,
                        requester_name=f"Admin 778 {marker}",
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
                        id=line_id,
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
                        approved_by_name=f"Admin 778 {marker}",
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
                        end_date=DAY,
                        planned_hours=Decimal("8.00"),
                        status="Planifié",
                        planning_type="Flexible",
                        confirmation="Confirmée",
                        origin="REQUEST",
                        assigned_resource_id=resource_id,
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
                        intent_mode="AUTO",
                        target_mode="INHERIT",
                    )
                )
                session.add(
                    Shift(
                        id=shift_id,
                        legacy_allocation_id=allocation_id,
                        resource_requirement_id=requirement_id,
                        resource_id=resource_id,
                        work_date=DAY,
                        hours=Decimal("8.00"),
                        source="AUTO",
                        locked=False,
                    )
                )

            with factory() as session:
                expected_version = SqlPlanningMutationVersionRepository(
                    session
                ).current_version()

            def mutate(key: str) -> tuple[str, str | None]:
                with factory.begin() as session:
                    adapter = SqlRequestWorkQueueCommandAdapter(
                        session,
                        planning=_NoopPlanning(),
                        versioning=SqlPlanningMutationVersionRepository(session),
                        journal=SqlPlanningAuditJournal(
                            session,
                            actor_name=f"Admin 778 {marker}",
                        ),
                        actor_user_id=actor_id,
                        actor_name=f"Admin 778 {marker}",
                        permissions=permissions_for_roles((ROLE_ADMIN,)),
                        roles=(ROLE_ADMIN,),
                    )
                    try:
                        result = adapter.return_shift(
                            ReturnShiftToWorkQueueCommand(
                                allocation_id=allocation_id,
                                expected_planning_version=expected_version,
                                expected_approval_revision_id=revision_id,
                                expected_operational_version=1,
                                command_id=key,
                            )
                        )
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "success", str(result["release_id"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(mutate, f"return-a-{marker}")
                second = executor.submit(mutate, f"return-b-{marker}")
                outcomes = (
                    first.result(timeout=30),
                    second.result(timeout=30),
                )

            self.assertEqual(
                sorted(row[0] for row in outcomes),
                ["conflict", "success"],
            )
            conflict = next(row for row in outcomes if row[0] == "conflict")
            self.assertEqual(conflict[1], "planning_version_conflict")

            with factory() as session:
                self.assertIsNone(session.get(Shift, shift_id))
                releases = tuple(
                    session.scalars(
                        select(PlanningWorkQueueRelease).where(
                            PlanningWorkQueueRelease.planning_intent_id == intent_id
                        )
                    ).all()
                )
                self.assertEqual(len(releases), 1)
                self.assertEqual(releases[0].original_shift_id, shift_id)
                self.assertEqual(releases[0].hours, Decimal("8.00"))
                self.assertEqual(
                    SqlPlanningMutationVersionRepository(session).current_version(),
                    expected_version + 1,
                )
        finally:
            with factory.begin() as session:
                session.execute(
                    delete(PlanningChangeHistory).where(
                        PlanningChangeHistory.entity_reference.in_(
                            (allocation_id, segment_id)
                        )
                    )
                )
                session.execute(
                    delete(PlanningWorkQueueRelease).where(
                        PlanningWorkQueueRelease.planning_intent_id == intent_id
                    )
                )
                session.execute(
                    delete(Shift).where(Shift.id == shift_id)
                )
                session.execute(
                    delete(RequestRequirementPlanningIntent).where(
                        RequestRequirementPlanningIntent.id == intent_id
                    )
                )
                session.execute(
                    delete(ResourceRequirement).where(
                        ResourceRequirement.id == requirement_id
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
                session.execute(
                    delete(RequestApprovalRevision).where(
                        RequestApprovalRevision.id == revision_id
                    )
                )
                session.execute(
                    delete(RequestLine).where(RequestLine.id == line_id)
                )
                session.execute(
                    delete(WorkforceRequest).where(
                        WorkforceRequest.id == request_id
                    )
                )
                session.execute(
                    delete(AppUser).where(AppUser.id == actor_id)
                )
                session.execute(
                    delete(Resource).where(Resource.id == resource_id)
                )
                session.execute(
                    delete(Project).where(Project.id == project_id)
                )
            engine.dispose()


    def test_permanent_reduction_race_creates_one_32h_reduction_and_resync_stays_deterministic(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P778R-{marker}"
        request_id = f"D778R-{marker}"
        request_number = f"DMO-778R-{marker}"
        line_id = f"L778R-{marker}"
        revision_id = f"REV778R-{marker}"
        requirement_id = f"REQ778R-{marker}"
        segment_id = f"SEG778R-{marker}"
        resource_id = f"R778R-{marker}"
        actor_id = f"USR778R-{marker}"
        intent_id = f"INT778R-{marker}"
        scope_id = f"SCOPE778R-{marker}"
        class_code = f"CLASS778R-{marker}".upper()
        availability_id = f"AV778R-{marker}"
        cycle_id = f"CYCLE778R-{marker}"
        approval_requirement_id = f"AR778R-{marker}"
        entry_key = json.dumps(["LINE", line_id, ""], separators=(",", ":"))
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
                    "authorization_fingerprint": marker.ljust(64, "8"),
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
            sort_keys=True,
            separators=(",", ":"),
        )

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        shift_ids = tuple(f"S778R-{marker}-{index}" for index in range(5))
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        Project(id=project_id, number=f"P-R-{marker}", name="778 reduction SQL"),
                        Resource(
                            id=resource_id,
                            name=f"Ressource 778 réduction {marker}",
                            active=True,
                            erp_active=True,
                        ),
                        AppUser(
                            id=actor_id,
                            issuer="urn:resourceplanner:test",
                            subject=actor_id,
                            display_name=f"Admin 778 réduction {marker}",
                            roles_json=json.dumps([ROLE_ADMIN]),
                            active=True,
                        ),
                        ApprovalScope(
                            id=scope_id,
                            code=f"SCOPE_{marker}".upper(),
                            label="Scope 778 réduction",
                            active=True,
                            version=1,
                        ),
                        ResourceClassConfig(
                            code=class_code,
                            label=f"Classe SQL 778 {marker}",
                            average_hourly_cost_cad=Decimal("100.00"),
                            active=True,
                            version=1,
                        ),
                    ]
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
                session.add(
                    ResourceAvailabilityRule(
                        id=availability_id,
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
                        requester_name=f"Admin 778 réduction {marker}",
                        desired_start=DAY,
                        desired_end=DAY + timedelta(days=4),
                        resource_count=1,
                        estimated_hours=Decimal("40.00"),
                        status="En planification",
                        aggregate_version=1,
                        line_mode=True,
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
                        confirmation="Confirmée",
                        required_resource_class=class_code,
                        active=True,
                    )
                )
                session.add(
                    RequestApprovalRevision(
                        id=revision_id,
                        workforce_request_id=request_id,
                        request_version=1,
                        approved_by_name=f"Admin 778 réduction {marker}",
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
                        assigned_resource_id=resource_id,
                    )
                )
                session.flush()
                session.add(
                    RequestRequirementPlanningIntent(
                        id=intent_id,
                        slot_identity_key=json.dumps(
                            [request_id, entry_key, 0],
                            separators=(",", ":"),
                        ),
                        workforce_request_id=request_id,
                        source_request_line_id=line_id,
                        approval_revision_id=revision_id,
                        approved_entry_key=entry_key,
                        slot_index=0,
                        current_resource_requirement_id=requirement_id,
                        intent_mode="AUTO",
                        target_mode="INHERIT",
                    )
                )
                for index, shift_id in enumerate(shift_ids):
                    session.add(
                        Shift(
                            id=shift_id,
                            legacy_allocation_id=f"A778R-{marker}-{index}",
                            resource_requirement_id=requirement_id,
                            resource_id=resource_id,
                            work_date=DAY + timedelta(days=index),
                            hours=Decimal("8.00"),
                            source="AUTO",
                            locked=False,
                        )
                    )
                session.add(
                    RequestApprovalCycle(
                        id=cycle_id,
                        workforce_request_id=request_id,
                        submitted_request_version=1,
                        state="COMPLETED",
                        subject_fingerprint=marker.ljust(64, "9"),
                        initialization_reason="SUBMISSION",
                        submitted_at=utc_now(),
                        completed_at=utc_now(),
                        approved_revision_id=revision_id,
                    )
                )
                session.flush()
                session.add(
                    ApprovalRequirement(
                        id=approval_requirement_id,
                        approval_cycle_id=cycle_id,
                        request_line_id=line_id,
                        approval_scope_id=scope_id,
                        routing_sources_text="[]",
                    )
                )
                session.flush()
                session.add(
                    ApprovalRequirementApprover(
                        requirement_id=approval_requirement_id,
                        app_user_id=actor_id,
                        sources_text='["TEST"]',
                    )
                )

            with factory() as session:
                expected_planning = SqlPlanningMutationVersionRepository(session).current_version()

            def reduce(key: str) -> tuple[str, str | None]:
                with factory.begin() as session:
                    adapter = SqlRequestWorkQueueCommandAdapter(
                        session,
                        versioning=SqlPlanningMutationVersionRepository(session),
                        journal=SqlPlanningAuditJournal(
                            session,
                            actor_name=f"Admin 778 réduction {marker}",
                        ),
                        actor_user_id=actor_id,
                        actor_name=f"Admin 778 réduction {marker}",
                        permissions=permissions_for_roles((ROLE_ADMIN,)),
                        roles=(ROLE_ADMIN,),
                    )
                    try:
                        result = adapter.permanently_reduce_shift(
                            PermanentlyReduceShiftHoursCommand(
                                allocation_id=f"A778R-{marker}-3",
                                expected_planning_version=expected_planning,
                                expected_request_version=1,
                                expected_approval_revision_id=revision_id,
                                expected_operational_version=1,
                                command_id=key,
                            )
                        )
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "success", str(result["reduction_id"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(reduce, f"reduce-a-{marker}")
                second = executor.submit(reduce, f"reduce-b-{marker}")
                outcomes = (
                    first.result(timeout=30),
                    second.result(timeout=30),
                )

            self.assertEqual(
                sorted(row[0] for row in outcomes),
                ["conflict", "success"],
            )
            self.assertEqual(
                next(row for row in outcomes if row[0] == "conflict")[1],
                "planning_version_conflict",
            )

            with factory.begin() as session:
                reductions = session.scalars(
                    select(RequestCompletionReduction).where(
                        RequestCompletionReduction.planning_intent_id == intent_id
                    )
                ).all()
                self.assertEqual(len(reductions), 1)
                self.assertEqual(reductions[0].reduction_hours, Decimal("8.00"))
                requirement = session.get(ResourceRequirement, requirement_id)
                self.assertEqual(requirement.planned_hours, Decimal("32.00"))
                survivors = session.scalars(
                    select(Shift).where(
                        Shift.resource_requirement_id == requirement_id
                    )
                ).all()
                self.assertEqual(len(survivors), 4)
                self.assertEqual(
                    sum((row.hours for row in survivors), Decimal("0.00")),
                    Decimal("32.00"),
                )
                self.assertEqual(
                    {row.work_date for row in survivors},
                    {
                        DAY,
                        DAY + timedelta(days=1),
                        DAY + timedelta(days=2),
                        DAY + timedelta(days=4),
                    },
                )
                current = SqlPlannerQueryRepositoryWithLoadProfiles(session).get_segment(
                    requirement_id
                )
                self.assertIsNotNone(current)
                self.assertEqual(current.planned_hours, 32.0)
                self.assertEqual(current.covered_hours, 32.0)
                self.assertEqual(current.remaining_hours, 0.0)

                SqlPeriodAwareApprovedDemandSyncAdapter(
                    session
                ).sync_operational_choices(request_number)
                self.assertEqual(requirement.planned_hours, Decimal("32.00"))
                self.assertEqual(
                    session.query(RequestCompletionReduction)
                    .filter_by(planning_intent_id=intent_id)
                    .count(),
                    1,
                )
        finally:
            with factory.begin() as session:
                session.execute(
                    delete(PlanningChangeHistory).where(
                        PlanningChangeHistory.entity_reference.in_(
                            tuple(f"A778R-{marker}-{index}" for index in range(5))
                            + (segment_id,)
                        )
                    )
                )
                session.execute(
                    delete(RequestCompletionReduction).where(
                        RequestCompletionReduction.planning_intent_id == intent_id
                    )
                )
                session.execute(delete(Shift).where(Shift.id.in_(shift_ids)))
                session.execute(
                    delete(RequestRequirementPlanningIntent).where(
                        RequestRequirementPlanningIntent.id == intent_id
                    )
                )
                session.execute(
                    delete(ResourceRequirement).where(
                        ResourceRequirement.id == requirement_id
                    )
                )
                session.execute(
                    delete(ApprovalRequirementApprover).where(
                        ApprovalRequirementApprover.requirement_id == approval_requirement_id
                    )
                )
                session.execute(
                    delete(ApprovalRequirement).where(
                        ApprovalRequirement.id == approval_requirement_id
                    )
                )
                session.execute(
                    delete(RequestApprovalCycle).where(
                        RequestApprovalCycle.id == cycle_id
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
                session.execute(
                    delete(RequestApprovalRevision).where(
                        RequestApprovalRevision.id == revision_id
                    )
                )
                session.execute(delete(RequestLine).where(RequestLine.id == line_id))
                session.execute(
                    delete(WorkforceRequestHistory).where(
                        WorkforceRequestHistory.workforce_request_id == request_id
                    )
                )
                session.execute(
                    delete(WorkforceRequest).where(
                        WorkforceRequest.id == request_id
                    )
                )
                session.execute(
                    delete(ResourceAvailabilityRule).where(
                        ResourceAvailabilityRule.id == availability_id
                    )
                )
                session.execute(
                    delete(ResourceClassApprovalScopeMapping).where(
                        ResourceClassApprovalScopeMapping.resource_class_code == class_code,
                        ResourceClassApprovalScopeMapping.approval_scope_id == scope_id,
                    )
                )
                session.execute(
                    delete(ApprovalScopeApprover).where(
                        ApprovalScopeApprover.approval_scope_id == scope_id,
                        ApprovalScopeApprover.app_user_id == actor_id,
                    )
                )
                session.execute(delete(ApprovalScope).where(ApprovalScope.id == scope_id))
                session.execute(
                    delete(ResourceClassConfig).where(
                        ResourceClassConfig.code == class_code
                    )
                )
                session.execute(delete(AppUser).where(AppUser.id == actor_id))
                session.execute(delete(Resource).where(Resource.id == resource_id))
                session.execute(delete(Project).where(Project.id == project_id))
            engine.dispose()



    def test_permanent_reduction_vs_compatible_reapproval_has_one_authoritative_winner(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P778X-{marker}"
        request_id = f"D778X-{marker}"
        request_number = f"DMO-778X-{marker}"
        line_id = f"L778X-{marker}"
        revision_id = f"REV778X-{marker}"
        requirement_id = f"REQ778X-{marker}"
        segment_id = f"SEG778X-{marker}"
        resource_id = f"R778X-{marker}"
        actor_id = f"USR778X-{marker}"
        intent_id = f"INT778X-{marker}"
        scope_id = f"SCOPE778X-{marker}"
        class_code = f"CLASS778X-{marker}".upper()
        availability_id = f"AV778X-{marker}"
        cycle_id = f"CYCLE778X-{marker}"
        approval_requirement_id = f"AR778X-{marker}"
        entry_key = json.dumps(["LINE", line_id, ""], separators=(",", ":"))
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
                    "authorization_fingerprint": marker.ljust(64, "a"),
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
            sort_keys=True,
            separators=(",", ":"),
        )
        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        shift_ids = tuple(f"S778X-{marker}-{index}" for index in range(5))
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        Project(id=project_id, number=f"P-X-{marker}", name="778 reapproval race SQL"),
                        Resource(
                            id=resource_id,
                            name=f"Ressource 778 race {marker}",
                            resource_class=class_code,
                            active=True,
                            erp_active=True,
                        ),
                        AppUser(
                            id=actor_id,
                            issuer="urn:resourceplanner:test",
                            subject=actor_id,
                            display_name=f"Admin 778 race {marker}",
                            roles_json=json.dumps([ROLE_ADMIN]),
                            active=True,
                        ),
                        ApprovalScope(
                            id=scope_id,
                            code=f"SCOPE_X_{marker}".upper(),
                            label="Scope 778 race",
                            active=True,
                            version=1,
                        ),
                        ResourceClassConfig(
                            code=class_code,
                            label=f"Classe SQL race 778 {marker}",
                            average_hourly_cost_cad=Decimal("100.00"),
                            active=True,
                            version=1,
                        ),
                    ]
                )
                session.flush()
                session.add_all(
                    [
                        ApprovalScopeApprover(
                            approval_scope_id=scope_id,
                            app_user_id=actor_id,
                        ),
                        ResourceClassApprovalScopeMapping(
                            resource_class_code=class_code,
                            approval_scope_id=scope_id,
                        ),
                        ResourceAvailabilityRule(
                            id=availability_id,
                            resource_id=resource_id,
                            availability_type="Horaire standard",
                            weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                            start_time=time(8, 0),
                            end_time=time(16, 0),
                            active=True,
                        ),
                    ]
                )
                session.add(
                    WorkforceRequest(
                        id=request_id,
                        legacy_demand_number=request_number,
                        project_id=project_id,
                        requester_user_id=actor_id,
                        requester_name=f"Admin 778 race {marker}",
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
                        approved_by_name=f"Admin 778 race {marker}",
                        approved_at=utc_now(),
                        provenance="APPROVAL",
                        payload_format_version=1,
                        payload_text=payload,
                        authorization_fingerprint=marker.ljust(64, "a"),
                    )
                )
                session.flush()
                session.add_all(
                    [
                        RequestApprovalReference(
                            workforce_request_id=request_id,
                            active_revision_id=revision_id,
                            status=APPROVAL_REFERENCE_CAPTURED,
                        ),
                        RequestOperationalState(
                            workforce_request_id=request_id,
                            approval_revision_id=revision_id,
                            version=1,
                            selections_text="{}",
                            confirmations_text="{}",
                            budget_overrides_text="{}",
                        ),
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
                            assigned_resource_id=resource_id,
                        ),
                    ]
                )
                session.flush()
                session.add(
                    RequestRequirementPlanningIntent(
                        id=intent_id,
                        slot_identity_key=json.dumps(
                            [request_id, entry_key, 0],
                            separators=(",", ":"),
                        ),
                        workforce_request_id=request_id,
                        source_request_line_id=line_id,
                        approval_revision_id=revision_id,
                        approved_entry_key=entry_key,
                        slot_index=0,
                        current_resource_requirement_id=requirement_id,
                        intent_mode="AUTO",
                        target_mode="INHERIT",
                    )
                )
                for index, shift_id in enumerate(shift_ids):
                    session.add(
                        Shift(
                            id=shift_id,
                            legacy_allocation_id=f"A778X-{marker}-{index}",
                            resource_requirement_id=requirement_id,
                            resource_id=resource_id,
                            work_date=DAY + timedelta(days=index),
                            hours=Decimal("8.00"),
                            source="AUTO",
                            locked=False,
                        )
                    )
                session.add(
                    RequestApprovalCycle(
                        id=cycle_id,
                        workforce_request_id=request_id,
                        submitted_request_version=1,
                        state="COMPLETED",
                        subject_fingerprint=marker.ljust(64, "b"),
                        initialization_reason="SUBMISSION",
                        submitted_at=utc_now(),
                        completed_at=utc_now(),
                        approved_revision_id=revision_id,
                    )
                )
                session.flush()
                session.add(
                    ApprovalRequirement(
                        id=approval_requirement_id,
                        approval_cycle_id=cycle_id,
                        request_line_id=line_id,
                        approval_scope_id=scope_id,
                        routing_sources_text='["REQUIRED_RESOURCE_CLASS"]',
                    )
                )
                session.flush()
                session.add(
                    ApprovalRequirementApprover(
                        requirement_id=approval_requirement_id,
                        app_user_id=actor_id,
                        sources_text='["SCOPE"]',
                    )
                )

            with factory() as session:
                expected_planning = SqlPlanningMutationVersionRepository(
                    session
                ).current_version()

            def reduce() -> tuple[str, str | None]:
                with factory.begin() as session:
                    try:
                        result = SqlRequestWorkQueueCommandAdapter(
                            session,
                            versioning=SqlPlanningMutationVersionRepository(session),
                            journal=SqlPlanningAuditJournal(
                                session,
                                actor_name=f"Admin 778 race {marker}",
                            ),
                            actor_user_id=actor_id,
                            actor_name=f"Admin 778 race {marker}",
                            permissions=permissions_for_roles((ROLE_ADMIN,)),
                            roles=(ROLE_ADMIN,),
                        ).permanently_reduce_shift(
                            PermanentlyReduceShiftHoursCommand(
                                allocation_id=f"A778X-{marker}-3",
                                expected_planning_version=expected_planning,
                                expected_request_version=1,
                                expected_approval_revision_id=revision_id,
                                expected_operational_version=1,
                                command_id=f"reduce-race-{marker}",
                            )
                        )
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "reduction", str(result["reduction_id"])

            def reapprove() -> tuple[str, str | None]:
                with factory.begin() as session:
                    versioning = SqlPlanningMutationVersionRepository(session)
                    try:
                        versioning.acquire(expected_planning)
                        request = session.get(WorkforceRequest, request_id)
                        acquire_request_aggregate_version(
                            session,
                            request,
                            1,
                        )
                        SqlPeriodAwareApprovedDemandSyncAdapter(
                            session
                        ).sync_approved(
                            request_number,
                            approved_request_version=2,
                        )
                        SqlPlanningCommandAdapter(
                            session,
                            versioning=versioning,
                        ).rebuild()
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "reapproval", None

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(reduce)
                second = executor.submit(reapprove)
                outcomes = (
                    first.result(timeout=45),
                    second.result(timeout=45),
                )

            self.assertEqual(
                sorted(row[0] for row in outcomes),
                ["conflict", next(row[0] for row in outcomes if row[0] != "conflict")],
            )
            self.assertEqual(sum(1 for row in outcomes if row[0] == "conflict"), 1)

            with factory.begin() as session:
                reductions = session.scalars(
                    select(RequestCompletionReduction).where(
                        RequestCompletionReduction.workforce_request_id == request_id
                    )
                ).all()
                self.assertLessEqual(len(reductions), 1)
                requirement = session.get(ResourceRequirement, requirement_id)
                self.assertIsNotNone(requirement)
                effective = SqlRequestRequirementPlanningIntentRepository(
                    session
                ).current_effective_need_for_requirement(
                    requirement_id,
                    fallback_hours=requirement.planned_hours,
                )
                if reductions:
                    self.assertEqual(effective, Decimal("32.00"))
                    self.assertEqual(requirement.planned_hours, Decimal("32.00"))
                    self.assertEqual(
                        sum(
                            session.scalars(
                                select(Shift.hours).where(
                                    Shift.resource_requirement_id == requirement_id
                                )
                            ).all(),
                            Decimal("0.00"),
                        ),
                        Decimal("32.00"),
                    )
                else:
                    self.assertEqual(effective, Decimal("40.00"))
                    reference = session.get(RequestApprovalReference, request_id)
                    self.assertIsNotNone(reference)
                    self.assertNotEqual(reference.active_revision_id, revision_id)
                    self.assertEqual(
                        sum(
                            session.scalars(
                                select(Shift.hours).where(
                                    Shift.resource_requirement_id == requirement_id
                                )
                            ).all(),
                            Decimal("0.00"),
                        ),
                        Decimal("40.00"),
                    )
        finally:
            with factory.begin() as session:
                session.execute(
                    delete(PlanningChangeHistory).where(
                        PlanningChangeHistory.entity_reference.in_(
                            tuple(f"A778X-{marker}-{index}" for index in range(5))
                            + (segment_id,)
                        )
                    )
                )
                session.execute(
                    delete(RequestCompletionReduction).where(
                        RequestCompletionReduction.workforce_request_id == request_id
                    )
                )
                session.execute(
                    delete(Shift).where(
                        Shift.resource_requirement_id == requirement_id
                    )
                )
                session.execute(
                    delete(RequestRequirementPlanningIntent).where(
                        RequestRequirementPlanningIntent.workforce_request_id == request_id
                    )
                )
                session.execute(
                    delete(ResourceRequirement).where(
                        ResourceRequirement.id == requirement_id
                    )
                )
                session.execute(
                    delete(ApprovalRequirementApprover).where(
                        ApprovalRequirementApprover.requirement_id == approval_requirement_id
                    )
                )
                session.execute(
                    delete(ApprovalRequirement).where(
                        ApprovalRequirement.id == approval_requirement_id
                    )
                )
                session.execute(
                    delete(RequestApprovalCycle).where(
                        RequestApprovalCycle.id == cycle_id
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
                newer_revision_ids = tuple(
                    session.scalars(
                        select(RequestApprovalRevision.id).where(
                            RequestApprovalRevision.workforce_request_id == request_id,
                            RequestApprovalRevision.id != revision_id,
                        )
                    ).all()
                )
                if newer_revision_ids:
                    session.execute(
                        delete(RequestApprovalRevision).where(
                            RequestApprovalRevision.id.in_(newer_revision_ids)
                        )
                    )
                session.execute(
                    delete(RequestApprovalRevision).where(
                        RequestApprovalRevision.id == revision_id
                    )
                )
                session.execute(delete(RequestLine).where(RequestLine.id == line_id))
                session.execute(
                    delete(WorkforceRequestHistory).where(
                        WorkforceRequestHistory.workforce_request_id == request_id
                    )
                )
                session.execute(
                    delete(WorkforceRequest).where(
                        WorkforceRequest.id == request_id
                    )
                )
                session.execute(
                    delete(ResourceAvailabilityRule).where(
                        ResourceAvailabilityRule.id == availability_id
                    )
                )
                session.execute(
                    delete(ResourceClassApprovalScopeMapping).where(
                        ResourceClassApprovalScopeMapping.resource_class_code == class_code,
                        ResourceClassApprovalScopeMapping.approval_scope_id == scope_id,
                    )
                )
                session.execute(
                    delete(ApprovalScopeApprover).where(
                        ApprovalScopeApprover.approval_scope_id == scope_id,
                        ApprovalScopeApprover.app_user_id == actor_id,
                    )
                )
                session.execute(delete(ApprovalScope).where(ApprovalScope.id == scope_id))
                session.execute(
                    delete(ResourceClassConfig).where(
                        ResourceClassConfig.code == class_code
                    )
                )
                session.execute(delete(AppUser).where(AppUser.id == actor_id))
                session.execute(delete(Resource).where(Resource.id == resource_id))
                session.execute(delete(Project).where(Project.id == project_id))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
