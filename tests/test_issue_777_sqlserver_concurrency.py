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
    PlaceSegmentFromDropCommand,
    ReturnShiftToWorkQueueCommand,
)
from app.application.security import ROLE_ADMIN, permissions_for_roles
from app.domain.approval_envelope import EnvelopeEntryIdentity
from app.infrastructure.sql import (
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
from app.infrastructure.sql.planning_version import SqlPlanningMutationVersionRepository
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
            planning_version = SqlPlanningMutationVersionRepository(
                session
            ).current_version()

        return {
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
            "planning_version": planning_version,
        }

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

    def _drop(
        self,
        fixture: dict[str, object],
        *,
        resource_id: str,
        dropped_date: date,
        command_id: str,
    ) -> tuple[str, str | None]:
        factory = fixture["factory"]
        with factory.begin() as session:
            try:
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
            except ApplicationConflictError as exc:
                return "conflict", exc.code
            return "success", str(result["placement_not_before_date"])

    def _cleanup(self, fixture: dict[str, object]) -> None:
        factory = fixture["factory"]
        with factory.begin() as session:
            session.execute(
                delete(PlanningChangeHistory).where(
                    PlanningChangeHistory.actor_name == str(fixture["actor_name"])
                )
            )
            session.execute(
                delete(PlanningWorkQueueRestoration).where(
                    PlanningWorkQueueRestoration.planning_intent_id
                    == str(fixture["intent_id"])
                )
            )
            session.execute(
                delete(PlanningWorkQueueRelease).where(
                    PlanningWorkQueueRelease.planning_intent_id
                    == str(fixture["intent_id"])
                )
            )
            session.execute(
                delete(RequestCompletionReduction).where(
                    RequestCompletionReduction.planning_intent_id
                    == str(fixture["intent_id"])
                )
            )
            session.execute(
                delete(Shift).where(
                    Shift.resource_requirement_id == str(fixture["requirement_id"])
                )
            )
            session.execute(
                delete(RequestRequirementPlanningIntent).where(
                    RequestRequirementPlanningIntent.id == str(fixture["intent_id"])
                )
            )
            session.execute(
                delete(ResourceRequirement).where(
                    ResourceRequirement.id == str(fixture["requirement_id"])
                )
            )
            session.execute(
                delete(RequestOperationalState).where(
                    RequestOperationalState.workforce_request_id
                    == str(fixture["request_id"])
                )
            )
            session.execute(
                delete(RequestApprovalReference).where(
                    RequestApprovalReference.workforce_request_id
                    == str(fixture["request_id"])
                )
            )
            session.execute(
                delete(RequestApprovalRevision).where(
                    RequestApprovalRevision.workforce_request_id
                    == str(fixture["request_id"])
                )
            )
            session.execute(
                delete(RequestLine).where(
                    RequestLine.id == str(fixture["line_id"])
                )
            )
            session.execute(
                delete(WorkforceRequestHistory).where(
                    WorkforceRequestHistory.workforce_request_id
                    == str(fixture["request_id"])
                )
            )
            session.execute(
                delete(WorkforceRequest).where(
                    WorkforceRequest.id == str(fixture["request_id"])
                )
            )
            for availability_id in fixture["availability_ids"].values():
                session.execute(
                    delete(ResourceAvailabilityRule).where(
                        ResourceAvailabilityRule.id == availability_id
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
                delete(AppUser).where(
                    AppUser.id == str(fixture["actor_id"])
                )
            )
            session.execute(
                delete(Resource).where(
                    Resource.id.in_(tuple(fixture["resources"].values()))
                )
            )
            session.execute(
                delete(Project).where(Project.id == str(fixture["project_id"]))
            )
        fixture["engine"].dispose()

    def test_drop_vs_drop_has_one_authoritative_winner(self) -> None:
        fixture = self._fixture()
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    self._drop,
                    fixture,
                    resource_id=fixture["resources"]["A"],
                    dropped_date=DAY + timedelta(days=2),
                    command_id=f"drop-a-{fixture['marker']}",
                )
                second = executor.submit(
                    self._drop,
                    fixture,
                    resource_id=fixture["resources"]["B"],
                    dropped_date=DAY + timedelta(days=3),
                    command_id=f"drop-b-{fixture['marker']}",
                )
                outcomes = (first.result(timeout=30), second.result(timeout=30))
            self.assertEqual(
                sorted(row[0] for row in outcomes),
                ["conflict", "success"],
            )
            self.assertEqual(
                next(row[1] for row in outcomes if row[0] == "conflict"),
                "planning_version_conflict",
            )
            with fixture["factory"]() as session:
                intent = session.get(
                    RequestRequirementPlanningIntent,
                    fixture["intent_id"],
                )
                self.assertIn(
                    intent.placement_not_before_date,
                    (DAY + timedelta(days=2), DAY + timedelta(days=3)),
                )
        finally:
            self._cleanup(fixture)

    def test_drop_vs_return_shift_has_one_authoritative_winner(self) -> None:
        fixture = self._fixture()
        try:
            def return_shift() -> tuple[str, str | None]:
                with fixture["factory"].begin() as session:
                    try:
                        result = self._adapter(session, fixture).return_shift(
                            ReturnShiftToWorkQueueCommand(
                                allocation_id=fixture["allocation_ids"][0],
                                expected_planning_version=int(fixture["planning_version"]),
                                expected_approval_revision_id=str(fixture["revision_id"]),
                                expected_operational_version=1,
                                command_id=f"return-race-{fixture['marker']}",
                            )
                        )
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "success", str(result["release_id"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                drop = executor.submit(
                    self._drop,
                    fixture,
                    resource_id=fixture["resources"]["B"],
                    dropped_date=DAY + timedelta(days=3),
                    command_id=f"drop-return-race-{fixture['marker']}",
                )
                returned = executor.submit(return_shift)
                outcomes = (drop.result(timeout=30), returned.result(timeout=30))
            self.assertEqual(
                sorted(row[0] for row in outcomes),
                ["conflict", "success"],
            )
            self.assertEqual(
                next(row[1] for row in outcomes if row[0] == "conflict"),
                "planning_version_conflict",
            )
        finally:
            self._cleanup(fixture)

    def test_drop_vs_reapproval_has_one_authoritative_winner(self) -> None:
        fixture = self._fixture()
        try:
            def reapprove() -> tuple[str, str | None]:
                with fixture["factory"].begin() as session:
                    versioning = SqlPlanningMutationVersionRepository(session)
                    try:
                        versioning.acquire(int(fixture["planning_version"]))
                        request = session.get(WorkforceRequest, fixture["request_id"])
                        acquire_request_aggregate_version(session, request, 1)
                        SqlPeriodAwareApprovedDemandSyncAdapter(
                            session
                        ).sync_approved(
                            str(fixture["request_number"]),
                            approved_request_version=2,
                        )
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "success", "reapproved"

            with ThreadPoolExecutor(max_workers=2) as executor:
                drop = executor.submit(
                    self._drop,
                    fixture,
                    resource_id=fixture["resources"]["B"],
                    dropped_date=DAY + timedelta(days=3),
                    command_id=f"drop-reapproval-race-{fixture['marker']}",
                )
                approved = executor.submit(reapprove)
                outcomes = (drop.result(timeout=30), approved.result(timeout=30))
            self.assertEqual(
                sorted(row[0] for row in outcomes),
                ["conflict", "success"],
            )
            self.assertEqual(
                next(row[1] for row in outcomes if row[0] == "conflict"),
                "planning_version_conflict",
            )
        finally:
            self._cleanup(fixture)

    def test_drop_vs_permanent_reduction_has_one_authoritative_winner(self) -> None:
        fixture = self._fixture()
        try:
            def reduce() -> tuple[str, str | None]:
                with fixture["factory"].begin() as session:
                    try:
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
                    except ApplicationConflictError as exc:
                        return "conflict", exc.code
                    return "success", str(result["reduction_id"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                drop = executor.submit(
                    self._drop,
                    fixture,
                    resource_id=fixture["resources"]["B"],
                    dropped_date=DAY + timedelta(days=3),
                    command_id=f"drop-reduction-race-{fixture['marker']}",
                )
                reduced = executor.submit(reduce)
                outcomes = (drop.result(timeout=30), reduced.result(timeout=30))
            self.assertEqual(
                sorted(row[0] for row in outcomes),
                ["conflict", "success"],
            )
            self.assertEqual(
                next(row[1] for row in outcomes if row[0] == "conflict"),
                "planning_version_conflict",
            )
        finally:
            self._cleanup(fixture)


if __name__ == "__main__":
    unittest.main()
