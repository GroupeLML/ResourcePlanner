from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
import json
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.application import ApplicationConflictError, SegmentDatedDropCommand
from app.application.security import ROLE_ADMIN, permissions_for_roles
from app.domain.approval_envelope import EnvelopeEntryIdentity
from app.infrastructure.sql import (
    PlanningChangeHistory,
    Project,
    RequestOperationalState,
    RequestRequirementPlanningIntent,
    Resource,
    ResourceRequirement,
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
from app.infrastructure.sql.composite_allocation import SqlCompositeAllocationCommandAdapter
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.planning_audit import SqlPlanningAuditJournal
from app.infrastructure.sql.planning_authorization_repository import (
    SqlRequestPlanningAuthorizationRepository,
)
from app.infrastructure.sql.planning_version import SqlPlanningMutationVersionRepository


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")
DAY = date(2026, 11, 2)


class _NoopPlanning:
    def rebuild(self, *, preserve_shift_ids=()):
        return {}


@unittest.skipUnless(
    IS_MSSQL,
    "Validation #777 réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class Issue777SqlServerConcurrencyTests(unittest.TestCase):
    def test_two_dated_drops_on_same_planning_version_have_one_winner(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P777-{marker}"
        request_id = f"D777-{marker}"
        request_number = f"DMO-777-{marker}"
        revision_id = f"REV777-{marker}"
        requirement_id = f"REQ777-{marker}"
        segment_id = f"SEG777-{marker}"
        actor_id = f"USR777-{marker}"
        line_id = f"LINE777-{marker}"
        resource_a = f"RA777-{marker}"
        resource_b = f"RB777-{marker}"
        intent_id = f"INT777-{marker}"
        entry_key = EnvelopeEntryIdentity(line_id=line_id).stable_key
        payload = json.dumps(
            {
                "authorization": {
                    "entries": [
                        {
                            "identity": entry_key,
                            "start_date": DAY.isoformat(),
                            "end_date": (DAY + timedelta(days=4)).isoformat(),
                            "hours": "40.00",
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
                session.add(Project(id=project_id, number=project_id, name="777 SQL Server"))
                session.add(
                    AppUser(
                        id=actor_id,
                        issuer="urn:resourceplanner:test",
                        subject=f"777-{marker}",
                        display_name=f"Admin 777 {marker}",
                        roles_json=json.dumps([ROLE_ADMIN]),
                        active=True,
                    )
                )
                session.add_all(
                    [
                        Resource(id=resource_a, name=f"A 777 {marker}", active=True),
                        Resource(id=resource_b, name=f"B 777 {marker}", active=True),
                    ]
                )
                session.flush()
                session.add(
                    WorkforceRequest(
                        id=request_id,
                        legacy_demand_number=request_number,
                        project_id=project_id,
                        requester_user_id=actor_id,
                        requester_name=f"Admin 777 {marker}",
                        desired_start=DAY,
                        desired_end=DAY + timedelta(days=4),
                        resource_count=1,
                        estimated_hours=Decimal("40.00"),
                        status="En planification",
                        aggregate_version=1,
                    )
                )
                session.flush()
                session.add(
                    RequestApprovalRevision(
                        id=revision_id,
                        workforce_request_id=request_id,
                        request_version=1,
                        approved_by_name=f"Admin 777 {marker}",
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
                        approval_revision_id=revision_id,
                        approved_entry_key=entry_key,
                        approval_reference_status=APPROVAL_REFERENCE_CAPTURED,
                        start_date=DAY,
                        end_date=DAY + timedelta(days=4),
                        planned_hours=Decimal("40.00"),
                        status="À assigner",
                        planning_type="Flexible",
                        confirmation="Confirmée",
                        origin="REQUEST",
                    )
                )
                session.flush()
                session.add(
                    RequestRequirementPlanningIntent(
                        id=intent_id,
                        slot_identity_key=f"777:{marker}:0",
                        workforce_request_id=request_id,
                        approval_revision_id=revision_id,
                        approved_entry_key=entry_key,
                        slot_index=0,
                        current_resource_requirement_id=requirement_id,
                        intent_mode="AUTO",
                        target_mode="INHERIT",
                    )
                )

            with factory() as session:
                expected_version = SqlPlanningMutationVersionRepository(session).current_version()

            def drop(resource_id: str, dropped: date) -> tuple[str, str | None]:
                with factory.begin() as session:
                    adapter = SqlCompositeAllocationCommandAdapter(
                        session,
                        planning=_NoopPlanning(),
                        authorization=SqlRequestPlanningAuthorizationRepository(
                            session,
                            actor_name=f"Admin 777 {marker}",
                            roles=(ROLE_ADMIN,),
                        ),
                        versioning=SqlPlanningMutationVersionRepository(session),
                        journal=SqlPlanningAuditJournal(
                            session,
                            actor_name=f"Admin 777 {marker}",
                        ),
                        actor_user_id=actor_id,
                        permissions=permissions_for_roles((ROLE_ADMIN,)),
                    )
                    try:
                        result = adapter.assign_segment_from_drop(
                            SegmentDatedDropCommand(
                                segment_id=segment_id,
                                resource_id=resource_id,
                                dropped_date=dropped,
                                expected_planning_version=expected_version,
                                expected_approval_revision_id=revision_id,
                                expected_operational_version=1,
                                correlation_id=f"777-{resource_id}",
                            )
                        )
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "success", str(result["placement_not_before_date"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(drop, resource_a, DAY + timedelta(days=2))
                second = executor.submit(drop, resource_b, DAY + timedelta(days=3))
                outcomes = (first.result(timeout=30), second.result(timeout=30))

            self.assertEqual(sorted(row[0] for row in outcomes), ["conflict", "success"])
            conflict = next(row for row in outcomes if row[0] == "conflict")
            self.assertEqual(conflict[1], "planning_version_conflict")

            with factory() as session:
                intent = session.get(RequestRequirementPlanningIntent, intent_id)
                self.assertIsNotNone(intent)
                self.assertIn(
                    intent.placement_not_before_date,
                    {DAY + timedelta(days=2), DAY + timedelta(days=3)},
                )
                state = session.get(RequestOperationalState, request_id)
                self.assertEqual(state.version, 2)
                self.assertEqual(
                    SqlPlanningMutationVersionRepository(session).current_version(),
                    expected_version + 1,
                )
        finally:
            with factory.begin() as session:
                session.execute(
                    delete(PlanningChangeHistory).where(
                        PlanningChangeHistory.entity_reference == segment_id
                    )
                )
                session.execute(
                    delete(RequestRequirementPlanningIntent).where(
                        RequestRequirementPlanningIntent.id == intent_id
                    )
                )
                session.execute(
                    delete(ResourceRequirement).where(ResourceRequirement.id == requirement_id)
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
                        RequestApprovalRevision.workforce_request_id == request_id
                    )
                )
                session.execute(delete(WorkforceRequest).where(WorkforceRequest.id == request_id))
                session.execute(delete(Resource).where(Resource.id.in_((resource_a, resource_b))))
                session.execute(delete(AppUser).where(AppUser.id == actor_id))
                session.execute(delete(Project).where(Project.id == project_id))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
