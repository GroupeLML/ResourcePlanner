from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
import json
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.application import (
    ApplicationConflictError,
    PlanningWindowOverrideExtendCommand,
)
from app.application.security import ROLE_ADMIN, permissions_for_roles
from app.domain.approval_envelope import EnvelopeEntryIdentity
from app.infrastructure.sql import (
    PLANNING_WINDOW_OVERRIDE_ACTIVE,
    PlanningChangeHistory,
    PlanningWindowOverride,
    Project,
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
from app.infrastructure.sql.composite_allocation import (
    SqlCompositeAllocationCommandAdapter,
)
from app.infrastructure.sql.idempotency import CommandIdempotencyReceipt
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.planning_audit import SqlPlanningAuditJournal
from app.infrastructure.sql.planning_authorization_repository import (
    SqlRequestPlanningAuthorizationRepository,
)
from app.infrastructure.sql.planning_version import (
    SqlPlanningMutationVersionRepository,
)


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")
DAY = date(2026, 10, 6)


class _NoopPlanning:
    def rebuild(self):
        return {}


@unittest.skipUnless(
    IS_MSSQL,
    "Validation 613C réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class PlanningWindowOverrideSqlServerConcurrencyTests(unittest.TestCase):
    def test_same_planning_version_allows_only_one_override_command(self) -> None:
        marker = uuid4().hex[:10]
        project_id = f"P613C-{marker}"
        request_id = f"D613C-{marker}"
        request_number = f"DMO-613C-{marker}"
        revision_id = f"REV613C-{marker}"
        requirement_id = f"REQ613C-{marker}"
        segment_id = f"SEG613C-{marker}"
        actor_id = f"USR613C-{marker}"
        line_id = f"LINE613C-{marker}"
        entry_key = EnvelopeEntryIdentity(line_id=line_id).stable_key
        payload = json.dumps(
            {
                "authorization": {
                    "entries": [
                        {
                            "identity": entry_key,
                            "start_date": DAY.isoformat(),
                            "end_date": DAY.isoformat(),
                            "hours": "8.00",
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
                session.add(
                    Project(
                        id=project_id,
                        number=f"P-{marker}",
                        name="613C SQL Server",
                    )
                )
                session.add(
                    AppUser(
                        id=actor_id,
                        issuer="urn:resourceplanner:test",
                        subject=f"613c-{marker}",
                        display_name=f"Admin 613C {marker}",
                        roles_json=json.dumps([ROLE_ADMIN]),
                        active=True,
                    )
                )
                session.flush()
                session.add(
                    WorkforceRequest(
                        id=request_id,
                        legacy_demand_number=request_number,
                        project_id=project_id,
                        requester_user_id=actor_id,
                        requester_name=f"Admin 613C {marker}",
                        desired_start=DAY,
                        desired_end=DAY,
                        resource_count=1,
                        estimated_hours=Decimal("8.00"),
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
                        approved_by_name=f"Admin 613C {marker}",
                        approved_at=utc_now(),
                        provenance="APPROVAL",
                        payload_format_version=1,
                        payload_text=payload,
                        authorization_fingerprint=marker.ljust(64, "6"),
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
                    ResourceRequirement(
                        id=requirement_id,
                        legacy_segment_id=segment_id,
                        project_id=project_id,
                        workforce_request_id=request_id,
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
                    )
                )

            with factory() as session:
                expected_version = SqlPlanningMutationVersionRepository(
                    session
                ).current_version()

            def mutate(days: int, key: str) -> tuple[str, str | None]:
                with factory.begin() as session:
                    versioning = SqlPlanningMutationVersionRepository(session)
                    adapter = SqlCompositeAllocationCommandAdapter(
                        session,
                        planning=_NoopPlanning(),
                        authorization=SqlRequestPlanningAuthorizationRepository(
                            session,
                            actor_name=f"Admin 613C {marker}",
                            roles=(ROLE_ADMIN,),
                        ),
                        versioning=versioning,
                        journal=SqlPlanningAuditJournal(
                            session,
                            actor_name=f"Admin 613C {marker}",
                        ),
                        actor_user_id=actor_id,
                        permissions=permissions_for_roles((ROLE_ADMIN,)),
                    )
                    try:
                        result = adapter.extend_planning_window(
                            PlanningWindowOverrideExtendCommand(
                                segment_id=segment_id,
                                start_date=DAY,
                                end_date=DAY + timedelta(days=days),
                                reason=f"Course SQL 613C +{days}",
                                expected_planning_version=expected_version,
                                expected_approval_revision_id=revision_id,
                                idempotency_key=key,
                            )
                        )
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "success", str(result["override_id"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    mutate,
                    1,
                    f"613c-a-{marker}",
                )
                second = executor.submit(
                    mutate,
                    2,
                    f"613c-b-{marker}",
                )
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
                overrides = tuple(
                    session.scalars(
                        select(PlanningWindowOverride).where(
                            PlanningWindowOverride.workforce_request_id
                            == request_id,
                            PlanningWindowOverride.status
                            == PLANNING_WINDOW_OVERRIDE_ACTIVE,
                        )
                    ).all()
                )
                self.assertEqual(len(overrides), 1)
                self.assertIn(
                    overrides[0].effective_end_date,
                    {
                        DAY + timedelta(days=1),
                        DAY + timedelta(days=2),
                    },
                )
                requirement = session.get(
                    ResourceRequirement,
                    requirement_id,
                )
                self.assertIsNotNone(requirement)
                assert requirement is not None
                self.assertEqual(
                    requirement.end_date,
                    overrides[0].effective_end_date,
                )
                self.assertEqual(
                    SqlPlanningMutationVersionRepository(
                        session
                    ).current_version(),
                    expected_version + 1,
                )
                receipts = tuple(
                    session.scalars(
                        select(CommandIdempotencyReceipt).where(
                            CommandIdempotencyReceipt.actor_name == actor_id,
                            CommandIdempotencyReceipt.command_scope
                            == "planning_window_override.extend_segment",
                        )
                    ).all()
                )
                self.assertEqual(len(receipts), 1)
        finally:
            with factory.begin() as session:
                session.execute(
                    delete(PlanningChangeHistory).where(
                        PlanningChangeHistory.entity_reference == segment_id
                    )
                )
                session.execute(
                    delete(CommandIdempotencyReceipt).where(
                        CommandIdempotencyReceipt.actor_name == actor_id
                    )
                )
                session.execute(
                    delete(PlanningWindowOverride).where(
                        PlanningWindowOverride.workforce_request_id == request_id
                    )
                )
                session.execute(
                    delete(ResourceRequirement).where(
                        ResourceRequirement.id == requirement_id
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
                    delete(WorkforceRequest).where(
                        WorkforceRequest.id == request_id
                    )
                )
                session.execute(
                    delete(AppUser).where(AppUser.id == actor_id)
                )
                session.execute(
                    delete(Project).where(Project.id == project_id)
                )
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
