from __future__ import annotations

from datetime import date
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application.communications import CommunicationTransportResult
from app.application.security import AuthPrincipal, ROLE_ADMIN
from app.server.security import static_auth_resolver
from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.application.operational_contacts import OperationalContactService
from app.application.project_communications import ProjectCommunicationService
from app.domain.operational_contacts import (
    PROVENANCE_MIGRATION_OBSERVED,
    RESPONSIBILITY_CONTEXT_VERSION,
    SOURCE_TASK_RESPONSIBLE,
    STATUS_RESOLVED,
)
from app.infrastructure.sql import (
    AppUser,
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    Base,
    BusinessContact,
    CommunicationBatchRow,
    ORIGIN_QUICK_SHIFT,
    Project,
    ProjectCoManager,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.operational_contact_repository import (
    SqlOperationalContactRepository,
)
from app.infrastructure.sql.project_communication_repository import (
    DIAGNOSTIC_RESOURCE_EMAIL_MISSING,
    DIAGNOSTIC_RESOURCE_ERP_INACTIVE,
    SqlProjectCommunicationRepository,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


WEEK = date(2026, 9, 21)
TEST_DOMAIN = "example.test"


class FakeSmtpDeliveryClient:
    def __init__(self) -> None:
        self.attempt_subjects: list[str] = []
        self.attempt_bodies: list[str] = []
        self.attempt_cc: list[tuple[str, ...]] = []
        self._failed_once = False

    def test_connection(self, configuration) -> None:
        return None

    def send_message(self, configuration, message, *, message_id: str) -> str:
        self.attempt_subjects.append(message.subject)
        self.attempt_bodies.append(message.body)
        self.attempt_cc.append(tuple(message.cc_emails))
        if "2000" in message.subject and not self._failed_once:
            self._failed_once = True
            raise RuntimeError("synthetic smtp failure")
        return message_id


class FakeProjectDraftTransport:
    def __init__(self) -> None:
        self.messages = ()
        self.create_calls = 0

    def create_drafts(self, messages):
        self.create_calls += 1
        self.messages = tuple(messages)
        return CommunicationTransportResult(
            provider="fake_graph",
            created_count=len(self.messages),
        )


class SqlProjectCommunicationProjectionTests(unittest.TestCase):
    @staticmethod
    def _add_second_project(database_url: str) -> None:
        engine = create_sql_engine(database_url)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add(
                    Project(
                        id="P2",
                        number="2000",
                        name="Deuxième projet",
                        project_manager_external_id="PM-1",
                        project_manager_name="Ancien nom",
                        project_manager_contact_id="C-RESP",
                        status="Actif",
                    )
                )
                session.add(
                    ResourceRequirement(
                        id="REQ-P2",
                        project_id="P2",
                        workforce_request_id=None,
                        approved_contact_context_status="NOT_APPLICABLE",
                        assigned_resource_id="R1",
                        start_date=WEEK,
                        end_date=WEEK,
                        planned_hours=Decimal("2"),
                        description="Travaux deuxième projet",
                        status="Planifié",
                        origin="AD_HOC",
                    )
                )
                session.flush()
                session.add(
                    Shift(
                        id="S-P2",
                        resource_requirement_id="REQ-P2",
                        resource_id="R1",
                        work_date=WEEK,
                        hours=Decimal("2"),
                        allocation_type="Flexible",
                        outside_standard_hours=False,
                    )
                )
        finally:
            engine.dispose()

    @staticmethod
    def _add_r29_project(database_url: str) -> str:
        project_id = "85400000-0000-0000-0000-000000005110"
        engine = create_sql_engine(database_url)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add(
                    Project(
                        id=project_id,
                        number="5110",
                        name="Projet R29",
                        project_manager_external_id="PM-1",
                        project_manager_name="Ancien nom",
                        project_manager_contact_id="C-RESP",
                        status="Actif",
                    )
                )
                session.add(
                    ResourceRequirement(
                        id="REQ-R29-854",
                        project_id=project_id,
                        workforce_request_id=None,
                        approved_contact_context_status="NOT_APPLICABLE",
                        assigned_resource_id="R1",
                        start_date=WEEK,
                        end_date=WEEK,
                        planned_hours=Decimal("2"),
                        description="Travaux R29",
                        status="Planifié",
                        origin="AD_HOC",
                    )
                )
                session.flush()
                session.add(
                    Shift(
                        id="S-R29-854",
                        resource_requirement_id="REQ-R29-854",
                        resource_id="R1",
                        work_date=WEEK,
                        hours=Decimal("2"),
                        allocation_type="Flexible",
                        outside_standard_hours=False,
                    )
                )
        finally:
            engine.dispose()
        return project_id

    @staticmethod
    def _add_quick_shift(
        database_url: str,
        *,
        requirement_id: str,
        shift_id: str,
        description: str,
        hours: str = "2",
    ) -> None:
        engine = create_sql_engine(database_url)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add(
                    ResourceRequirement(
                        id=requirement_id,
                        project_id="P1",
                        workforce_request_id=None,
                        approved_contact_context_status="NOT_APPLICABLE",
                        assigned_resource_id="R1",
                        start_date=WEEK,
                        end_date=WEEK,
                        planned_hours=Decimal(hours),
                        description=description,
                        status="Planifié",
                        planning_type="Fixe",
                        confirmation="Confirmée",
                        origin=ORIGIN_QUICK_SHIFT,
                    )
                )
                session.flush()
                session.add(
                    Shift(
                        id=shift_id,
                        resource_requirement_id=requirement_id,
                        resource_id="R1",
                        work_date=WEEK,
                        hours=Decimal(hours),
                        allocation_type="Fixe",
                        source="MANUAL",
                        locked=True,
                        outside_standard_hours=False,
                    )
                )
        finally:
            engine.dispose()

    @staticmethod
    def _seed(session) -> None:
        pm_email = "pm" + chr(64) + TEST_DOMAIN
        tech_email = "tech" + chr(64) + TEST_DOMAIN
        resource_one_email = "resource-one" + chr(64) + TEST_DOMAIN
        resource_two_email = "resource-two" + chr(64) + TEST_DOMAIN
        session.add_all(
            [
                BusinessContact(
                    id="C-PM",
                    display_name="Chargé de projet Démo",
                    email=pm_email,
                    source="APP_USER",
                ),
                BusinessContact(
                    id="C-RESP",
                    display_name="Responsable approuvé",
                    phone="555" + "-" + "0100",
                    source="APP_USER",
                ),
                BusinessContact(
                    id="C-R1",
                    display_name="Technicien profil",
                    email=tech_email,
                    source="APP_USER",
                ),
                BusinessContact(
                    id="C-CURRENT",
                    display_name="Responsable courant",
                    source="APP_USER",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                AppUser(
                    id="U-PM",
                    issuer="urn:test",
                    subject="pm",
                    display_name="Chargé de projet Démo",
                    email=pm_email,
                    employee_external_id="PM-1",
                    business_contact_id="C-PM",
                    roles_json='["PROJECT_MANAGER"]',
                    active=True,
                ),
                AppUser(
                    id="U-RESP",
                    issuer="urn:test",
                    subject="resp",
                    display_name="Responsable approuvé",
                    email=None,
                    employee_external_id="RESP-1",
                    business_contact_id="C-RESP",
                    roles_json='["PROJECT_MANAGER"]',
                    active=True,
                ),
                AppUser(
                    id="U-R1",
                    issuer="urn:test",
                    subject="tech",
                    display_name="Technicien profil",
                    email=tech_email,
                    employee_external_id="EMP-1",
                    business_contact_id="C-R1",
                    roles_json='["TECHNICIAN"]',
                    active=True,
                ),
                AppUser(
                    id="U-CURRENT",
                    issuer="urn:test",
                    subject="current",
                    display_name="Responsable courant",
                    email=None,
                    employee_external_id="RESP-2",
                    business_contact_id="C-CURRENT",
                    roles_json='["PROJECT_MANAGER"]',
                    active=True,
                ),
            ]
        )
        session.add(
            Project(
                id="P1",
                number="1000",
                name="Projet projection",
                project_manager_external_id="PM-1",
                project_manager_name="Ancien nom",
                project_manager_contact_id="C-RESP",
                status="Actif",
            )
        )
        session.add_all(
            [
                TaskCatalogEntry(
                    id="T-APPROVED",
                    project_number="1000",
                    task_code="210",
                    label="Tâche approuvée",
                    operational_responsible_contact_id="C-RESP",
                    active=True,
                ),
                TaskCatalogEntry(
                    id="T-CURRENT",
                    project_number="1000",
                    task_code="220",
                    label="Tâche courante",
                    operational_responsible_contact_id="C-CURRENT",
                    active=True,
                ),
                Resource(
                    id="R1",
                    external_id="EMP-1",
                    name="Technicien ressource",
                    email=resource_one_email,
                    active=True,
                ),
                Resource(
                    id="R2",
                    external_id="EMP-2",
                    name="Ressource sans utilisateur",
                    email=resource_two_email,
                    active=True,
                ),
            ]
        )
        session.flush()
        session.add(
            WorkforceRequest(
                id="D1",
                legacy_demand_number="DMO-290A-1",
                project_id="P1",
                description="Description courante de la demande",
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
                description="Description modifiée non approuvée",
            )
        )
        session.add_all(
            [
                ResourceRequirement(
                    id="REQ1",
                    project_id="P1",
                    workforce_request_id="D1",
                    source_request_line_id="L1",
                    approved_task_catalog_item_id="T-APPROVED",
                    approved_request_version=3,
                    approved_contact_context_status="CAPTURED",
                    captured_operational_responsible_contact_id="C-RESP",
                    captured_operational_responsible_source_type=SOURCE_TASK_RESPONSIBLE,
                    captured_operational_responsible_source_entity_id="T-APPROVED",
                    captured_operational_responsible_status=STATUS_RESOLVED,
                    operational_responsibility_context_provenance=(
                        PROVENANCE_MIGRATION_OBSERVED
                    ),
                    operational_responsibility_context_version=(
                        RESPONSIBILITY_CONTEXT_VERSION
                    ),
                    assigned_resource_id="R1",
                    start_date=WEEK,
                    end_date=WEEK,
                    planned_hours=Decimal("8"),
                    description="Installation approuvée",
                    status="Planifié",
                    origin="REQUEST",
                ),
                ResourceRequirement(
                    id="REQ2",
                    project_id="P1",
                    workforce_request_id="D1",
                    source_request_line_id="L1",
                    approved_task_catalog_item_id="T-APPROVED",
                    approved_request_version=3,
                    approved_contact_context_status="CAPTURED",
                    captured_operational_responsible_contact_id="C-RESP",
                    captured_operational_responsible_source_type=SOURCE_TASK_RESPONSIBLE,
                    captured_operational_responsible_source_entity_id="T-APPROVED",
                    captured_operational_responsible_status=STATUS_RESOLVED,
                    operational_responsibility_context_provenance=(
                        PROVENANCE_MIGRATION_OBSERVED
                    ),
                    operational_responsibility_context_version=(
                        RESPONSIBILITY_CONTEXT_VERSION
                    ),
                    assigned_resource_id="R2",
                    start_date=WEEK,
                    end_date=WEEK,
                    planned_hours=Decimal("4"),
                    description="Installation approuvée",
                    status="Planifié",
                    origin="REQUEST",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Shift(
                    id="S1",
                    resource_requirement_id="REQ1",
                    resource_id="R1",
                    work_date=WEEK,
                    hours=Decimal("8"),
                    allocation_type="Flexible",
                    outside_standard_hours=False,
                ),
                Shift(
                    id="S2",
                    resource_requirement_id="REQ2",
                    resource_id="R2",
                    work_date=WEEK,
                    hours=Decimal("4"),
                    allocation_type="Flexible",
                    outside_standard_hours=True,
                ),
                Shift(
                    id="S-MISSING",
                    resource_requirement_id="REQ1",
                    resource_id="R2",
                    work_date=WEEK,
                    hours=Decimal("6"),
                    allocation_type=MISSING_ALLOCATION_TYPE,
                    outside_standard_hours=False,
                ),
            ]
        )

    def _database(self, directory: str) -> str:
        path = Path(directory) / "project-communication.db"
        url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            self._seed(session)
        engine.dispose()
        return url

    @staticmethod
    def _service(session) -> ProjectCommunicationService:
        operational = OperationalContactService(
            SqlOperationalContactRepository(session)
        )
        return ProjectCommunicationService(
            SqlProjectCommunicationRepository(
                session,
                operational_contacts=operational,
            )
        )

    def test_missing_allocation_proposals_are_not_project_assignments(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    assignments = SqlProjectCommunicationRepository(
                        session,
                        operational_contacts=OperationalContactService(
                            SqlOperationalContactRepository(session)
                        ),
                    ).list_assignments(
                        week_start=WEEK,
                        week_end=WEEK,
                    )
            finally:
                engine.dispose()

        self.assertEqual({row.shift_id for row in assignments}, {"S1", "S2"})
        self.assertNotIn("S-MISSING", {row.shift_id for row in assignments})

    def test_assignment_projection_uses_bulk_operational_resolution(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    operational = OperationalContactService(
                        SqlOperationalContactRepository(session)
                    )
                    with patch.object(
                        operational,
                        "resolve_shift",
                        side_effect=AssertionError("single-shift resolution is an N+1"),
                    ), patch.object(
                        operational,
                        "resolve_shifts",
                        wraps=operational.resolve_shifts,
                    ) as bulk:
                        assignments = SqlProjectCommunicationRepository(
                            session,
                            operational_contacts=operational,
                        ).list_assignments(
                            week_start=WEEK,
                            week_end=WEEK,
                        )
            finally:
                engine.dispose()

        self.assertEqual({row.shift_id for row in assignments}, {"S1", "S2"})
        bulk.assert_called_once()
        self.assertEqual(set(bulk.call_args.args[0]), {"S1", "S2"})

    def test_co_manager_change_updates_fingerprint_and_persists_to_recipients(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app) as client:
                before = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(before.status_code, 200, before.text)

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                try:
                    with factory.begin() as session:
                        session.add(
                            BusinessContact(
                                id="C-CO",
                                display_name="Co chargé RP",
                                email="co" + chr(64) + "example.test",
                                source="APP_USER",
                            )
                        )
                        session.add(
                            AppUser(
                                id="U-CO",
                                issuer=None,
                                subject=None,
                                display_name="Co chargé RP",
                                email="co" + chr(64) + "example.test",
                                employee_external_id=None,
                                business_contact_id="C-CO",
                                roles_json='["PROJECT_MANAGER"]',
                                active=True,
                            )
                        )
                        project = session.get(Project, "P1")
                        assert project is not None
                        project.co_managers_version += 1
                        session.flush()
                        session.add(
                            ProjectCoManager(
                                project_id="P1",
                                business_contact_id="C-CO",
                                created_by_user_id="U-CO",
                            )
                        )
                finally:
                    engine.dispose()

                after = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(after.status_code, 200, after.text)
                after_payload = after.json()
                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": after_payload["snapshot_fingerprint"],
                        "reviews": [],
                    },
                )
                self.assertEqual(prepared.status_code, 201, prepared.text)

        before_payload = before.json()
        self.assertNotEqual(
            after_payload["snapshot_fingerprint"],
            before_payload["snapshot_fingerprint"],
        )
        self.assertEqual(
            after_payload["drafts"][0]["to_recipient"],
            before_payload["drafts"][0]["to_recipient"],
        )
        self.assertEqual(
            [row["email"] for row in before_payload["drafts"][0]["to_recipients"]],
            ["pm" + chr(64) + TEST_DOMAIN],
        )
        self.assertEqual(
            [row["email"] for row in after_payload["drafts"][0]["to_recipients"]],
            [
                "pm" + chr(64) + TEST_DOMAIN,
                "co" + chr(64) + TEST_DOMAIN,
            ],
        )
        self.assertEqual(
            after_payload["drafts"][0]["cc_recipients"],
            before_payload["drafts"][0]["cc_recipients"],
        )
        self.assertEqual(
            prepared.json()["messages"][0]["to_emails"],
            [
                "pm" + chr(64) + TEST_DOMAIN,
                "co" + chr(64) + TEST_DOMAIN,
            ],
        )

    def test_projection_uses_approved_context_and_resource_recipients(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    projection = self._service(session).project_projection(
                        week_start=WEEK
                    )
            finally:
                engine.dispose()

        project = projection.projects[0]
        self.assertEqual(project.project_manager.display_name, "Chargé de projet Démo")
        self.assertEqual(project.project_manager.email, "pm" + chr(64) + TEST_DOMAIN)
        task = project.days[0].tasks[0]
        self.assertEqual(task.task_description, "Installation approuvée")
        self.assertEqual(task.task_ids, ("T-APPROVED",))
        self.assertEqual(
            task.operational_responsibles[0].display_name,
            "Responsable approuvé",
        )
        tech = next(row for row in task.resources if row.resource_id == "R1")
        self.assertEqual(
            tech.contact.email,
            "resource-one" + chr(64) + TEST_DOMAIN,
        )
        self.assertIsNone(tech.contact.user_id)
        self.assertIsNone(tech.contact.contact_id)
        self.assertNotEqual(tech.contact.email, "tech" + chr(64) + TEST_DOMAIN)

    def test_requester_is_resolved_from_canonical_app_user_and_added_to_project_to(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    session.add(
                        AppUser(
                            id="U-REQUESTER-826",
                            issuer="urn:test",
                            subject="requester-826",
                            display_name="Demandeuse canonique",
                            email="requester" + chr(64) + TEST_DOMAIN,
                            roles_json='["PROJECT_MANAGER"]',
                            active=True,
                        )
                    )
                    request = session.get(WorkforceRequest, "D1")
                    assert request is not None
                    request.requester_user_id = "U-REQUESTER-826"
                    request.requester_name = "Nom texte obsolète"

                with factory() as session:
                    projection = self._service(session).project_projection(
                        week_start=WEEK
                    )
                    project = projection.projects[0]
                    self.assertEqual(len(project.requesters), 1)
                    requester = project.requesters[0]
                    self.assertEqual(requester.user_id, "U-REQUESTER-826")
                    self.assertEqual(requester.display_name, "Demandeuse canonique")
                    self.assertEqual(
                        requester.email,
                        "requester" + chr(64) + TEST_DOMAIN,
                    )

                app = create_api_app(
                    database_url,
                    auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                )
                with TestClient(app) as client:
                    preview = client.get(
                        "/api/v1/communications/project-preview"
                        "?week_start=2026-09-23"
                    )
                    self.assertEqual(preview.status_code, 200, preview.text)
                    draft = preview.json()["drafts"][0]
                    self.assertEqual(
                        [row["email"] for row in draft["to_recipients"]],
                        [
                            "pm" + chr(64) + TEST_DOMAIN,
                            "requester" + chr(64) + TEST_DOMAIN,
                        ],
                    )
                    self.assertEqual(
                        [row["email"] for row in draft["cc_recipients"]],
                        [
                            "resource-two" + chr(64) + TEST_DOMAIN,
                            "resource-one" + chr(64) + TEST_DOMAIN,
                        ],
                    )
            finally:
                engine.dispose()

    def test_requester_same_as_project_manager_is_deduplicated_by_canonical_identity(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request = session.get(WorkforceRequest, "D1")
                    assert request is not None
                    request.requester_user_id = "U-PM"
                    request.requester_name = "Nom historique différent"

                app = create_api_app(
                    database_url,
                    auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                )
                with TestClient(app) as client:
                    preview = client.get(
                        "/api/v1/communications/project-preview"
                        "?week_start=2026-09-23"
                    )
                    self.assertEqual(preview.status_code, 200, preview.text)
                    draft = preview.json()["drafts"][0]
                    self.assertEqual(
                        [row["email"] for row in draft["to_recipients"]],
                        ["pm" + chr(64) + TEST_DOMAIN],
                    )
            finally:
                engine.dispose()

    def test_resource_without_app_user_uses_resource_email(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    projection = self._service(session).project_projection(
                        week_start=WEEK
                    )
            finally:
                engine.dispose()

        resources = projection.projects[0].days[0].tasks[0].resources
        unlinked = next(row for row in resources if row.resource_id == "R2")
        self.assertEqual(
            unlinked.contact.email,
            "resource-two" + chr(64) + TEST_DOMAIN,
        )
        self.assertTrue(unlinked.contact.active)
        self.assertIsNone(unlinked.contact.user_id)
        self.assertIsNone(unlinked.contact.contact_id)

    def test_resource_without_email_is_explicitly_diagnosed(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    resource = session.get(Resource, "R2")
                    assert resource is not None
                    resource.email = None
                with factory() as session:
                    projection = self._service(session).project_projection(
                        week_start=WEEK
                    )
            finally:
                engine.dispose()

        resources = projection.projects[0].days[0].tasks[0].resources
        missing = next(row for row in resources if row.resource_id == "R2")
        self.assertIsNone(missing.contact.email)
        self.assertIn(DIAGNOSTIC_RESOURCE_EMAIL_MISSING, missing.diagnostics)

    def test_erp_inactive_resource_is_explicitly_diagnosed(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    resource = session.get(Resource, "R2")
                    assert resource is not None
                    resource.erp_active = False
                with factory() as session:
                    projection = self._service(session).project_projection(
                        week_start=WEEK
                    )
            finally:
                engine.dispose()

        resources = projection.projects[0].days[0].tasks[0].resources
        inactive = next(row for row in resources if row.resource_id == "R2")
        self.assertFalse(inactive.contact.active)
        self.assertIn(DIAGNOSTIC_RESOURCE_ERP_INACTIVE, inactive.diagnostics)


    @staticmethod
    def _generator_resolver(email: str | None, subject: str):
        return static_auth_resolver(
            AuthPrincipal.from_roles(
                local_user_id="U-GENERATOR",
                issuer="urn:resourceplanner:test",
                subject=subject,
                display_name="Générateur explicite",
                email=email,
                roles=(ROLE_ADMIN,),
                auth_mode="test",
            )
        )

    def test_727_generator_cc_is_persisted_for_included_messages_and_transports(self) -> None:
        generator = "generator" + chr(64) + TEST_DOMAIN
        reviewer = "reviewer" + chr(64) + TEST_DOMAIN
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            self._add_second_project(url)
            transport = FakeProjectDraftTransport()
            smtp = FakeSmtpDeliveryClient()
            generator_app = create_api_app(
                url,
                auth_resolver=self._generator_resolver(generator, "generator-A"),
            )
            reviewer_app = create_api_app(
                url,
                auth_resolver=self._generator_resolver(reviewer, "reviewer-B"),
                communication_transport=transport,
                smtp_client=smtp,
            )
            with TestClient(generator_app) as origin, TestClient(reviewer_app) as approver:
                default = origin.get(
                    "/api/v1/communications/project-preview?week_start=2026-09-23"
                )
                self.assertEqual(default.status_code, 200, default.text)
                self.assertFalse(default.json()["add_generator_cc"])
                self.assertIsNone(default.json()["generator_identity_fingerprint"])
                self.assertTrue(all(
                    generator not in [r["email"] for r in draft["cc_recipients"]]
                    for draft in default.json()["drafts"]
                ))

                preview_response = origin.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23&add_generator_cc=true"
                )
                self.assertEqual(preview_response.status_code, 200, preview_response.text)
                preview = preview_response.json()
                self.assertTrue(preview["add_generator_cc"])
                self.assertEqual(len(preview["drafts"]), 2)
                self.assertEqual(
                    preview["snapshot_fingerprint"], default.json()["snapshot_fingerprint"]
                )
                self.assertTrue(preview["generator_identity_fingerprint"])
                self.assertTrue(all(
                    [r["email"] for r in draft["cc_recipients"]].count(generator) == 1
                    for draft in preview["drafts"]
                ))
                keys = {
                    draft["project_id"]: draft["message_key"]
                    for draft in preview["drafts"]
                }
                body = {
                    "week_start": "2026-09-23",
                    "expected_fingerprint": preview["snapshot_fingerprint"],
                    "expected_generator_identity_fingerprint": (
                        preview["generator_identity_fingerprint"]
                    ),
                    "add_generator_cc": True,
                    "reviews": [
                        {"message_key": keys["P1"], "include": True},
                        {"message_key": keys["P2"], "include": False},
                    ],
                }
                switched = approver.post("/api/v1/communications/project-batches", json=body)
                self.assertEqual(switched.status_code, 409, switched.text)
                self.assertEqual(
                    switched.json()["error"]["code"],
                    "project_communication_generator_changed",
                )
                prepared = origin.post("/api/v1/communications/project-batches", json=body)
                self.assertEqual(prepared.status_code, 201, prepared.text)
                messages = {
                    row["project_id"]: row for row in prepared.json()["messages"]
                }
                self.assertEqual(messages["P1"]["cc_emails"].count(generator), 1)
                self.assertNotIn(generator, messages["P2"]["cc_emails"])
                self.assertFalse(messages["P2"]["included"])
                self.assertEqual(smtp.attempt_subjects, [])

                batch_id = prepared.json()["id"]
                reloaded = approver.get(
                    "/api/v1/communications/project-batches?week_start=2026-09-23"
                ).json()
                self.assertIn(
                    generator,
                    next(
                        message["cc_emails"]
                        for message in reloaded[0]["messages"]
                        if message["project_id"] == "P1"
                    ),
                )
                self.assertEqual(
                    approver.post(
                        f"/api/v1/communications/project-batches/{batch_id}/approve"
                    ).status_code,
                    200,
                )
                created = approver.post(
                    f"/api/v1/communications/project-batches/{batch_id}/create-drafts"
                )
                self.assertEqual(created.status_code, 200, created.text)
                self.assertEqual(len(transport.messages), 1)
                self.assertIn(generator, transport.messages[0].cc_emails)
                self.assertNotIn(reviewer, transport.messages[0].cc_emails)
                downloaded = approver.get(
                    f"/api/v1/communications/project-batches/{batch_id}/draft-download"
                )
                self.assertEqual(downloaded.status_code, 200, downloaded.text)
                eml = BytesParser(policy=policy.default).parsebytes(downloaded.content)
                self.assertIn(generator, str(eml["Cc"]))
                self.assertNotIn(reviewer, str(eml["Cc"]))

                configured = approver.put(
                    "/api/v1/admin/settings/smtp",
                    json={
                        "host": "smtp.example.invalid",
                        "port": 587,
                        "security": "STARTTLS",
                        "username": None,
                        "password": None,
                        "clear_password": False,
                        "from_email": "planning" + chr(64) + TEST_DOMAIN,
                        "from_name": "RessourcePlanner",
                        "reply_to": None,
                        "timeout_seconds": 20,
                        "enabled": True,
                    },
                )
                self.assertEqual(configured.status_code, 200, configured.text)
                sent = approver.post(
                    f"/api/v1/communications/project-batches/{batch_id}/send-smtp"
                )
                self.assertEqual(sent.status_code, 200, sent.text)
                self.assertEqual(sent.json()["status"], "COMMUNICATED")
                self.assertEqual(len(smtp.attempt_cc), 1)
                self.assertIn(generator, smtp.attempt_cc[0])
                self.assertNotIn(reviewer, smtp.attempt_cc[0])

                engine = create_sql_engine(url)
                factory = create_session_factory(engine)
                try:
                    with factory.begin() as session:
                        shift = session.get(Shift, "S1")
                        assert shift is not None
                        shift.hours = Decimal("6")
                finally:
                    engine.dispose()
                delta = origin.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23&add_generator_cc=true"
                )
                self.assertEqual(delta.status_code, 200, delta.text)
                self.assertEqual(delta.json()["mode"], "project_planning_change")
                self.assertTrue(all(
                    generator in [r["email"] for r in draft["cc_recipients"]]
                    for draft in delta.json()["drafts"]
                ))

    def test_727_cc_requires_explicit_valid_authenticated_email(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            for invalid in (None, "invalid", "two@@example.test", "a b@" "example.test"):
                with self.subTest(invalid=invalid):
                    app = create_api_app(
                        url,
                        auth_resolver=self._generator_resolver(invalid, "invalid-generator"),
                    )
                    with TestClient(app) as client:
                        unchanged = client.get(
                            "/api/v1/communications/project-preview?week_start=2026-09-23"
                        )
                        self.assertEqual(unchanged.status_code, 200, unchanged.text)
                        invalid_preview = client.get(
                            "/api/v1/communications/project-preview"
                            "?week_start=2026-09-23&add_generator_cc=true"
                        )
                        self.assertEqual(invalid_preview.status_code, 422, invalid_preview.text)
                        self.assertEqual(
                            invalid_preview.json()["error"]["code"],
                            "project_communication_generator_email_invalid",
                        )

    def test_727_generator_already_in_to_or_cc_is_not_duplicated(self) -> None:
        for generator in (
            "PM" + chr(64) + TEST_DOMAIN,
            "RESOURCE-ONE" + chr(64) + TEST_DOMAIN,
        ):
            with self.subTest(generator=generator), TemporaryDirectory() as directory:
                app = create_api_app(
                    self._database(directory),
                    auth_resolver=self._generator_resolver(generator, "generator"),
                )
                with TestClient(app) as client:
                    response = client.get(
                        "/api/v1/communications/project-preview"
                        "?week_start=2026-09-23&add_generator_cc=true"
                    )
                self.assertEqual(response.status_code, 200, response.text)
                draft = response.json()["drafts"][0]
                recipients = draft["to_recipients"] + draft["cc_recipients"]
                values = [
                    row["email"].casefold()
                    for row in recipients if row["email"]
                ]
                self.assertEqual(values.count(generator.casefold()), 1)

    def test_http_preview_exposes_one_project_message_with_to_cc_and_diagnostics(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(
                self._database(directory),
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(len(payload["drafts"]), 1)
        draft = payload["drafts"][0]
        self.assertEqual(draft["message_key"], "project:P1")
        self.assertEqual(draft["audience"], "project")
        self.assertEqual(
            draft["to_recipient"]["email"],
            "pm" + chr(64) + TEST_DOMAIN,
        )
        self.assertEqual(
            [row["email"] for row in draft["cc_recipients"]],
            [
                "resource-two" + chr(64) + TEST_DOMAIN,
                "resource-one" + chr(64) + TEST_DOMAIN,
            ],
        )
        self.assertTrue(draft["approvable"])
        self.assertIn("Responsable approuvé", draft["body"])
        self.assertIn("555" + "-" + "0100", draft["body"])
        self.assertFalse(
            any(
                row["code"] == "PROJECT_CC_EMAIL_MISSING"
                for row in draft["diagnostics"]
            )
        )

    def test_project_workflow_persists_to_cc_snapshot_and_builds_delta(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app) as client:
                preview = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                preview_payload = preview.json()
                self.assertEqual(
                    preview_payload["mode"],
                    "project_confirmation",
                )

                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview_payload[
                            "snapshot_fingerprint"
                        ],
                        "reviews": [],
                    },
                )
                self.assertEqual(prepared.status_code, 201, prepared.text)
                batch = prepared.json()
                self.assertEqual(batch["model_version"], "project_v2")
                self.assertEqual(len(batch["messages"]), 1)
                message = batch["messages"][0]
                self.assertEqual(message["message_key"], "project:P1")
                self.assertEqual(
                    message["recipient_email"],
                    "pm" + chr(64) + TEST_DOMAIN,
                )
                self.assertEqual(
                    message["cc_emails"],
                    [
                        "resource-two" + chr(64) + TEST_DOMAIN,
                        "resource-one" + chr(64) + TEST_DOMAIN,
                    ],
                )
                self.assertNotIn(
                    "PROJECT_CC_EMAIL_MISSING",
                    message["diagnostics_json"],
                )

                approved = client.post(
                    f"/api/v1/communications/project-batches/{batch['id']}/approve"
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                communicated = client.post(
                    f"/api/v1/communications/project-batches/{batch['id']}/mark-communicated"
                )
                self.assertEqual(
                    communicated.status_code,
                    200,
                    communicated.text,
                )

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                try:
                    with factory.begin() as session:
                        shift = session.get(Shift, "S1")
                        assert shift is not None
                        shift.hours = Decimal("6")
                finally:
                    engine.dispose()

                delta = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(delta.status_code, 200, delta.text)
                delta_payload = delta.json()
                self.assertTrue(delta_payload["has_communicated_baseline"])
                self.assertEqual(
                    delta_payload["mode"],
                    "project_planning_change",
                )
                self.assertEqual(len(delta_payload["drafts"]), 1)
                self.assertIn("6 h", delta_payload["drafts"][0]["body"])
                self.assertNotEqual(
                    delta_payload["snapshot_fingerprint"],
                    delta_payload["baseline_fingerprint"],
                )

    def test_854_persisted_batch_exposes_business_number_without_changing_identity(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            project_id = self._add_r29_project(database_url)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app) as client:
                preview = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                preview_payload = preview.json()
                r29_draft = next(
                    row
                    for row in preview_payload["drafts"]
                    if row["project_id"] == project_id
                )
                self.assertEqual(r29_draft["project_number"], "5110")
                self.assertEqual(r29_draft["message_key"], f"project:{project_id}")

                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview_payload["snapshot_fingerprint"],
                        "reviews": [],
                    },
                )
                self.assertEqual(prepared.status_code, 201, prepared.text)
                message = next(
                    row
                    for row in prepared.json()["messages"]
                    if row["project_id"] == project_id
                )
                self.assertEqual(message["project_id"], project_id)
                self.assertEqual(message["message_key"], f"project:{project_id}")
                self.assertEqual(message["project_number"], "5110")

                reloaded = client.get(
                    "/api/v1/communications/project-batches"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(reloaded.status_code, 200, reloaded.text)
                reloaded_message = next(
                    row
                    for row in reloaded.json()[0]["messages"]
                    if row["project_id"] == project_id
                )
                self.assertEqual(reloaded_message["project_id"], project_id)
                self.assertEqual(
                    reloaded_message["message_key"],
                    f"project:{project_id}",
                )
                self.assertEqual(reloaded_message["project_number"], "5110")

    def test_854_missing_or_invalid_snapshot_metadata_keeps_batch_readable(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app) as client:
                preview = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview.json()["snapshot_fingerprint"],
                        "reviews": [],
                    },
                )
                self.assertEqual(prepared.status_code, 201, prepared.text)
                batch_id = prepared.json()["id"]

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                try:
                    with factory.begin() as session:
                        batch = session.get(CommunicationBatchRow, batch_id)
                        assert batch is not None
                        batch.project_snapshot_json = None
                finally:
                    engine.dispose()

                missing = client.get(
                    "/api/v1/communications/project-batches"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(missing.status_code, 200, missing.text)
                missing_message = missing.json()[0]["messages"][0]
                self.assertEqual(missing_message["message_key"], "project:P1")
                self.assertEqual(missing_message["project_id"], "P1")
                self.assertIsNone(missing_message["project_number"])

                engine = create_sql_engine(database_url)
                factory = create_session_factory(engine)
                try:
                    with factory.begin() as session:
                        batch = session.get(CommunicationBatchRow, batch_id)
                        assert batch is not None
                        batch.project_snapshot_json = "{invalid-json"
                finally:
                    engine.dispose()

                invalid = client.get(
                    "/api/v1/communications/project-batches"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(invalid.status_code, 200, invalid.text)
                invalid_message = invalid.json()[0]["messages"][0]
                self.assertEqual(invalid_message["message_key"], "project:P1")
                self.assertEqual(invalid_message["project_id"], "P1")
                self.assertIsNone(invalid_message["project_number"])

    def test_708b_assets_follow_canonical_shift_applicability_change_release_and_dedup(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    session.add_all(
                        [
                            AssetType(
                                id="TYPE-VEH-708B",
                                code="VEH",
                                label="Véhicule",
                                category="VEHICLE",
                            ),
                            AssetType(
                                id="TYPE-EQ-708B",
                                code="EQ",
                                label="Équipement",
                                category="EQUIPMENT",
                            ),
                        ]
                    )
                    session.flush()
                    session.add_all(
                        [
                            Asset(
                                id="ASSET-VEH-708B",
                                code="V-708B",
                                label="Camion 708B",
                                asset_type_id="TYPE-VEH-708B",
                            ),
                            Asset(
                                id="ASSET-VEH-NEW-708B",
                                code="V-NEW-708B",
                                label="Camion remplacement 708B",
                                asset_type_id="TYPE-VEH-708B",
                            ),
                            Asset(
                                id="ASSET-EQ-708B",
                                code="EQ-708B",
                                label="Nacelle 708B",
                                asset_type_id="TYPE-EQ-708B",
                            ),
                            Asset(
                                id="ASSET-OTHER-708B",
                                code="OTHER-708B",
                                label="Actif autre ressource",
                                asset_type_id="TYPE-EQ-708B",
                            ),
                            Asset(
                                id="ASSET-FUTURE-708B",
                                code="FUTURE-708B",
                                label="Actif hors date",
                                asset_type_id="TYPE-EQ-708B",
                            ),
                            Asset(
                                id="ASSET-CONTEXT-708B",
                                code="CTX-708B",
                                label="Actif segment sans opérateur",
                                asset_type_id="TYPE-EQ-708B",
                            ),
                            Asset(
                                id="ASSET-REQUEST-843",
                                code="REQ-843",
                                label="Actif demande 843",
                                asset_type_id="TYPE-EQ-708B",
                            ),
                        ]
                    )
                    session.add(
                        Shift(
                            id="S1-SECOND-708B",
                            resource_requirement_id="REQ1",
                            resource_id="R1",
                            work_date=WEEK,
                            hours=Decimal("1"),
                            allocation_type="Flexible",
                            outside_standard_hours=False,
                        )
                    )
                    session.flush()
                    session.add_all(
                        [
                            AssetRequirement(
                                id="AREQ-SHIFT-708B",
                                project_id="P1",
                                origin="SHIFT_AD_HOC",
                                shift_id="S1",
                                asset_type_id="TYPE-EQ-708B",
                                start_date=WEEK,
                                end_date=WEEK,
                                status="Affecté",
                            ),
                            AssetRequirement(
                                id="AREQ-CONT-708B",
                                project_id=None,
                                origin="RESOURCE_PERIOD",
                                context_resource_id="R1",
                                asset_type_id="TYPE-VEH-708B",
                                start_date=WEEK,
                                end_date=None,
                                resource_period_mode="CONTINUOUS",
                                status="Affecté",
                            ),
                            AssetRequirement(
                                id="AREQ-OTHER-708B",
                                project_id=None,
                                origin="RESOURCE_PERIOD",
                                context_resource_id="R2",
                                asset_type_id="TYPE-EQ-708B",
                                start_date=WEEK,
                                end_date=None,
                                resource_period_mode="CONTINUOUS",
                                status="Affecté",
                            ),
                            AssetRequirement(
                                id="AREQ-FUTURE-708B",
                                project_id=None,
                                origin="RESOURCE_PERIOD",
                                context_resource_id="R1",
                                asset_type_id="TYPE-EQ-708B",
                                start_date=date(2026, 9, 22),
                                end_date=date(2026, 9, 22),
                                resource_period_mode="BOUNDED",
                                status="Affecté",
                            ),
                            AssetRequirement(
                                id="AREQ-REQUEST-843",
                                project_id="P1",
                                origin="REQUEST",
                                workforce_request_id="D1",
                                source_request_line_id="L1",
                                approved_entry_key="REQ-ASSET-843",
                                slot_index=0,
                                asset_type_id="TYPE-EQ-708B",
                                start_date=WEEK,
                                end_date=WEEK,
                                status="Affecté",
                            ),
                            AssetRequirement(
                                id="AREQ-CONTEXT-708B",
                                project_id="P1",
                                origin="SEGMENT",
                                resource_requirement_id="REQ1",
                                asset_type_id="TYPE-EQ-708B",
                                start_date=WEEK,
                                end_date=WEEK,
                                status="Affecté",
                            ),
                        ]
                    )
                    session.flush()
                    session.add_all(
                        [
                            AssetAllocation(
                                id="ALLOC-SHIFT-708B",
                                asset_requirement_id="AREQ-SHIFT-708B",
                                asset_id="ASSET-EQ-708B",
                                operator_resource_id="R1",
                                start_date=WEEK,
                                end_date=WEEK,
                                locked=True,
                                source="MANUAL",
                            ),
                            AssetAllocation(
                                id="ALLOC-CONT-708B",
                                asset_requirement_id="AREQ-CONT-708B",
                                asset_id="ASSET-VEH-708B",
                                operator_resource_id="R1",
                                start_date=WEEK,
                                end_date=None,
                                locked=True,
                                source="MANUAL",
                            ),
                            AssetAllocation(
                                id="ALLOC-OTHER-708B",
                                asset_requirement_id="AREQ-OTHER-708B",
                                asset_id="ASSET-OTHER-708B",
                                operator_resource_id="R2",
                                start_date=WEEK,
                                end_date=None,
                                locked=True,
                                source="MANUAL",
                            ),
                            AssetAllocation(
                                id="ALLOC-FUTURE-708B",
                                asset_requirement_id="AREQ-FUTURE-708B",
                                asset_id="ASSET-FUTURE-708B",
                                operator_resource_id="R1",
                                start_date=date(2026, 9, 22),
                                end_date=date(2026, 9, 22),
                                locked=True,
                                source="MANUAL",
                            ),
                            AssetAllocation(
                                id="ALLOC-REQUEST-843",
                                asset_requirement_id="AREQ-REQUEST-843",
                                asset_id="ASSET-REQUEST-843",
                                operator_resource_id="R1",
                                start_date=WEEK,
                                end_date=WEEK,
                                locked=True,
                                source="MANUAL",
                            ),
                            AssetAllocation(
                                id="ALLOC-CONTEXT-708B",
                                asset_requirement_id="AREQ-CONTEXT-708B",
                                asset_id="ASSET-CONTEXT-708B",
                                operator_resource_id=None,
                                start_date=WEEK,
                                end_date=WEEK,
                                locked=True,
                                source="MANUAL",
                            ),
                        ]
                    )

                with factory() as session:
                    repository = SqlProjectCommunicationRepository(
                        session,
                        operational_contacts=OperationalContactService(
                            SqlOperationalContactRepository(session)
                        ),
                    )
                    assignments = repository.list_assignments(
                        week_start=WEEK,
                        week_end=date(2026, 9, 27),
                    )
                    r1_rows = [
                        row
                        for row in assignments
                        if row.resource_id == "R1"
                    ]
                    self.assertEqual(len(r1_rows), 2)
                    by_shift = {row.shift_id: row for row in r1_rows}
                    self.assertEqual(
                        {asset.asset_label for asset in by_shift["S1"].assets},
                        {"Nacelle 708B", "Camion 708B", "Actif demande 843"},
                    )
                    self.assertEqual(
                        {asset.asset_label for asset in by_shift["S1-SECOND-708B"].assets},
                        {"Camion 708B", "Actif demande 843"},
                    )
                    s2 = next(row for row in assignments if row.shift_id == "S2")
                    self.assertNotIn(
                        "Actif demande 843",
                        {asset.asset_label for asset in s2.assets},
                    )
                    self.assertEqual(
                        [asset.allocation_id for asset in by_shift["S1"].assets].count(
                            "ALLOC-REQUEST-843"
                        ),
                        1,
                    )
                    self.assertNotIn(
                        "Actif autre ressource",
                        {
                            asset.asset_label
                            for row in r1_rows
                            for asset in row.assets
                        },
                    )
                    self.assertNotIn(
                        "Actif hors date",
                        {
                            asset.asset_label
                            for row in r1_rows
                            for asset in row.assets
                        },
                    )
                    self.assertNotIn(
                        "Actif segment sans opérateur",
                        {
                            asset.asset_label
                            for row in r1_rows
                            for asset in row.assets
                        },
                    )

                    projection = ProjectCommunicationService(
                        repository
                    ).project_preview(week_start=WEEK)
                    draft = next(
                        row
                        for row in projection.drafts
                        if row.project_id == "P1"
                    )
                    self.assertIn("Actif : V-708B / REQ-843 / EQ-708B", draft.body)
                    self.assertNotIn("Camion 708B", draft.body)
                    self.assertNotIn("Nacelle 708B", draft.body)

                with factory.begin() as session:
                    continuous = session.get(AssetAllocation, "ALLOC-CONT-708B")
                    owned = session.get(AssetRequirement, "AREQ-SHIFT-708B")
                    assert continuous is not None
                    assert owned is not None
                    continuous.asset_id = "ASSET-VEH-NEW-708B"
                    owned.status = "Annulé"

                with factory() as session:
                    repository = SqlProjectCommunicationRepository(
                        session,
                        operational_contacts=OperationalContactService(
                            SqlOperationalContactRepository(session)
                        ),
                    )
                    changed = ProjectCommunicationService(
                        repository
                    ).project_preview(week_start=WEEK)
                    draft = next(
                        row for row in changed.drafts if row.project_id == "P1"
                    )
                    self.assertIn("Actif : V-NEW-708B", draft.body)
                    self.assertNotIn("Camion remplacement 708B", draft.body)
                    self.assertNotIn("Camion 708B", draft.body)
                    self.assertNotIn("Nacelle 708B", draft.body)
            finally:
                engine.dispose()

    def test_quick_shift_default_origin_label_is_hidden_across_preview_graph_and_smtp(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            self._add_quick_shift(
                database_url,
                requirement_id="REQ-QS-DEFAULT",
                shift_id="S-QS-DEFAULT",
                description="Quart rapide",
            )
            self._add_quick_shift(
                database_url,
                requirement_id="REQ-QS-CUSTOM",
                shift_id="S-QS-CUSTOM",
                description="Inspection urgente",
                hours="1",
            )
            asset_engine = create_sql_engine(database_url)
            asset_factory = create_session_factory(asset_engine)
            try:
                with asset_factory.begin() as session:
                    session.add(
                        AssetType(
                            id="TYPE-MAIL-708B",
                            code="EQ-708B-MAIL",
                            label="Équipement",
                            category="EQUIPMENT",
                        )
                    )
                    session.flush()
                    session.add(
                        Asset(
                            id="ASSET-MAIL-708B",
                            code="NAC-MAIL-708B",
                            label="Nacelle courriel 708B",
                            asset_type_id="TYPE-MAIL-708B",
                        )
                    )
                    session.add(
                        AssetRequirement(
                            id="AREQ-MAIL-708B",
                            project_id="P1",
                            origin="SHIFT_AD_HOC",
                            shift_id="S-QS-DEFAULT",
                            asset_type_id="TYPE-MAIL-708B",
                            start_date=WEEK,
                            end_date=WEEK,
                            status="Affecté",
                        )
                    )
                    session.flush()
                    session.add(
                        AssetAllocation(
                            id="ALLOC-MAIL-708B",
                            asset_requirement_id="AREQ-MAIL-708B",
                            asset_id="ASSET-MAIL-708B",
                            operator_resource_id="R1",
                            start_date=WEEK,
                            end_date=WEEK,
                            locked=True,
                            source="MANUAL",
                        )
                    )
            finally:
                asset_engine.dispose()
            transport = FakeProjectDraftTransport()
            smtp = FakeSmtpDeliveryClient()
            app = create_api_app(
                database_url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                communication_transport=transport,
                smtp_client=smtp,
            )

            with TestClient(app) as client:
                preview_response = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(
                    preview_response.status_code,
                    200,
                    preview_response.text,
                )
                preview = preview_response.json()
                project_draft = next(
                    row for row in preview["drafts"] if row["project_id"] == "P1"
                )
                preview_body = project_draft["body"]
                self.assertNotIn("Quart rapide", preview_body)
                self.assertNotIn("Quick Shift", preview_body)
                self.assertIn("Travaux planifiés", preview_body)
                self.assertIn("Inspection urgente", preview_body)
                self.assertIn("Installation approuvée", preview_body)
                self.assertIn("Technicien ressource", preview_body)
                self.assertIn("2 h", preview_body)
                self.assertIn("1 h", preview_body)
                self.assertIn("Actif : NAC-MAIL-708B", preview_body)
                self.assertNotIn("Nacelle courriel 708B", preview_body)

                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview["snapshot_fingerprint"],
                        "reviews": [],
                    },
                )
                self.assertEqual(prepared.status_code, 201, prepared.text)
                batch = prepared.json()
                batch_id = batch["id"]
                persisted_body = next(
                    message["body"]
                    for message in batch["messages"]
                    if message["project_id"] == "P1"
                )
                self.assertEqual(persisted_body, preview_body)

                approved = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/approve"
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                created = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/create-drafts"
                )
                self.assertEqual(created.status_code, 200, created.text)
                self.assertEqual(len(transport.messages), 1)
                self.assertEqual(transport.messages[0].body, preview_body)

                configured = client.put(
                    "/api/v1/admin/settings/smtp",
                    json={
                        "host": "smtp.example.invalid",
                        "port": 587,
                        "security": "STARTTLS",
                        "username": None,
                        "password": None,
                        "clear_password": False,
                        "from_email": "planning" + chr(64) + TEST_DOMAIN,
                        "from_name": "RessourcePlanner",
                        "reply_to": None,
                        "timeout_seconds": 20,
                        "enabled": True,
                    },
                )
                self.assertEqual(configured.status_code, 200, configured.text)
                sent = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/send-smtp"
                )
                self.assertEqual(sent.status_code, 200, sent.text)
                self.assertEqual(smtp.attempt_bodies, [preview_body])

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    default_requirement = session.get(
                        ResourceRequirement,
                        "REQ-QS-DEFAULT",
                    )
                    custom_requirement = session.get(
                        ResourceRequirement,
                        "REQ-QS-CUSTOM",
                    )
                    self.assertIsNotNone(default_requirement)
                    self.assertIsNotNone(custom_requirement)
                    assert default_requirement is not None
                    assert custom_requirement is not None
                    self.assertEqual(
                        default_requirement.origin,
                        ORIGIN_QUICK_SHIFT,
                    )
                    self.assertEqual(
                        default_requirement.description,
                        "Quart rapide",
                    )
                    self.assertEqual(
                        custom_requirement.origin,
                        ORIGIN_QUICK_SHIFT,
                    )
                    self.assertEqual(
                        custom_requirement.description,
                        "Inspection urgente",
                    )
            finally:
                engine.dispose()

    def test_project_create_drafts_passes_persisted_to_and_cc_to_transport(self) -> None:
        with TemporaryDirectory() as directory:
            transport = FakeProjectDraftTransport()
            app = create_api_app(
                self._database(directory),
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                communication_transport=transport,
            )
            with TestClient(app) as client:
                preview = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                ).json()
                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview["snapshot_fingerprint"],
                        "reviews": [],
                    },
                )
                self.assertEqual(prepared.status_code, 201, prepared.text)
                batch_id = prepared.json()["id"]
                approved = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/approve"
                )
                self.assertEqual(approved.status_code, 200, approved.text)

                before_creation = client.get(
                    f"/api/v1/communications/project-batches/{batch_id}/draft-download"
                )
                self.assertEqual(before_creation.status_code, 409, before_creation.text)
                self.assertEqual(
                    before_creation.json()["error"]["code"],
                    "project_communication_drafts_not_created",
                )

                created = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/create-drafts"
                )
                self.assertEqual(created.status_code, 200, created.text)
                self.assertIsNotNone(created.json()["drafts_created_at"])

                downloaded = client.get(
                    f"/api/v1/communications/project-batches/{batch_id}/draft-download"
                )
                self.assertEqual(downloaded.status_code, 200, downloaded.text)
                self.assertTrue(
                    downloaded.headers["content-type"].startswith("message/rfc822")
                )
                self.assertIn(
                    'filename="projet-P1.eml"',
                    downloaded.headers["content-disposition"],
                )
                self.assertEqual(downloaded.headers["cache-control"], "private, no-store")

                retried = client.get(
                    f"/api/v1/communications/project-batches/{batch_id}/draft-download"
                )
                self.assertEqual(retried.status_code, 200, retried.text)
                self.assertEqual(retried.content, downloaded.content)

        self.assertEqual(transport.create_calls, 1)
        self.assertEqual(len(transport.messages), 1)
        self.assertEqual(
            transport.messages[0].recipient_email,
            "pm" + chr(64) + TEST_DOMAIN,
        )
        self.assertEqual(
            transport.messages[0].cc_emails,
            (
                "resource-two" + chr(64) + TEST_DOMAIN,
                "resource-one" + chr(64) + TEST_DOMAIN,
            ),
        )

        mime = BytesParser(policy=policy.default).parsebytes(downloaded.content)
        self.assertEqual(mime["Subject"], transport.messages[0].subject)
        self.assertEqual(
            mime["To"],
            ", ".join(transport.messages[0].to_emails),
        )
        self.assertEqual(
            mime["Cc"],
            ", ".join(transport.messages[0].cc_emails),
        )
        self.assertEqual(
            mime.get_content().replace("\r\n", "\n").rstrip("\n"),
            transport.messages[0].body.replace("\r\n", "\n").rstrip("\n"),
        )


    def test_project_batch_subset_excludes_unchecked_messages_from_graph_and_smtp(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            self._add_second_project(database_url)
            transport = FakeProjectDraftTransport()
            smtp = FakeSmtpDeliveryClient()
            app = create_api_app(
                database_url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                communication_transport=transport,
                smtp_client=smtp,
            )
            with TestClient(app) as client:
                preview = client.get(
                    "/api/v1/communications/project-preview?week_start=2026-09-23"
                ).json()
                self.assertEqual(len(preview["drafts"]), 2)
                keys = {
                    draft["project_id"]: draft["message_key"]
                    for draft in preview["drafts"]
                }
                excluded = [
                    {"message_key": key, "include": False}
                    for key in keys.values()
                ]
                empty = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview["snapshot_fingerprint"],
                        "reviews": excluded,
                    },
                )
                self.assertEqual(empty.status_code, 422, empty.text)
                self.assertEqual(
                    empty.json()["error"]["code"],
                    "project_communication_no_included_messages",
                )
                stale = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": "0" * 64,
                        "reviews": [{"message_key": keys["P2"], "include": False}],
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["error"]["code"],
                    "project_communication_preview_stale",
                )

                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview["snapshot_fingerprint"],
                        "reviews": [
                            {"message_key": keys["P1"], "include": True},
                            {"message_key": keys["P2"], "include": False},
                        ],
                    },
                )
                self.assertEqual(prepared.status_code, 201, prepared.text)
                batch = prepared.json()
                self.assertEqual(
                    {message["project_id"]: message["included"] for message in batch["messages"]},
                    {"P1": True, "P2": False},
                )
                batch_id = batch["id"]
                reloaded = client.get(
                    "/api/v1/communications/project-batches?week_start=2026-09-23"
                ).json()
                self.assertEqual(
                    {message["project_id"]: message["included"] for message in reloaded[0]["messages"]},
                    {"P1": True, "P2": False},
                )
                approved = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/approve"
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                created = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/create-drafts"
                )
                self.assertEqual(created.status_code, 200, created.text)
                self.assertEqual(created.json()["drafts_created_count"], 1)
                downloaded = client.get(
                    f"/api/v1/communications/project-batches/{batch_id}/draft-download"
                )
                self.assertEqual(downloaded.status_code, 200, downloaded.text)
                self.assertTrue(downloaded.headers["content-type"].startswith("message/rfc822"))
                self.assertIn('filename="projet-P1.eml"', downloaded.headers["content-disposition"])

                configured = client.put(
                    "/api/v1/admin/settings/smtp",
                    json={
                        "host": "smtp.example.invalid",
                        "port": 587,
                        "security": "STARTTLS",
                        "username": None,
                        "password": None,
                        "clear_password": False,
                        "from_email": "planning" + chr(64) + TEST_DOMAIN,
                        "from_name": "RessourcePlanner",
                        "reply_to": None,
                        "timeout_seconds": 20,
                        "enabled": True,
                    },
                )
                self.assertEqual(configured.status_code, 200, configured.text)
                sent = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/send-smtp"
                )
                self.assertEqual(sent.status_code, 200, sent.text)
                self.assertEqual(sent.json()["status"], "COMMUNICATED")
                self.assertEqual(
                    next(message for message in sent.json()["messages"] if message["project_id"] == "P2")["deliveries"],
                    [],
                )
            self.assertEqual(transport.create_calls, 1)
            self.assertEqual(len(transport.messages), 1)
            self.assertIn("1000", transport.messages[0].subject)
            self.assertEqual(len(smtp.attempt_subjects), 1)
            self.assertIn("1000", smtp.attempt_subjects[0])

    def test_project_draft_download_returns_zip_for_multiple_messages(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            self._add_second_project(database_url)
            transport = FakeProjectDraftTransport()
            app = create_api_app(
                database_url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                communication_transport=transport,
            )
            with TestClient(app) as client:
                preview = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                ).json()
                self.assertEqual(len(preview["drafts"]), 2)
                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview["snapshot_fingerprint"],
                        "reviews": [],
                    },
                )
                batch_id = prepared.json()["id"]
                self.assertEqual(
                    client.post(
                        f"/api/v1/communications/project-batches/{batch_id}/approve"
                    ).status_code,
                    200,
                )
                created = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/create-drafts"
                )
                self.assertEqual(created.status_code, 200, created.text)
                downloaded = client.get(
                    f"/api/v1/communications/project-batches/{batch_id}/draft-download"
                )
                self.assertEqual(downloaded.status_code, 200, downloaded.text)
                self.assertEqual(downloaded.headers["content-type"], "application/zip")
                self.assertIn(".zip", downloaded.headers["content-disposition"])

        self.assertEqual(transport.create_calls, 1)
        with ZipFile(BytesIO(downloaded.content)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"projet-P1.eml", "projet-P2.eml"},
            )
            messages = [
                BytesParser(policy=policy.default).parsebytes(archive.read(name))
                for name in archive.namelist()
            ]
        self.assertEqual(
            {str(message["Subject"]) for message in messages},
            {message.subject for message in transport.messages},
        )
        for message in messages:
            self.assertEqual(
                message["To"],
                ", ".join(transport.messages[0].to_emails),
            )

    def test_project_draft_download_failure_preserves_created_audit_and_is_retryable(self) -> None:
        with TemporaryDirectory() as directory:
            transport = FakeProjectDraftTransport()
            app = create_api_app(
                self._database(directory),
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                communication_transport=transport,
            )
            with TestClient(app) as client:
                preview = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                ).json()
                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview["snapshot_fingerprint"],
                        "reviews": [],
                    },
                )
                batch_id = prepared.json()["id"]
                client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/approve"
                )
                created = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/create-drafts"
                )
                self.assertEqual(created.status_code, 200, created.text)

                with patch(
                    "app.application.project_communications._project_message_eml",
                    side_effect=UnicodeError("synthetic MIME failure"),
                ):
                    failed = client.get(
                        f"/api/v1/communications/project-batches/{batch_id}/draft-download"
                    )
                self.assertEqual(failed.status_code, 503, failed.text)
                self.assertEqual(
                    failed.json()["error"]["code"],
                    "project_communication_draft_download_failed",
                )

                batches = client.get(
                    "/api/v1/communications/project-batches"
                    "?week_start=2026-09-23"
                )
                persisted = next(
                    row for row in batches.json() if row["id"] == batch_id
                )
                self.assertIsNotNone(persisted["drafts_created_at"])

                retry = client.get(
                    f"/api/v1/communications/project-batches/{batch_id}/draft-download"
                )
                self.assertEqual(retry.status_code, 200, retry.text)

        self.assertEqual(transport.create_calls, 1)

    def test_smtp_partial_failure_retries_only_unsent_message(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            self._add_second_project(database_url)
            smtp = FakeSmtpDeliveryClient()
            app = create_api_app(
                database_url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
                smtp_client=smtp,
            )
            with TestClient(app) as client:
                configured = client.put(
                    "/api/v1/admin/settings/smtp",
                    json={
                        "host": "smtp.example.invalid",
                        "port": 587,
                        "security": "STARTTLS",
                        "username": None,
                        "password": None,
                        "clear_password": False,
                        "from_email": "planning" + chr(64) + TEST_DOMAIN,
                        "from_name": "RessourcePlanner",
                        "reply_to": None,
                        "timeout_seconds": 20,
                        "enabled": True,
                    },
                )
                self.assertEqual(configured.status_code, 200, configured.text)

                preview = client.get(
                    "/api/v1/communications/project-preview"
                    "?week_start=2026-09-23"
                )
                self.assertEqual(preview.status_code, 200, preview.text)
                preview_payload = preview.json()
                self.assertEqual(len(preview_payload["drafts"]), 2)

                prepared = client.post(
                    "/api/v1/communications/project-batches",
                    json={
                        "week_start": "2026-09-23",
                        "expected_fingerprint": preview_payload[
                            "snapshot_fingerprint"
                        ],
                        "reviews": [],
                    },
                )
                self.assertEqual(prepared.status_code, 201, prepared.text)
                batch_id = prepared.json()["id"]

                approved = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/approve"
                )
                self.assertEqual(approved.status_code, 200, approved.text)
                self.assertEqual(smtp.attempt_subjects, [])

                first = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/send-smtp"
                )
                self.assertEqual(first.status_code, 200, first.text)
                first_payload = first.json()
                self.assertEqual(first_payload["status"], "APPROVED")

                first_deliveries = {
                    message["project_id"]: next(
                        delivery
                        for delivery in message["deliveries"]
                        if delivery["provider"] == "SMTP"
                    )
                    for message in first_payload["messages"]
                }
                self.assertEqual(first_deliveries["P1"]["status"], "SENT")
                self.assertEqual(first_deliveries["P1"]["attempt_count"], 1)
                self.assertEqual(first_deliveries["P2"]["status"], "FAILED")
                self.assertEqual(first_deliveries["P2"]["attempt_count"], 1)

                second = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/send-smtp"
                )
                self.assertEqual(second.status_code, 200, second.text)
                second_payload = second.json()
                self.assertEqual(second_payload["status"], "COMMUNICATED")

                second_deliveries = {
                    message["project_id"]: next(
                        delivery
                        for delivery in message["deliveries"]
                        if delivery["provider"] == "SMTP"
                    )
                    for message in second_payload["messages"]
                }
                self.assertEqual(second_deliveries["P1"]["status"], "SENT")
                self.assertEqual(second_deliveries["P1"]["attempt_count"], 1)
                self.assertEqual(second_deliveries["P2"]["status"], "SENT")
                self.assertEqual(second_deliveries["P2"]["attempt_count"], 2)

        project_1000_attempts = [
            subject for subject in smtp.attempt_subjects if "1000" in subject
        ]
        project_2000_attempts = [
            subject for subject in smtp.attempt_subjects if "2000" in subject
        ]
        self.assertEqual(len(project_1000_attempts), 1)
        self.assertEqual(len(project_2000_attempts), 2)

    def test_http_contract_exposes_project_projection_under_communications(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(
                self._database(directory),
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/communications/project-projection"
                    "?week_start=2026-09-23"
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["week_start"], "2026-09-21")
        self.assertEqual(payload["week_end"], "2026-09-27")
        self.assertEqual(payload["projects"][0]["project_number"], "1000")
        self.assertEqual(
            payload["projects"][0]["days"][0]["tasks"][0]["task_description"],
            "Installation approuvée",
        )


if __name__ == "__main__":
    unittest.main()
