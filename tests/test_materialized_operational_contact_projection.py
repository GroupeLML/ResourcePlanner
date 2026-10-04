from __future__ import annotations

from datetime import date
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.operational_contacts import OperationalContactService
from app.domain.operational_contacts import (
    DIAGNOSTIC_APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN,
    SOURCE_REQUEST_OVERRIDE,
    SOURCE_REQUIREMENT_OVERRIDE,
    SOURCE_RESOURCE_COORDINATOR,
    SOURCE_SHIFT_OVERRIDE,
    SOURCE_TASK_RESPONSIBLE,
    STATUS_INACTIVE,
    STATUS_RESOLVED,
    STATUS_UNRESOLVED,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    SqlOperationalContactRepository,
    TaskCatalogEntry,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class MaterializedOperationalContactProjectionTests(unittest.TestCase):
    def _database(self, directory: str) -> str:
        path = Path(directory) / "operational-contact-projection.db"
        url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    BusinessContact(
                        id="C-PM",
                        display_name="Jean PM",
                        email="configured-jean",
                        phone="555-0100",
                    ),
                    BusinessContact(
                        id="C-TASK-APPROVED",
                        display_name="Marc approuvé",
                        email="configured-marc",
                        phone="555-0200",
                    ),
                    BusinessContact(
                        id="C-TASK-CURRENT",
                        display_name="Nouveau responsable",
                        email="configured-new",
                        phone="555-0300",
                    ),
                    BusinessContact(
                        id="C-CURRENT-OVERRIDE",
                        display_name="Override courant",
                        email="configured-override",
                        phone="555-0400",
                    ),
                    BusinessContact(
                        id="C-RREQ",
                        display_name="Coord besoin",
                        email="configured-req",
                        phone="555-0500",
                    ),
                    BusinessContact(
                        id="C-RSHIFT",
                        display_name="Coord quart",
                        email="configured-shift",
                        phone="555-0600",
                    ),
                    BusinessContact(
                        id="C-REQ-OVERRIDE",
                        display_name="Responsable besoin",
                        email="configured-req-override",
                        phone="555-0700",
                    ),
                    BusinessContact(
                        id="C-SHIFT-OVERRIDE",
                        display_name="Responsable quart",
                        email="configured-shift-override",
                        phone="555-0800",
                    ),
                    AppUser(
                        id="U-PM",
                        issuer="urn:test",
                        subject="pm",
                        display_name="Jean PM",
                        email="configured-jean",
                        employee_external_id="EMP-PM",
                        business_contact_id="C-PM",
                        roles_json='["PROJECT_MANAGER"]',
                        active=True,
                    ),
                    Project(
                        id="P1",
                        number="P-1",
                        name="Projet 1",
                        project_manager_external_id="EMP-PM",
                        project_manager_name="Jean PM",
                        project_manager_contact_id="C-PM",
                    ),
                    TaskCatalogEntry(
                        id="T-APPROVED",
                        project_number="P-1",
                        task_code="210",
                        label="Tâche approuvée",
                        operational_responsible_contact_id="C-TASK-APPROVED",
                        active=True,
                    ),
                    TaskCatalogEntry(
                        id="T-CURRENT",
                        project_number="P-1",
                        task_code="220",
                        label="Tâche courante",
                        operational_responsible_contact_id="C-TASK-CURRENT",
                        active=True,
                    ),
                    Resource(
                        id="R-PROPOSED",
                        name="Ressource proposée",
                        active=True,
                    ),
                    Resource(
                        id="R-REQ",
                        name="Ressource du besoin",
                        coordinator_contact_id="C-RREQ",
                        active=True,
                    ),
                    Resource(
                        id="R-SHIFT",
                        name="Ressource du quart",
                        coordinator_contact_id="C-RSHIFT",
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add(
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DMO-289F-1",
                    project_id="P1",
                    operational_responsible_override_contact_id="C-CURRENT-OVERRIDE",
                    aggregate_version=4,
                    line_mode=True,
                )
            )
            session.flush()
            session.add(
                RequestLine(
                    id="L1",
                    workforce_request_id="D1",
                    position=0,
                    task_catalog_item_id="T-CURRENT",
                    erp_task_code="220",
                    erp_task_label="Tâche courante",
                    proposed_resource_id="R-PROPOSED",
                )
            )
            session.add_all(
                [
                    ResourceRequirement(
                        id="REQ-CAPTURED",
                        project_id="P1",
                        workforce_request_id="D1",
                        source_request_line_id="L1",
                        approved_task_catalog_item_id="T-APPROVED",
                        approved_request_version=3,
                        approved_contact_context_status="CAPTURED",
                        captured_operational_responsible_contact_id="C-TASK-APPROVED",
                        captured_operational_responsible_source_type="TASK_RESPONSIBLE",
                        captured_operational_responsible_source_entity_id="T-APPROVED",
                        captured_operational_responsible_status="RESOLVED",
                        captured_operational_responsible_diagnostics="[]",
                        operational_responsibility_context_provenance="APPROVAL_CAPTURE",
                        operational_responsibility_context_version=1,
                        assigned_resource_id="R-REQ",
                        start_date=date(2026, 9, 21),
                        end_date=date(2026, 9, 21),
                        planned_hours=8,
                        origin="REQUEST",
                    ),
                    ResourceRequirement(
                        id="REQ-MIGRATED",
                        project_id="P1",
                        workforce_request_id="D1",
                        source_request_line_id="L1",
                        approved_task_catalog_item_id="T-APPROVED",
                        approved_request_version=2,
                        approved_contact_context_status="CAPTURED",
                        captured_operational_responsible_contact_id="C-TASK-APPROVED",
                        captured_operational_responsible_source_type="TASK_RESPONSIBLE",
                        captured_operational_responsible_source_entity_id="T-APPROVED",
                        captured_operational_responsible_status=None,
                        operational_responsibility_context_provenance="MIGRATION_OBSERVED",
                        operational_responsibility_context_version=1,
                        assigned_resource_id="R-REQ",
                        start_date=date(2026, 9, 20),
                        end_date=date(2026, 9, 20),
                        planned_hours=8,
                        origin="REQUEST",
                    ),
                    ResourceRequirement(
                        id="REQ-LEGACY",
                        project_id="P1",
                        workforce_request_id="D1",
                        source_request_line_id="L1",
                        approved_contact_context_status="LEGACY_UNKNOWN",
                        assigned_resource_id="R-REQ",
                        start_date=date(2026, 9, 22),
                        end_date=date(2026, 9, 22),
                        planned_hours=8,
                        origin="REQUEST",
                    ),
                    ResourceRequirement(
                        id="REQ-LEGACY-NO-COORD",
                        project_id="P1",
                        workforce_request_id="D1",
                        source_request_line_id="L1",
                        approved_contact_context_status="LEGACY_UNKNOWN",
                        assigned_resource_id="R-PROPOSED",
                        start_date=date(2026, 9, 23),
                        end_date=date(2026, 9, 23),
                        planned_hours=8,
                        origin="REQUEST",
                    ),
                ]
            )
            session.flush()
            session.add(
                Shift(
                    id="SHIFT-1",
                    resource_requirement_id="REQ-CAPTURED",
                    resource_id="R-SHIFT",
                    work_date=date(2026, 9, 21),
                    hours=8,
                )
            )
        engine.dispose()
        return url

    def test_captured_requirement_uses_approved_context_not_current_request_line(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    service = OperationalContactService(
                        SqlOperationalContactRepository(session)
                    )
                    current = service.resolve_request_line("L1")
                    approved = service.resolve_resource_requirement("REQ-CAPTURED")

                self.assertEqual(
                    current.operational_responsible.source_type,
                    SOURCE_REQUEST_OVERRIDE,
                )
                self.assertEqual(
                    current.operational_responsible.contact_id,
                    "C-CURRENT-OVERRIDE",
                )
                self.assertEqual(
                    approved.operational_responsible.source_type,
                    SOURCE_TASK_RESPONSIBLE,
                )
                self.assertEqual(
                    approved.operational_responsible.contact_id,
                    "C-TASK-APPROVED",
                )
                self.assertEqual(
                    approved.operational_responsible.email,
                    "configured-marc",
                )
                self.assertEqual(approved.task_id, "T-APPROVED")
                self.assertEqual(approved.approved_request_version, 3)
                self.assertEqual(approved.resource_id, "R-REQ")
                self.assertEqual(
                    approved.coordinator.source_type,
                    SOURCE_RESOURCE_COORDINATOR,
                )
                self.assertEqual(approved.coordinator.contact_id, "C-RREQ")
            finally:
                engine.dispose()

    def test_captured_responsibility_is_non_retroactive_after_lower_sources_change(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    task = session.get(TaskCatalogEntry, "T-APPROVED")
                    project = session.get(Project, "P1")
                    request = session.get(WorkforceRequest, "D1")
                    assert task is not None and project is not None and request is not None
                    task.operational_responsible_contact_id = "C-TASK-CURRENT"
                    project.operational_responsible_override_contact_id = "C-CURRENT-OVERRIDE"
                    project.project_manager_external_id = None
                    project.project_manager_contact_id = None
                    request.operational_responsible_override_contact_id = None

                with factory() as session:
                    result = OperationalContactService(
                        SqlOperationalContactRepository(session)
                    ).resolve_resource_requirement("REQ-CAPTURED")

                self.assertEqual(result.operational_responsible.status, STATUS_RESOLVED)
                self.assertEqual(
                    result.operational_responsible.contact_id,
                    "C-TASK-APPROVED",
                )
                self.assertEqual(
                    result.operational_responsible.source_type,
                    SOURCE_TASK_RESPONSIBLE,
                )
            finally:
                engine.dispose()

    def test_shift_and_requirement_overrides_layer_over_captured_inheritance(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    requirement = session.get(ResourceRequirement, "REQ-CAPTURED")
                    shift = session.get(Shift, "SHIFT-1")
                    assert requirement is not None and shift is not None
                    requirement.operational_responsible_override_contact_id = "C-REQ-OVERRIDE"
                    shift.operational_responsible_override_contact_id = "C-SHIFT-OVERRIDE"

                with factory() as session:
                    service = OperationalContactService(
                        SqlOperationalContactRepository(session)
                    )
                    requirement_result = service.resolve_resource_requirement(
                        "REQ-CAPTURED"
                    )
                    shift_result = service.resolve_shift("SHIFT-1")

                self.assertEqual(
                    requirement_result.operational_responsible.source_type,
                    SOURCE_REQUIREMENT_OVERRIDE,
                )
                self.assertEqual(
                    requirement_result.operational_responsible.contact_id,
                    "C-REQ-OVERRIDE",
                )
                self.assertEqual(
                    shift_result.operational_responsible.source_type,
                    SOURCE_SHIFT_OVERRIDE,
                )
                self.assertEqual(
                    shift_result.operational_responsible.contact_id,
                    "C-SHIFT-OVERRIDE",
                )
            finally:
                engine.dispose()

    def test_inactive_explicit_requirement_override_is_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    requirement = session.get(ResourceRequirement, "REQ-CAPTURED")
                    override = session.get(BusinessContact, "C-REQ-OVERRIDE")
                    assert requirement is not None and override is not None
                    requirement.operational_responsible_override_contact_id = "C-REQ-OVERRIDE"
                    override.active = False

                with factory() as session:
                    result = OperationalContactService(
                        SqlOperationalContactRepository(session)
                    ).resolve_resource_requirement("REQ-CAPTURED")

                self.assertEqual(result.operational_responsible.status, STATUS_INACTIVE)
                self.assertEqual(
                    result.operational_responsible.source_type,
                    SOURCE_REQUIREMENT_OVERRIDE,
                )
                self.assertEqual(
                    result.operational_responsible.contact_id,
                    "C-REQ-OVERRIDE",
                )
            finally:
                engine.dispose()

    def test_shift_resource_is_authoritative_over_requirement_assignment(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    result = OperationalContactService(
                        SqlOperationalContactRepository(session)
                    ).resolve_shift("SHIFT-1")

                self.assertEqual(result.subject_type, "SHIFT")
                self.assertEqual(result.requirement_id, "REQ-CAPTURED")
                self.assertEqual(result.resource_id, "R-SHIFT")
                self.assertEqual(result.coordinator.contact_id, "C-RSHIFT")
                self.assertNotIn(
                    "SHIFT_RESOURCE_DIFFERS_FROM_REQUIREMENT",
                    result.diagnostics,
                )
            finally:
                engine.dispose()

    def test_594a_migration_observed_snapshot_remains_readable_without_captured_status(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    result = OperationalContactService(
                        SqlOperationalContactRepository(session)
                    ).resolve_resource_requirement("REQ-MIGRATED")

                self.assertEqual(result.operational_responsible.status, STATUS_RESOLVED)
                self.assertEqual(
                    result.operational_responsible.contact_id,
                    "C-TASK-APPROVED",
                )
                self.assertEqual(
                    result.operational_responsible.source_type,
                    SOURCE_TASK_RESPONSIBLE,
                )
            finally:
                engine.dispose()

    def test_legacy_unknown_never_infers_request_or_task_context(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    service = OperationalContactService(
                        SqlOperationalContactRepository(session)
                    )
                    with_resource_coordinator = service.resolve_resource_requirement(
                        "REQ-LEGACY"
                    )
                    without_resource_coordinator = service.resolve_resource_requirement(
                        "REQ-LEGACY-NO-COORD"
                    )

                self.assertEqual(
                    with_resource_coordinator.operational_responsible.status,
                    STATUS_UNRESOLVED,
                )
                self.assertIn(
                    DIAGNOSTIC_APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN,
                    with_resource_coordinator.operational_responsible.diagnostics,
                )
                self.assertEqual(
                    with_resource_coordinator.coordinator.status,
                    STATUS_RESOLVED,
                )
                self.assertEqual(
                    with_resource_coordinator.coordinator.contact_id,
                    "C-RREQ",
                )
                self.assertEqual(
                    without_resource_coordinator.coordinator.status,
                    STATUS_UNRESOLVED,
                )
                self.assertIn(
                    DIAGNOSTIC_APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN,
                    without_resource_coordinator.coordinator.diagnostics,
                )
            finally:
                engine.dispose()

    def test_http_contract_exposes_common_requirement_and_shift_projection(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                requirement = client.get(
                    "/api/v1/resource-requirements/REQ-CAPTURED/contact-resolution"
                )
                shift = client.get("/api/v1/shifts/SHIFT-1/contact-resolution")
                missing = client.get(
                    "/api/v1/resource-requirements/REQ-MISSING/contact-resolution"
                )

            self.assertEqual(requirement.status_code, 200, requirement.text)
            self.assertEqual(shift.status_code, 200, shift.text)
            self.assertEqual(missing.status_code, 404, missing.text)

            requirement_payload = requirement.json()
            shift_payload = shift.json()
            self.assertEqual(
                requirement_payload["subject_type"],
                "RESOURCE_REQUIREMENT",
            )
            self.assertEqual(
                requirement_payload["subject_id"],
                "REQ-CAPTURED",
            )
            self.assertEqual(
                requirement_payload["approved_contact_context_status"],
                "CAPTURED",
            )
            self.assertEqual(requirement_payload["task_id"], "T-APPROVED")
            self.assertEqual(
                requirement_payload["operational_responsible"]["contact_id"],
                "C-TASK-APPROVED",
            )
            self.assertEqual(shift_payload["subject_type"], "SHIFT")
            self.assertEqual(shift_payload["subject_id"], "SHIFT-1")
            self.assertEqual(shift_payload["requirement_id"], "REQ-CAPTURED")
            self.assertEqual(shift_payload["resource_id"], "R-SHIFT")
            self.assertEqual(
                shift_payload["coordinator"]["contact_id"],
                "C-RSHIFT",
            )


if __name__ == "__main__":
    unittest.main()
