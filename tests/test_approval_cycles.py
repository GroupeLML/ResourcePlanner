from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import create_engine, delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.application.approval_cycles import ApprovalCycleService
from app.application.approval_scopes import ApprovalScopeService
from app.application.errors import ApplicationConflictError, ApplicationValidationError
from app.domain.approval_cycles import (
    APPROVAL_CYCLE_INIT_LEGACY_EXPLICIT,
    LEGACY_APPROVAL_CYCLE_APPROVED_HISTORICAL,
    LEGACY_APPROVAL_CYCLE_SUBMITTED_REQUIRES_INITIALIZATION,
    ApprovalSubjectRoutingEntry,
    approval_subject_fingerprint,
)
from app.domain.approval_routing import (
    APPROVER_SOURCE_RESOURCE,
    APPROVER_SOURCE_SCOPE,
    DIAGNOSTIC_NO_ELIGIBLE_APPROVER,
    DIAGNOSTIC_ROUTING_SOURCE_MISSING,
    DIAGNOSTIC_SCOPE_AMBIGUOUS,
)
from app.infrastructure.sql import (
    AppUser,
    ApprovalDecision,
    ApprovalRequirement,
    ApprovalRequirementApprover,
    ApprovalScope,
    ApprovalScopeApprover,
    AssetAllocation,
    Asset,
    AssetApprover,
    AssetType,
    AssetTypeApprovalScopeMapping,
    Base,
    BusinessContact,
    Project,
    RequestApprovalCycle,
    RequestApprovalRevision,
    RequestLine,
    Resource,
    ResourceClassApprovalScopeMapping,
    ResourceClassConfig,
    ResourceRequirement,
    Shift,
    SqlApprovalCycleRepository,
    SqlApprovalScopeRepository,
    TaskApprovalScopeMapping,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestHistory,
)
from app.infrastructure.sql.request_version import acquire_request_aggregate_version


DAY = date(2026, 10, 1)


class _ResourceAuthority:
    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self.mapping = mapping

    def list_approver_user_ids(self, resource_id: str) -> tuple[str, ...]:
        return self.mapping.get(resource_id, ())


class ApprovalCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        database_path = Path(self._temp.name) / "approval-cycles.db"
        self.engine = create_engine(
            "sqlite+pysqlite:///" + database_path.as_posix()
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )
        with self.factory() as session, session.begin():
            self._seed(session)

    def tearDown(self) -> None:
        self.engine.dispose()
        self._temp.cleanup()

    @staticmethod
    def _seed(session: Session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet"))
        session.add(
            Resource(
                id="R1",
                name="Ressource proposée",
                resource_class="AUT",
                active=True,
            )
        )
        for user_id in ("U1", "U2", "U3"):
            session.add(
                AppUser(
                    id=user_id,
                    issuer="urn:test",
                    subject=f"subject-{user_id}",
                    display_name=user_id,
                    roles_json=json.dumps(["MANAGER"]),
                    active=True,
                )
            )
        session.add_all(
            [
                TaskCatalogEntry(
                    id="T1",
                    project_number="P-1",
                    task_code="210",
                    label="Automatisation",
                ),
                TaskCatalogEntry(
                    id="T2",
                    project_number="P-1",
                    task_code="110",
                    label="Installation",
                ),
            ]
        )
        session.add(
            WorkforceRequest(
                id="D1",
                legacy_demand_number="DMO-2026-0001",
                project_id="P1",
                status="Soumise",
                priority="Normale",
                aggregate_version=1,
                line_mode=True,
            )
        )
        session.add_all(
            [
                RequestLine(
                    id="L1",
                    workforce_request_id="D1",
                    position=0,
                    kind="WORKFORCE",
                    desired_start=DAY,
                    desired_end=date(2026, 10, 3),
                    estimated_hours=16,
                    task_catalog_item_id="T1",
                    erp_task_code="210",
                    erp_task_label="Automatisation",
                    proposed_resource_id="R1",
                    required_resource_class="AUT",
                    active=True,
                ),
                RequestLine(
                    id="L2",
                    workforce_request_id="D1",
                    position=1,
                    kind="WORKFORCE",
                    desired_start=DAY,
                    desired_end=date(2026, 10, 2),
                    estimated_hours=8,
                    task_catalog_item_id="T2",
                    erp_task_code="110",
                    required_resource_class="ELEC",
                    active=True,
                ),
            ]
        )
        session.add_all(
            [
                ResourceClassConfig(
                    code="AUT",
                    label="Automatisation",
                    active=True,
                    version=1,
                ),
                ResourceClassConfig(
                    code="ELEC",
                    label="Électricité",
                    active=True,
                    version=1,
                ),
            ]
        )
        session.add_all(
            [
                ApprovalScope(
                    id="S1",
                    code="AUTOMATION",
                    label="Automatisation",
                    active=True,
                ),
                ApprovalScope(
                    id="S2",
                    code="ELECTRICAL_INSTALLATION",
                    label="Installation électrique",
                    active=True,
                ),
            ]
        )
        session.add_all(
            [
                ResourceClassApprovalScopeMapping(
                    resource_class_code="AUT",
                    approval_scope_id="S1",
                ),
                ResourceClassApprovalScopeMapping(
                    resource_class_code="ELEC",
                    approval_scope_id="S2",
                ),
                TaskApprovalScopeMapping(
                    task_catalog_item_id="T1",
                    approval_scope_id="S1",
                ),
                TaskApprovalScopeMapping(
                    task_catalog_item_id="T2",
                    approval_scope_id="S2",
                ),
                ApprovalScopeApprover(
                    approval_scope_id="S1",
                    app_user_id="U1",
                ),
                ApprovalScopeApprover(
                    approval_scope_id="S1",
                    app_user_id="U2",
                ),
                ApprovalScopeApprover(
                    approval_scope_id="S2",
                    app_user_id="U1",
                ),
            ]
        )

    @staticmethod
    def _service(
        session: Session,
        *,
        resource_authority: _ResourceAuthority | None = None,
    ) -> ApprovalCycleService:
        return ApprovalCycleService(
            SqlApprovalCycleRepository(
                session,
                actor_user_id="U1",
                actor_name="U1",
            ),
            ApprovalScopeService(
                SqlApprovalScopeRepository(session)
            ),
            resource_authority=resource_authority,
        )

    def test_cycle_snapshots_multiple_lines_approvers_and_provenance(self) -> None:
        with self.factory() as session:
            cycle = self._service(
                session,
                resource_authority=_ResourceAuthority(
                    {"R1": ("U1",)}
                ),
            ).initialize_cycle(
                "D1",
                expected_version=1,
            )
            session.commit()

            self.assertEqual(cycle.submitted_request_version, 1)
            self.assertEqual(cycle.state, "OPEN")
            self.assertEqual(len(cycle.requirements), 2)
            by_line = {
                row.request_line_id: row
                for row in cycle.requirements
            }
            self.assertEqual(
                [row.app_user_id for row in by_line["L1"].approvers],
                ["U1", "U2"],
            )
            self.assertEqual(
                [row.app_user_id for row in by_line["L2"].approvers],
                ["U1"],
            )
            u1 = next(
                row
                for row in by_line["L1"].approvers
                if row.app_user_id == "U1"
            )
            self.assertEqual(
                set(u1.sources),
                {
                    APPROVER_SOURCE_SCOPE,
                    APPROVER_SOURCE_RESOURCE,
                },
            )
            self.assertEqual(
                set(by_line["L1"].routing_sources),
                {
                    APPROVER_SOURCE_SCOPE,
                    APPROVER_SOURCE_RESOURCE,
                },
            )
            request = session.get(WorkforceRequest, "D1")
            self.assertEqual(request.aggregate_version, 2)

    def test_cycle_accepts_taskless_class_and_resource_routing_in_same_cycle(self) -> None:
        with self.factory() as session:
            line1 = session.get(RequestLine, "L1")
            line1.task_catalog_item_id = None
            line1.required_resource_class = None
            line1.proposed_resource_id = "R1"
            line2 = session.get(RequestLine, "L2")
            line2.task_catalog_item_id = None
            line2.required_resource_class = "ELEC"
            session.flush()

            cycle = self._service(session).initialize_cycle(
                "D1",
                expected_version=1,
            )
            session.commit()

            self.assertEqual(len(cycle.requirements), 2)
            by_line = {
                row.request_line_id: row
                for row in cycle.requirements
            }
            self.assertIsNone(by_line["L1"].task_catalog_item_id)
            self.assertIsNone(by_line["L2"].task_catalog_item_id)
            self.assertEqual(by_line["L1"].approval_scope_id, "S1")
            self.assertEqual(by_line["L2"].approval_scope_id, "S2")
            self.assertIn(
                "PROPOSED_RESOURCE_CLASS",
                by_line["L1"].routing_sources,
            )
            self.assertIn(
                "REQUIRED_RESOURCE_CLASS",
                by_line["L2"].routing_sources,
            )

    def test_unroutable_taskless_line_prevents_partial_cycle_creation(self) -> None:
        with self.factory() as session:
            line1 = session.get(RequestLine, "L1")
            line1.task_catalog_item_id = None
            line1.required_resource_class = "AUT"
            line2 = session.get(RequestLine, "L2")
            line2.task_catalog_item_id = None
            line2.required_resource_class = None
            line2.proposed_resource_id = None
            session.flush()

            with self.assertRaises(ApplicationValidationError):
                self._service(session).initialize_cycle(
                    "D1",
                    expected_version=1,
                )

            self.assertIsNone(
                session.scalar(
                    select(RequestApprovalCycle).where(
                        RequestApprovalCycle.workforce_request_id == "D1"
                    )
                )
            )

    def test_asset_cycle_snapshots_authority_and_tracks_proposed_asset_subject(self) -> None:
        with self.factory() as session, session.begin():
            session.add(
                AssetType(
                    id="AT1",
                    code="LAPTOP",
                    label="Portable",
                    category="EQUIPMENT",
                    active=True,
                )
            )
            session.flush()
            session.add_all(
                [
                    Asset(
                        id="A1",
                        code="LAPTOP-1",
                        label="Portable 1",
                        asset_type_id="AT1",
                        active=True,
                    ),
                    Asset(
                        id="A2",
                        code="LAPTOP-2",
                        label="Portable 2",
                        asset_type_id="AT1",
                        active=True,
                    ),
                    ApprovalScope(
                        id="S-ASSET",
                        code="ASSET_AUTOMATION",
                        label="Actifs automation",
                        active=True,
                    ),
                    WorkforceRequest(
                        id="D-ASSET",
                        legacy_demand_number="DMO-ASSET",
                        project_id="P1",
                        status="Soumise",
                        priority="Normale",
                        aggregate_version=1,
                        line_mode=True,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    RequestLine(
                        id="L-ASSET",
                        workforce_request_id="D-ASSET",
                        position=0,
                        kind="ASSET",
                        asset_type_id="AT1",
                        proposed_asset_id="A1",
                        desired_start=DAY,
                        desired_end=DAY,
                        active=True,
                    ),
                    AssetTypeApprovalScopeMapping(
                        asset_type_id="AT1",
                        approval_scope_id="S-ASSET",
                    ),
                    ApprovalScopeApprover(
                        approval_scope_id="S-ASSET",
                        app_user_id="U1",
                    ),
                    AssetApprover(
                        asset_id="A1",
                        app_user_id="U2",
                    ),
                ]
            )

        with self.factory() as session:
            service = self._service(session)
            cycle = service.initialize_cycle("D-ASSET", expected_version=1)
            session.commit()

            requirement = cycle.requirements[0]
            self.assertEqual(requirement.asset_type_id, "AT1")
            self.assertEqual(requirement.proposed_asset_id, "A1")
            self.assertIn("ASSET_TYPE", requirement.routing_sources)
            self.assertEqual(
                {row.app_user_id for row in requirement.approvers},
                {"U1", "U2"},
            )
            u2 = next(
                row for row in requirement.approvers
                if row.app_user_id == "U2"
            )
            self.assertEqual(u2.sources, ("PROPOSED_ASSET",))

            session.add(
                AssetApprover(asset_id="A1", app_user_id="U3")
            )
            session.commit()
            self.assertEqual(
                service.validate_active_cycle("D-ASSET").id,
                cycle.id,
            )

            line = session.get(RequestLine, "L-ASSET")
            line.proposed_asset_id = "A2"
            session.commit()
            with self.assertRaises(ApplicationConflictError) as context:
                service.validate_active_cycle("D-ASSET")
            self.assertEqual(
                context.exception.code,
                "approval_cycle_subject_changed",
            )

    def test_snapshot_does_not_change_when_scope_configuration_changes(self) -> None:
        with self.factory() as session:
            service = self._service(session)
            cycle = service.initialize_cycle("D1", expected_version=1)
            session.commit()
            original = {
                row.request_line_id: tuple(
                    (approver.app_user_id, approver.sources)
                    for approver in row.approvers
                )
                for row in cycle.requirements
            }

            scope = session.get(ApprovalScope, "S1")
            scope.label = "Libellé modifié"
            scope.active = False
            session.add(
                ApprovalScopeApprover(
                    approval_scope_id="S1",
                    app_user_id="U3",
                )
            )
            session.commit()

            refreshed = service.get_active_cycle("D1")
            self.assertEqual(
                {
                    row.request_line_id: tuple(
                        (approver.app_user_id, approver.sources)
                        for approver in row.approvers
                    )
                    for row in refreshed.requirements
                },
                original,
            )
            self.assertEqual(
                service.validate_active_cycle("D1").id,
                cycle.id,
            )

    def test_open_cycle_snapshot_ignores_later_resource_class_mapping_changes(self) -> None:
        with self.factory() as session:
            session.execute(delete(TaskApprovalScopeMapping))
            session.add_all(
                [
                    ResourceClassConfig(
                        code="RC-AUT",
                        label="Automation",
                        active=True,
                        version=1,
                    ),
                    ResourceClassConfig(
                        code="RC-ELEC",
                        label="Electrical",
                        active=True,
                        version=1,
                    ),
                    ResourceClassApprovalScopeMapping(
                        resource_class_code="RC-AUT",
                        approval_scope_id="S1",
                    ),
                    ResourceClassApprovalScopeMapping(
                        resource_class_code="RC-ELEC",
                        approval_scope_id="S2",
                    ),
                ]
            )
            session.get(TaskCatalogEntry, "T1").resource_class_code = "RC-AUT"
            session.get(TaskCatalogEntry, "T2").resource_class_code = "RC-ELEC"
            line1 = session.get(RequestLine, "L1")
            line1.required_resource_class = None
            line1.proposed_resource_id = None
            session.get(RequestLine, "L2").required_resource_class = None
            session.commit()

            service = self._service(session)
            cycle = service.initialize_cycle("D1", expected_version=1)
            session.commit()
            original = {
                row.request_line_id: (
                    row.approval_scope_id,
                    tuple(
                        (approver.app_user_id, approver.sources)
                        for approver in row.approvers
                    ),
                )
                for row in cycle.requirements
            }

            session.execute(
                delete(ResourceClassApprovalScopeMapping).where(
                    ResourceClassApprovalScopeMapping.resource_class_code
                    == "RC-AUT"
                )
            )
            session.add(
                ResourceClassApprovalScopeMapping(
                    resource_class_code="RC-AUT",
                    approval_scope_id="S2",
                )
            )
            session.commit()

            refreshed = service.get_active_cycle("D1")
            self.assertEqual(
                {
                    row.request_line_id: (
                        row.approval_scope_id,
                        tuple(
                            (approver.app_user_id, approver.sources)
                            for approver in row.approvers
                        ),
                    )
                    for row in refreshed.requirements
                },
                original,
            )
            self.assertEqual(
                service.validate_active_cycle("D1").id,
                cycle.id,
            )

    def test_fingerprint_is_deterministic_and_order_independent(self) -> None:
        entries = (
            {
                "identity": '["LINE","L2",null]',
                "competency_ids": ["C2", "C1"],
                "hours": "8",
            },
            {
                "identity": '["LINE","L1",null]',
                "competency_ids": [],
                "hours": "16",
            },
        )
        routing = (
            ApprovalSubjectRoutingEntry(
                request_line_id="L2",
                task_catalog_item_id="T2",
                approval_scope_ids=("S2",),
                source_kinds=(APPROVER_SOURCE_SCOPE,),
            ),
            ApprovalSubjectRoutingEntry(
                request_line_id="L1",
                task_catalog_item_id="T1",
                approval_scope_ids=("S1",),
                source_kinds=(
                    APPROVER_SOURCE_RESOURCE,
                    APPROVER_SOURCE_SCOPE,
                ),
                proposed_resource_id="R1",
            ),
        )
        first = approval_subject_fingerprint(
            request_id="D1",
            project_id="P1",
            priority="Normale",
            site_client=None,
            location=None,
            line_mode=True,
            authorization_entries=entries,
            routing_entries=routing,
        )
        second = approval_subject_fingerprint(
            request_id="D1",
            project_id="P1",
            priority="Normale",
            site_client=None,
            location=None,
            line_mode=True,
            authorization_entries=tuple(reversed(entries)),
            routing_entries=tuple(reversed(routing)),
        )
        changed = approval_subject_fingerprint(
            request_id="D1",
            project_id="P1",
            priority="Normale",
            site_client=None,
            location=None,
            line_mode=True,
            authorization_entries=(
                {**entries[0], "hours": "12"},
                entries[1],
            ),
            routing_entries=routing,
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_subject_change_invalidates_cycle_and_preserves_history(self) -> None:
        with self.factory() as session:
            service = self._service(session)
            cycle = service.initialize_cycle("D1", expected_version=1)
            session.commit()

            line = session.get(RequestLine, "L1")
            line.estimated_hours = 24
            session.commit()

            invalidated = service.invalidate_if_subject_changed(
                "D1",
                expected_version=2,
            )
            session.commit()

            self.assertEqual(invalidated.id, cycle.id)
            self.assertEqual(invalidated.state, "INVALIDATED")
            self.assertEqual(
                session.get(RequestApprovalCycle, cycle.id).state,
                "INVALIDATED",
            )
            self.assertEqual(
                session.scalar(
                    select(func.count(ApprovalRequirement.id)).where(
                        ApprovalRequirement.approval_cycle_id == cycle.id
                    )
                ),
                2,
            )
            self.assertIsNone(service.get_active_cycle("D1"))

    def test_aggregate_version_change_alone_does_not_invalidate_cycle(self) -> None:
        with self.factory() as session:
            service = self._service(session)
            cycle = service.initialize_cycle("D1", expected_version=1)
            session.commit()

            request = session.get(WorkforceRequest, "D1")
            acquire_request_aggregate_version(session, request, 2)
            session.commit()
            self.assertEqual(request.aggregate_version, 3)

            unchanged = service.invalidate_if_subject_changed(
                "D1",
                expected_version=3,
            )
            session.commit()
            self.assertEqual(unchanged.id, cycle.id)
            self.assertEqual(unchanged.state, "OPEN")
            self.assertEqual(
                session.get(WorkforceRequest, "D1").aggregate_version,
                3,
            )

    def test_request_cas_rejects_stale_concurrent_writer(self) -> None:
        first = self.factory()
        second = self.factory()
        try:
            request_first = first.get(WorkforceRequest, "D1")
            request_second = second.get(WorkforceRequest, "D1")
            self.assertEqual(request_first.aggregate_version, 1)
            self.assertEqual(request_second.aggregate_version, 1)

            acquire_request_aggregate_version(
                first,
                request_first,
                1,
            )
            first.commit()

            with self.assertRaises(ApplicationConflictError):
                acquire_request_aggregate_version(
                    second,
                    request_second,
                    1,
                )
            second.rollback()
        finally:
            first.close()
            second.close()

    def test_ambiguous_or_empty_routing_blocks_snapshot(self) -> None:
        with self.factory() as session:
            session.add(
                TaskApprovalScopeMapping(
                    task_catalog_item_id="T1",
                    approval_scope_id="S2",
                )
            )
            session.commit()
            with self.assertRaises(ApplicationValidationError) as ambiguous:
                self._service(session).initialize_cycle(
                    "D1",
                    expected_version=1,
                )
            self.assertEqual(
                ambiguous.exception.code,
                "approval_cycle_routing_blocked",
            )
            session.rollback()

        with self.factory() as session:
            session.execute(
                delete(TaskApprovalScopeMapping).where(
                    TaskApprovalScopeMapping.task_catalog_item_id == "T1"
                )
            )
            session.execute(
                delete(ResourceClassApprovalScopeMapping).where(
                    ResourceClassApprovalScopeMapping.resource_class_code
                    == "AUT"
                )
            )
            session.commit()
            with self.assertRaises(ApplicationValidationError) as unmapped:
                self._service(session).initialize_cycle(
                    "D1",
                    expected_version=1,
                )
            self.assertEqual(
                unmapped.exception.code,
                "approval_cycle_routing_blocked",
            )

    def test_blocked_routing_error_exposes_structured_line_task_and_scope_context(self) -> None:
        with self.factory() as session:
            session.add(
                TaskApprovalScopeMapping(
                    task_catalog_item_id="T1",
                    approval_scope_id="S2",
                )
            )
            session.commit()

            with self.assertRaises(ApplicationValidationError) as error:
                self._service(session).initialize_cycle(
                    "D1",
                    expected_version=1,
                )

            context = error.exception.context
            self.assertEqual(error.exception.code, "approval_cycle_routing_blocked")
            self.assertEqual(context["request_line_id"], "L1")
            self.assertEqual(context["request_line_position"], 0)
            self.assertEqual(context["task_catalog_item_id"], "T1")
            self.assertEqual(context["task_code"], "210")
            self.assertEqual(context["task_label"], "Automatisation")
            self.assertIsNone(context["approval_scope"])
            self.assertEqual(
                [scope["code"] for scope in context["approval_scope_candidates"]],
                ["AUTOMATION", "ELECTRICAL_INSTALLATION"],
            )
            self.assertIn(
                DIAGNOSTIC_SCOPE_AMBIGUOUS,
                context["diagnostics"],
            )

    def test_no_routing_source_reports_erp_snapshot_and_precise_reason(self) -> None:
        with self.factory() as session:
            line = session.get(RequestLine, "L1")
            line.task_catalog_item_id = None
            line.required_resource_class = None
            line.proposed_resource_id = None
            session.commit()

            with self.assertRaises(ApplicationValidationError) as error:
                self._service(session).initialize_cycle(
                    "D1",
                    expected_version=1,
                )

            context = error.exception.context
            self.assertEqual(context["request_line_id"], "L1")
            self.assertIsNone(context["task_catalog_item_id"])
            self.assertEqual(context["task_code"], "210")
            self.assertEqual(context["task_label"], "Automatisation")
            self.assertEqual(context["routing_sources"], [])
            self.assertIn(
                DIAGNOSTIC_ROUTING_SOURCE_MISSING,
                context["diagnostics"],
            )

    def test_no_eligible_approver_reports_effective_class_and_resolved_scope(self) -> None:
        with self.factory() as session:
            session.execute(
                delete(TaskApprovalScopeMapping).where(
                    TaskApprovalScopeMapping.task_catalog_item_id == "T1"
                )
            )
            session.add(
                ResourceClassConfig(
                    code="PROGRAMMEUR",
                    label="Programmeur",
                    average_hourly_cost_cad=100,
                    active=True,
                    version=1,
                )
            )
            session.add(
                ResourceClassApprovalScopeMapping(
                    resource_class_code="PROGRAMMEUR",
                    approval_scope_id="S1",
                )
            )
            session.get(TaskCatalogEntry, "T1").resource_class_code = "PROGRAMMEUR"
            line = session.get(RequestLine, "L1")
            line.required_resource_class = None
            line.proposed_resource_id = None
            session.execute(
                delete(ApprovalScopeApprover).where(
                    ApprovalScopeApprover.approval_scope_id == "S1"
                )
            )
            session.commit()

            with self.assertRaises(ApplicationValidationError) as error:
                self._service(session).initialize_cycle(
                    "D1",
                    expected_version=1,
                )

            context = error.exception.context
            self.assertEqual(context["task_code"], "210")
            self.assertEqual(context["effective_resource_class"], "PROGRAMMEUR")
            self.assertIsNone(context["proposed_resource_id"])
            self.assertEqual(context["approval_scope"]["id"], "S1")
            self.assertEqual(context["approval_scope"]["code"], "AUTOMATION")
            self.assertEqual(
                [scope["code"] for scope in context["approval_scope_candidates"]],
                ["AUTOMATION"],
            )
            self.assertIn(
                DIAGNOSTIC_NO_ELIGIBLE_APPROVER,
                context["diagnostics"],
            )

    def test_business_contact_never_confers_approval_authority(self) -> None:
        with self.factory() as session:
            session.execute(delete(ApprovalScopeApprover))
            session.add(
                BusinessContact(
                    id="C1",
                    display_name="Coordonnateur métier",
                    active=True,
                )
            )
            task = session.get(TaskCatalogEntry, "T1")
            task.coordinator_contact_id = "C1"
            task.approver_name = "U1"
            session.commit()

            with self.assertRaises(ApplicationValidationError) as error:
                self._service(session).initialize_cycle(
                    "D1",
                    expected_version=1,
                )
            self.assertEqual(
                error.exception.code,
                "approval_cycle_routing_blocked",
            )

    def test_schema_uniqueness_for_requirement_and_approver(self) -> None:
        with self.factory() as session:
            cycle = self._service(session).initialize_cycle(
                "D1",
                expected_version=1,
            )
            session.commit()
            requirement = cycle.requirements[0]

            with self.assertRaises(IntegrityError):
                session.execute(
                    insert(ApprovalRequirement).values(
                        id="DUP-REQ",
                        approval_cycle_id=cycle.id,
                        request_line_id=requirement.request_line_id,
                        task_catalog_item_id=requirement.task_catalog_item_id,
                        approval_scope_id=requirement.approval_scope_id,
                        routing_sources_text="[]",
                    )
                )
                session.commit()
            session.rollback()

            approver = requirement.approvers[0]
            with self.assertRaises(IntegrityError):
                session.execute(
                    insert(ApprovalRequirementApprover).values(
                        requirement_id=requirement.id,
                        app_user_id=approver.app_user_id,
                        sources_text='["APPROVAL_SCOPE"]',
                    )
                )
                session.commit()
            session.rollback()

    def test_legacy_requests_are_detected_without_reconstructing_votes(self) -> None:
        with self.factory() as session:
            service = self._service(session)
            self.assertEqual(
                service.legacy_compatibility_state("D1"),
                LEGACY_APPROVAL_CYCLE_SUBMITTED_REQUIRES_INITIALIZATION,
            )
            self.assertEqual(
                session.scalar(select(func.count(ApprovalDecision.id))),
                0,
            )

            approved = WorkforceRequest(
                id="D-APPROVED",
                project_id="P1",
                status="En planification",
                approved_by_name="Nom historique",
                approved_at=datetime.now(timezone.utc),
                aggregate_version=1,
            )
            session.add(approved)
            session.commit()

            self.assertEqual(
                service.legacy_compatibility_state("D-APPROVED"),
                LEGACY_APPROVAL_CYCLE_APPROVED_HISTORICAL,
            )
            with self.assertRaises(ApplicationValidationError):
                service.initialize_cycle(
                    "D-APPROVED",
                    expected_version=1,
                )
            self.assertEqual(
                session.scalar(select(func.count(ApprovalDecision.id))),
                0,
            )

    def test_legacy_submitted_initialization_is_explicit_and_audited(self) -> None:
        with self.factory() as session:
            service = self._service(session)
            cycle = service.initialize_cycle(
                "D1",
                expected_version=1,
                initialization_reason=APPROVAL_CYCLE_INIT_LEGACY_EXPLICIT,
            )
            session.commit()

            history = session.scalar(
                select(WorkforceRequestHistory)
                .where(
                    WorkforceRequestHistory.workforce_request_id == "D1",
                    WorkforceRequestHistory.action
                    == "Initialisation cycle approbation",
                )
                .order_by(WorkforceRequestHistory.occurred_at.desc())
            )
            self.assertIsNotNone(history)
            details = json.loads(history.details)
            self.assertEqual(
                details["initialization_reason"],
                APPROVAL_CYCLE_INIT_LEGACY_EXPLICIT,
            )
            self.assertEqual(
                details["approval_cycle_id"],
                cycle.id,
            )

    def test_cycle_creation_has_no_planning_or_approval_revision_side_effects(self) -> None:
        with self.factory() as session:
            self._service(session).initialize_cycle(
                "D1",
                expected_version=1,
            )
            session.commit()

            for model in (
                ResourceRequirement,
                Shift,
                AssetAllocation,
                RequestApprovalRevision,
                ApprovalDecision,
            ):
                self.assertEqual(
                    session.scalar(select(func.count(model.id))),
                    0,
                    model.__name__,
                )


if __name__ == "__main__":
    unittest.main()
