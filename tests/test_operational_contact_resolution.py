from __future__ import annotations

import unittest

from app.application.operational_contacts import OperationalContactService
from app.domain.operational_contacts import (
    BusinessContactSnapshot,
    ContactCandidate,
    DIAGNOSTIC_CONTACT_EMAIL_MISSING,
    DIAGNOSTIC_CONTACT_INACTIVE,
    DIAGNOSTIC_CONTACT_PHONE_MISSING,
    DIAGNOSTIC_CONTACT_REFERENCE_INVALID,
    DIAGNOSTIC_CONTACT_UNRESOLVED,
    DIAGNOSTIC_TASK_REFERENCE_LEGACY_CODE,
    SOURCE_PROJECT_MANAGER,
    SOURCE_PROJECT_OVERRIDE,
    SOURCE_REQUEST_OVERRIDE,
    SOURCE_REQUIREMENT_OVERRIDE,
    SOURCE_RESOURCE_COORDINATOR,
    SOURCE_SHIFT_OVERRIDE,
    SOURCE_TASK_COORDINATOR,
    SOURCE_TASK_RESPONSIBLE,
    STATUS_INACTIVE,
    STATUS_INVALID_REFERENCE,
    STATUS_RESOLVED,
    STATUS_UNRESOLVED,
    resolve_coordinator,
    resolve_operational_responsible,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    Project,
    ProjectCoManager,
    RequestLine,
    Resource,
    SqlOperationalContactRepository,
    TaskCatalogEntry,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)


def contact(
    contact_id: str,
    name: str,
    *,
    active: bool = True,
    email: str | None = "configured-email",
    phone: str | None = "555-0100",
) -> BusinessContactSnapshot:
    return BusinessContactSnapshot(
        contact_id=contact_id,
        display_name=name,
        email=email,
        phone=phone,
        active=active,
    )


def candidate(
    source_type: str,
    contact_value: BusinessContactSnapshot | None = None,
    *,
    configured_id: str | None = None,
) -> ContactCandidate:
    contact_id = configured_id
    if contact_value is not None:
        contact_id = contact_value.contact_id
    return ContactCandidate(
        source_type=source_type,
        source_entity_id=f"entity:{source_type}",
        source_label=source_type,
        contact_id=contact_id,
        contact=contact_value,
    )


