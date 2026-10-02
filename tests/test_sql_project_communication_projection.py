from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application.communications import CommunicationTransportResult
from app.domain.planning_engine import MISSING_ALLOCATION_TYPE
from app.application.operational_contacts import OperationalContactService
from app.application.project_communications import ProjectCommunicationService
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
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
    DIAGNOSTIC_RESOURCE_USER_LINK_MISSING,
    SqlProjectCommunicationRepository,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


WEEK = date(2026, 9, 21)
TEST_DOMAIN = "example.test"


class FakeSmtpDeliveryClient:
    def __init__(self) -> None:
        self.attempt_subjects: list[str] = []
        self._failed_once = False

    def test_connection(self, configuration) -> None:
        return None

    def send_message(self, configuration, message, *, message_id: str) -> str:
        self.attempt_subjects.append(message.subject)
        if "2000" in message.subject and not self._failed_once:
            self._failed_once = True
            raise RuntimeError("synthetic smtp failure")
        return message_id


class FakeProjectDraftTransport:
    def __init__(self) -> None:
        self.messages = ()

    def create_drafts(self, messages):
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
    def _seed(session) -> None:
        pm_email = "pm" + chr(64) + TEST_DOMAIN
        tech_email = "tech" + chr(64) + TEST_DOMAIN
        legacy_email = "legacy" + chr(64) + TEST_DOMAIN
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
                    email=legacy_email,
                    active=True,
                ),
                Resource(
                    id="R2",
                    external_id="EMP-2",
                    name="Ressource sans utilisateur",
                    email=legacy_email,
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

    def test_co_manager_only_change_does_not_change_project_message_fingerprint(self) -> None:
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
                                email="co@example.test",
                                source="APP_USER",
                            )
                        )
                        session.add(
                            AppUser(
                                id="U-CO",
                                issuer=None,
                                subject=None,
                                display_name="Co chargé RP",
                                email="co@example.test",
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

        before_payload = before.json()
        after_payload = after.json()
        self.assertEqual(
            after_payload["snapshot_fingerprint"],
            before_payload["snapshot_fingerprint"],
        )
        self.assertEqual(
            after_payload["drafts"][0]["to_recipient"],
            before_payload["drafts"][0]["to_recipient"],
        )
        self.assertEqual(
            after_payload["drafts"][0]["cc_recipients"],
            before_payload["drafts"][0]["cc_recipients"],
        )

    def test_projection_uses_approved_context_and_user_backed_contacts(self) -> None:
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
        self.assertEqual(tech.contact.email, "tech" + chr(64) + TEST_DOMAIN)
        self.assertNotEqual(tech.contact.email, "legacy" + chr(64) + TEST_DOMAIN)

    def test_unlinked_resource_is_explicitly_diagnosed(self) -> None:
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
        missing = next(row for row in resources if row.resource_id == "R2")
        self.assertIsNone(missing.contact.email)
        self.assertIn(DIAGNOSTIC_RESOURCE_USER_LINK_MISSING, missing.diagnostics)

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
            ["tech" + chr(64) + TEST_DOMAIN],
        )
        self.assertTrue(draft["approvable"])
        self.assertIn("Responsable approuvé", draft["body"])
        self.assertIn("555" + "-" + "0100", draft["body"])
        self.assertTrue(
            any(
                row["code"] == "PROJECT_CC_EMAIL_MISSING"
                and row["entity_id"] == "R2"
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
                    ["tech" + chr(64) + TEST_DOMAIN],
                )
                self.assertIn(
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
                created = client.post(
                    f"/api/v1/communications/project-batches/{batch_id}/create-drafts"
                )
                self.assertEqual(created.status_code, 200, created.text)

        self.assertEqual(len(transport.messages), 1)
        self.assertEqual(
            transport.messages[0].recipient_email,
            "pm" + chr(64) + TEST_DOMAIN,
        )
        self.assertEqual(
            transport.messages[0].cc_emails,
            ("tech" + chr(64) + TEST_DOMAIN,),
        )

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
