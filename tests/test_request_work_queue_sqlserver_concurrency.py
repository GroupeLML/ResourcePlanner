from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import json
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.application.errors import ApplicationConflictError
from app.application.request_work_queue import ReturnShiftToWorkQueueCommand
from app.application.security import ROLE_ADMIN, permissions_for_roles
from app.infrastructure.sql import (
    PlanningChangeHistory,
    PlanningWorkQueueRelease,
    Project,
    RequestOperationalState,
    RequestRequirementPlanningIntent,
    RequestLine,
    Resource,
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
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.planning_audit import SqlPlanningAuditJournal
from app.infrastructure.sql.planning_version import SqlPlanningMutationVersionRepository
from app.infrastructure.sql.request_work_queue_commands import SqlRequestWorkQueueCommandAdapter


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


if __name__ == "__main__":
    unittest.main()