class OperationalContactDomainTests(unittest.TestCase):
    def test_responsible_follows_request_then_task_then_project_hierarchy(self) -> None:
        jean = contact("C-JEAN", "Jean")
        marc = contact("C-MARC", "Marc")
        sophie = contact("C-SOPHIE", "Sophie")
        absent_override = candidate(SOURCE_REQUEST_OVERRIDE)
        task_marc = candidate(SOURCE_TASK_RESPONSIBLE, marc)
        project_jean = candidate(SOURCE_PROJECT_MANAGER, jean)

        result = resolve_operational_responsible(
            request_override=absent_override,
            task_responsible=task_marc,
            project_manager=project_jean,
        )
        self.assertEqual(result.status, STATUS_RESOLVED)
        self.assertEqual(result.contact_id, "C-MARC")
        self.assertEqual(result.source_type, SOURCE_TASK_RESPONSIBLE)

        result = resolve_operational_responsible(
            request_override=absent_override,
            task_responsible=candidate(SOURCE_TASK_RESPONSIBLE),
            project_manager=project_jean,
        )
        self.assertEqual(result.contact_id, "C-JEAN")
        self.assertEqual(result.source_type, SOURCE_PROJECT_MANAGER)

        result = resolve_operational_responsible(
            request_override=candidate(SOURCE_REQUEST_OVERRIDE, sophie),
            task_responsible=task_marc,
            project_manager=project_jean,
        )
        self.assertEqual(result.contact_id, "C-SOPHIE")
        self.assertEqual(result.source_type, SOURCE_REQUEST_OVERRIDE)

    def test_responsible_supports_the_complete_six_level_hierarchy(self) -> None:
        shift = candidate(SOURCE_SHIFT_OVERRIDE, contact("C-SHIFT", "Shift"))
        requirement = candidate(
            SOURCE_REQUIREMENT_OVERRIDE,
            contact("C-REQ", "Requirement"),
        )
        request = candidate(SOURCE_REQUEST_OVERRIDE, contact("C-REQUEST", "Request"))
        task = candidate(SOURCE_TASK_RESPONSIBLE, contact("C-TASK", "Task"))
        project = candidate(SOURCE_PROJECT_OVERRIDE, contact("C-PROJECT", "Project"))
        manager = candidate(SOURCE_PROJECT_MANAGER, contact("C-MANAGER", "Manager"))

        levels = (
            (shift, requirement, request, task, project, manager, SOURCE_SHIFT_OVERRIDE),
            (None, requirement, request, task, project, manager, SOURCE_REQUIREMENT_OVERRIDE),
            (None, None, request, task, project, manager, SOURCE_REQUEST_OVERRIDE),
            (None, None, None, task, project, manager, SOURCE_TASK_RESPONSIBLE),
            (None, None, None, None, project, manager, SOURCE_PROJECT_OVERRIDE),
            (None, None, None, None, None, manager, SOURCE_PROJECT_MANAGER),
        )
        for (
            shift_override,
            requirement_override,
            request_override,
            task_responsible,
            project_override,
            project_manager,
            expected_source,
        ) in levels:
            with self.subTest(source=expected_source):
                result = resolve_operational_responsible(
                    shift_override=shift_override,
                    requirement_override=requirement_override,
                    request_override=request_override,
                    task_responsible=task_responsible,
                    project_override=project_override,
                    project_manager=project_manager,
                )
                self.assertEqual(result.status, STATUS_RESOLVED)
                self.assertEqual(result.source_type, expected_source)

    def test_materialized_hierarchy_layers_local_overrides_over_captured_inheritance(self) -> None:
        captured = ContactCandidate(
            source_type=SOURCE_TASK_RESPONSIBLE,
            source_entity_id="T-APPROVED",
            source_label="Tâche approuvée",
            contact_id="C-CAPTURED",
            contact=contact("C-CAPTURED", "Captured"),
            captured_status=STATUS_RESOLVED,
        )
        inherited = resolve_operational_responsible(
            captured_inherited=captured,
        )
        self.assertEqual(inherited.contact_id, "C-CAPTURED")
        self.assertEqual(inherited.source_type, SOURCE_TASK_RESPONSIBLE)

        overridden = resolve_operational_responsible(
            requirement_override=candidate(
                SOURCE_REQUIREMENT_OVERRIDE,
                contact("C-REQ", "Requirement"),
            ),
            captured_inherited=captured,
        )
        self.assertEqual(overridden.contact_id, "C-REQ")
        self.assertEqual(
            overridden.source_type,
            SOURCE_REQUIREMENT_OVERRIDE,
        )

    def test_coordinator_follows_resource_then_task_hierarchy(self) -> None:
        julie = contact("C-JULIE", "Julie")
        paul = contact("C-PAUL", "Paul")

        result = resolve_coordinator(
            resource_coordinator=candidate(SOURCE_RESOURCE_COORDINATOR, julie),
            task_coordinator=candidate(SOURCE_TASK_COORDINATOR, paul),
        )
        self.assertEqual(result.contact_id, "C-JULIE")
        self.assertEqual(result.source_type, SOURCE_RESOURCE_COORDINATOR)

        result = resolve_coordinator(
            resource_coordinator=candidate(SOURCE_RESOURCE_COORDINATOR),
            task_coordinator=candidate(SOURCE_TASK_COORDINATOR, paul),
        )
        self.assertEqual(result.contact_id, "C-PAUL")
        self.assertEqual(result.source_type, SOURCE_TASK_COORDINATOR)

        result = resolve_coordinator(
            resource_coordinator=candidate(SOURCE_RESOURCE_COORDINATOR),
            task_coordinator=candidate(SOURCE_TASK_COORDINATOR),
        )
        self.assertEqual(result.status, STATUS_UNRESOLVED)
        self.assertIn(DIAGNOSTIC_CONTACT_UNRESOLVED, result.diagnostics)

    def test_configured_invalid_or_inactive_reference_blocks_fallback(self) -> None:
        fallback = candidate(
            SOURCE_TASK_RESPONSIBLE,
            contact("C-MARC", "Marc"),
        )

        invalid = resolve_operational_responsible(
            request_override=candidate(
                SOURCE_REQUEST_OVERRIDE,
                configured_id="C-MISSING",
            ),
            task_responsible=fallback,
            project_manager=candidate(
                SOURCE_PROJECT_MANAGER,
                contact("C-JEAN", "Jean"),
            ),
        )
        self.assertEqual(invalid.status, STATUS_INVALID_REFERENCE)
        self.assertEqual(invalid.contact_id, "C-MISSING")
        self.assertEqual(invalid.source_type, SOURCE_REQUEST_OVERRIDE)
        self.assertIn(DIAGNOSTIC_CONTACT_REFERENCE_INVALID, invalid.diagnostics)

        inactive = resolve_operational_responsible(
            request_override=candidate(
                SOURCE_REQUEST_OVERRIDE,
                contact("C-SOPHIE", "Sophie", active=False),
            ),
            task_responsible=fallback,
            project_manager=candidate(
                SOURCE_PROJECT_MANAGER,
                contact("C-JEAN", "Jean"),
            ),
        )
        self.assertEqual(inactive.status, STATUS_INACTIVE)
        self.assertEqual(inactive.contact_id, "C-SOPHIE")
        self.assertIn(DIAGNOSTIC_CONTACT_INACTIVE, inactive.diagnostics)

    def test_missing_coordinates_are_diagnostics_not_fallbacks(self) -> None:
        result = resolve_coordinator(
            resource_coordinator=candidate(
                SOURCE_RESOURCE_COORDINATOR,
                contact("C-JULIE", "Julie", email=None, phone=None),
            ),
            task_coordinator=candidate(
                SOURCE_TASK_COORDINATOR,
                contact("C-PAUL", "Paul"),
            ),
        )
        self.assertEqual(result.status, STATUS_RESOLVED)
        self.assertEqual(result.contact_id, "C-JULIE")
        self.assertIn(DIAGNOSTIC_CONTACT_EMAIL_MISSING, result.diagnostics)
        self.assertIn(DIAGNOSTIC_CONTACT_PHONE_MISSING, result.diagnostics)


class SqlOperationalContactRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add_all(
                [
                    BusinessContact(
                        id="C-PM",
                        display_name="Jean PM",
                        email="configured-jean",
                        phone="555-1000",
                    ),
                    BusinessContact(
                        id="C-TASK",
                        display_name="Marc tâche",
                        email="configured-marc",
                        phone="555-2000",
                    ),
                    BusinessContact(
                        id="C-TCOORD",
                        display_name="Paul coord tâche",
                        email="configured-paul",
                        phone="555-3000",
                    ),
                    BusinessContact(
                        id="C-RCOORD",
                        display_name="Julie coord ressource",
                        email="configured-julie",
                        phone="555-4000",
                    ),
                    BusinessContact(
                        id="C-OVERRIDE",
                        display_name="Sophie override",
                        email="configured-sophie",
                        phone="555-5000",
                    ),
                    BusinessContact(
                        id="C-PROJECT",
                        display_name="Responsable projet",
                        email="configured-project",
                        phone="555-5100",
                    ),
                ]
            )
            session.add(
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
                )
            )
            session.add(
                Project(
                    id="P1",
                    number="P-1",
                    name="Projet 1",
                    project_manager_external_id="EMP-PM",
                    project_manager_name="Jean PM",
                    project_manager_contact_id="C-TASK",
                )
            )
            session.add(
                TaskCatalogEntry(
                    id="T1",
                    project_number="P-1",
                    task_code="210",
                    label="Installation",
                    operational_responsible_contact_id="C-TASK",
                    coordinator_contact_id="C-TCOORD",
                    active=True,
                )
            )
            session.add(
                Resource(
                    id="R1",
                    name="Alice",
                    coordinator_contact_id="C-RCOORD",
                    active=True,
                )
            )
            session.add(
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DMO-1",
                    project_id="P1",
                    line_mode=True,
                )
            )
            # These ORM models intentionally expose no relationships. Flush the
            # FK parents before inserting the child, matching the production
            # demand repository's persistence order.
            session.flush()
            session.add(
                RequestLine(
                    id="L1",
                    workforce_request_id="D1",
                    position=0,
                    task_catalog_item_id="T1",
                    erp_task_code="210",
                    erp_task_label="Installation",
                    proposed_resource_id="R1",
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def resolve(self):
        with self.factory() as session:
            service = OperationalContactService(
                SqlOperationalContactRepository(session)
            )
            return service.resolve_request_line("L1")

    def test_sql_context_resolves_task_responsible_and_resource_coordinator(self) -> None:
        result = self.resolve()
        self.assertEqual(result.operational_responsible.contact_id, "C-TASK")
        self.assertEqual(
            result.operational_responsible.source_type,
            SOURCE_TASK_RESPONSIBLE,
        )
        self.assertEqual(result.coordinator.contact_id, "C-RCOORD")
        self.assertEqual(
            result.coordinator.source_type,
            SOURCE_RESOURCE_COORDINATOR,
        )
        self.assertEqual(result.task_id, "T1")
        self.assertEqual(result.task_code, "210")
        self.assertEqual(result.proposed_resource_id, "R1")
        self.assertEqual(result.diagnostics, ())

    def test_sql_context_falls_back_when_relationship_is_absent(self) -> None:
        with self.factory.begin() as session:
            line = session.get(RequestLine, "L1")
            task = session.get(TaskCatalogEntry, "T1")
            resource = session.get(Resource, "R1")
            assert line is not None and task is not None and resource is not None
            task.operational_responsible_contact_id = None
            resource.coordinator_contact_id = None

        result = self.resolve()
        self.assertEqual(result.operational_responsible.contact_id, "C-PM")
        self.assertEqual(
            result.operational_responsible.source_type,
            SOURCE_PROJECT_MANAGER,
        )
        self.assertEqual(result.coordinator.contact_id, "C-TCOORD")
        self.assertEqual(
            result.coordinator.source_type,
            SOURCE_TASK_COORDINATOR,
        )

    def test_unresolved_erp_principal_stays_project_manager_and_does_not_fallback_to_co_manager(self) -> None:
        with self.factory.begin() as session:
            task = session.get(TaskCatalogEntry, "T1")
            project = session.get(Project, "P1")
            assert task is not None and project is not None
            task.operational_responsible_contact_id = None
            project.project_manager_external_id = "EMP-UNRESOLVED"
            project.project_manager_name = "Principal ERP non lié"
            session.add(
                BusinessContact(
                    id="C-CO",
                    display_name="Co chargé RP",
                    email="co" + chr(64) + "example.invalid",
                    phone="555-6000",
                )
            )
            session.add(
                AppUser(
                    id="U-CO",
                    issuer=None,
                    subject=None,
                    display_name="Co chargé RP",
                    email="co" + chr(64) + "example.invalid",
                    employee_external_id=None,
                    business_contact_id="C-CO",
                    roles_json='["PROJECT_MANAGER"]',
                    active=True,
                )
            )
            session.flush()
            session.add(
                ProjectCoManager(
                    project_id="P1",
                    business_contact_id="C-CO",
                    created_by_user_id="U-CO",
                )
            )

        result = self.resolve()
        self.assertEqual(result.operational_responsible.status, STATUS_UNRESOLVED)
        self.assertEqual(
            result.operational_responsible.source_type,
            SOURCE_PROJECT_MANAGER,
        )
        self.assertIsNone(result.operational_responsible.contact_id)
        self.assertEqual(
            result.operational_responsible.display_name,
            "Principal ERP non lié",
        )
        self.assertIn(
            "ERP_PROJECT_MANAGER_APP_USER_NOT_LINKED",
            result.operational_responsible.diagnostics,
        )

    def test_sql_project_override_precedes_erp_principal(self) -> None:
        with self.factory.begin() as session:
            task = session.get(TaskCatalogEntry, "T1")
            project = session.get(Project, "P1")
            assert task is not None and project is not None
            task.operational_responsible_contact_id = None
            project.operational_responsible_override_contact_id = "C-PROJECT"

        result = self.resolve()
        self.assertEqual(result.operational_responsible.contact_id, "C-PROJECT")
        self.assertEqual(
            result.operational_responsible.source_type,
            SOURCE_PROJECT_OVERRIDE,
        )

    def test_sql_request_override_takes_priority(self) -> None:
        with self.factory.begin() as session:
            request = session.get(WorkforceRequest, "D1")
            assert request is not None
            request.operational_responsible_override_contact_id = "C-OVERRIDE"

        result = self.resolve()
        self.assertEqual(result.operational_responsible.contact_id, "C-OVERRIDE")
        self.assertEqual(
            result.operational_responsible.source_type,
            SOURCE_REQUEST_OVERRIDE,
        )

    def test_legacy_task_code_is_resolved_with_explicit_diagnostic(self) -> None:
        with self.factory.begin() as session:
            line = session.get(RequestLine, "L1")
            assert line is not None
            line.task_catalog_item_id = None

        result = self.resolve()
        self.assertEqual(result.task_id, "T1")
        self.assertEqual(result.operational_responsible.contact_id, "C-TASK")
        self.assertIn(DIAGNOSTIC_TASK_REFERENCE_LEGACY_CODE, result.diagnostics)


if __name__ == "__main__":
    unittest.main()
