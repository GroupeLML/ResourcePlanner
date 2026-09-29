"""Serve the built React V2 application against a fresh SQLite E2E fixture.

This process is intended for Playwright only. Authentication is resolved from
``X-E2E-Role`` so browser contexts can exercise real authorization middleware
without an external OIDC provider. No production authentication path is changed.
"""

from __future__ import annotations

import argparse
from datetime import time
from pathlib import Path
import sys

import uvicorn
from cryptography.fernet import Fernet
from fastapi import Request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application.communications import (
    CommunicationTransportMessage,
    CommunicationTransportResult,
)
from app.application.resource_classes import ResourceClassConfigurationService
from app.application.security import (
    AuthPrincipal,
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_DELIVERY_CONTRIBUTOR,
    ROLE_MANAGER,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
)
from app.infrastructure.smtp import FernetSecretCipher
from app.infrastructure.sql.approval_scope_models import (
    ApprovalScope,
    ApprovalScopeApprover,
    TaskApprovalScopeMapping,
)
from app.infrastructure.sql.resource_class_repository import SqlResourceClassRepository
from app.infrastructure.sql import (
    Asset,
    AssetType,
    Base,
    Competency,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceCompetency,
    SqlUserIdentityRepository,
    TaskCatalogEntry,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.dev_user_switcher import (
    DevUserSwitcherRuntime,
    dev_user_switcher_auth_resolver,
)
from app.server.frontend import attach_frontend


E2E_TEAM_LEAD = "TEAM_LEAD"
E2E_DELIVERY_TECHNICIAN = "DELIVERY_TECHNICIAN"

ROLE_IDENTITIES = {
    ROLE_ADMIN: ("Administrateur E2E", None),
    ROLE_PROJECT_MANAGER: ("Chargé E2E", "EMP-PM"),
    ROLE_COORDINATOR: ("Coordonnateur E2E", None),
    ROLE_MANAGER: ("Gestionnaire E2E", None),
    ROLE_TECHNICIAN: ("Technicien Alice", "EMP-ALICE"),
    E2E_TEAM_LEAD: ("Team Lead E2E", None),
    E2E_DELIVERY_TECHNICIAN: ("Technicien Delivery E2E", None),
}

ROLE_APP_USER_SUBJECTS = {
    ROLE_ADMIN: "admin",
    ROLE_PROJECT_MANAGER: "project-manager",
    ROLE_COORDINATOR: "coordinator",
    ROLE_MANAGER: "manager",
    ROLE_TECHNICIAN: "technician-a",
    E2E_TEAM_LEAD: "team-lead",
    E2E_DELIVERY_TECHNICIAN: "delivery-technician",
}

ROLE_ASSIGNED_ROLES = {
    ROLE_ADMIN: (ROLE_ADMIN,),
    ROLE_PROJECT_MANAGER: (ROLE_PROJECT_MANAGER,),
    ROLE_COORDINATOR: (ROLE_COORDINATOR,),
    ROLE_MANAGER: (ROLE_MANAGER,),
    ROLE_TECHNICIAN: (ROLE_TECHNICIAN,),
    E2E_TEAM_LEAD: (ROLE_DELIVERY_CONTRIBUTOR,),
    E2E_DELIVERY_TECHNICIAN: (ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR),
}


class FakePlaywrightSmtpClient:
    """Capture explicit SMTP sends locally; never opens a network connection."""

    def __init__(self) -> None:
        self.test_count = 0
        self.messages: list[tuple[CommunicationTransportMessage, str]] = []

    def test_connection(self, configuration) -> None:
        self.test_count += 1

    def send_message(self, configuration, message, *, message_id: str) -> str:
        self.messages.append((message, message_id))
        return message_id


class FakePlaywrightCommunicationTransport:
    """Capture draft creation locally; never contacts Microsoft 365."""

    def __init__(self) -> None:
        self.messages: tuple[CommunicationTransportMessage, ...] = ()

    def create_drafts(self, messages):
        self.messages = tuple(messages)
        return CommunicationTransportResult(
            provider="fake_playwright",
            created_count=len(self.messages),
        )


def _principal_for_request(request: Request) -> AuthPrincipal | None:
    role = str(request.headers.get("X-E2E-Role") or "").strip().upper()
    identity = ROLE_IDENTITIES.get(role)
    subject = ROLE_APP_USER_SUBJECTS.get(role)
    assigned_roles = ROLE_ASSIGNED_ROLES.get(role)
    if identity is None or subject is None or assigned_roles is None:
        return None

    factory = request.app.state.session_factory
    with factory() as session:
        record = SqlUserIdentityRepository(session).get_by_external_identity(
            "urn:resourceplanner:e2e-dev",
            subject,
        )
    if record is None or not record.active:
        return None

    display_name, employee_external_id = identity
    return AuthPrincipal.from_roles(
        local_user_id=record.user_id,
        issuer=record.issuer,
        subject=record.subject,
        display_name=display_name,
        email=record.email,
        employee_external_id=employee_external_id,
        roles=assigned_roles,
        auth_mode="test",
    )


def _bootstrap_principal() -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=None,
        issuer="urn:resourceplanner:local",
        subject="playwright-bootstrap",
        display_name="Administrateur bootstrap E2E",
        email=None,
        roles=(ROLE_ADMIN,),
        auth_mode="local",
    )


def _combined_auth_resolver(dev_resolver):
    def resolve(request: Request) -> AuthPrincipal | None:
        if str(request.headers.get("X-E2E-Anonymous") or "").strip():
            return None
        if str(request.headers.get("X-E2E-Role") or "").strip():
            return _principal_for_request(request)
        return dev_resolver(request)

    return resolve


def _seed(database_url: str) -> None:
    engine = create_sql_engine(database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    address_domain = "example.test"
    try:
        with factory.begin() as session:
            session.add(
                Project(
                    id="P-251-ID",
                    number="P-251",
                    name="Projet Playwright V2",
                    client="Client E2E",
                    project_manager_external_id="EMP-PM",
                    project_manager_name="Chargé E2E",
                    status="Actif",
                )
            )
            session.add_all(
                [
                    TaskCatalogEntry(
                        id="TASK-P251-110",
                        project_number="P-251",
                        task_code="110",
                        label="INSTALLATION ÉLECTRIQUE E2E",
                        status="Actif",
                        active=True,
                        time_entry_enabled=True,
                        expenses_enabled=False,
                    ),
                    TaskCatalogEntry(
                        id="TASK-P251-210",
                        project_number="P-251",
                        task_code="210",
                        label="AUTOMATISATION E2E",
                        status="Actif",
                        active=True,
                        time_entry_enabled=True,
                        expenses_enabled=False,
                    ),
                ]
            )
            resource_classes = ResourceClassConfigurationService(
                SqlResourceClassRepository(session)
            )
            for code, label, average_cost in (
                ("INSTALLATEUR_ELECTRIQUE", "Installateur électrique", "100.00"),
                ("PROGRAMMEUR", "Programmeur", "125.00"),
                (
                    "INSTALLATEUR_AUTOMATISATION",
                    "Installateur automatisation",
                    "110.00",
                ),
            ):
                resource_classes.create_resource_class(
                    code=code,
                    label=label,
                    average_hourly_cost_cad=average_cost,
                    active=True,
                )
            for task_code, class_code in (
                ("117", "INSTALLATEUR_ELECTRIQUE"),
                ("216", "PROGRAMMEUR"),
                ("217", "INSTALLATEUR_AUTOMATISATION"),
            ):
                resource_classes.create_task_standard(
                    task_code=task_code,
                    resource_class_code=class_code,
                    active=True,
                )

            session.add(
                AssetType(
                    id="AT-LIFT",
                    code="LIFT",
                    label="Nacelle",
                    category="EQUIPMENT",
                )
            )
            session.flush()
            session.add_all(
                [
                    Asset(
                        id="A-LIFT-1",
                        code="NAC-01",
                        label="Nacelle 01",
                        asset_type_id="AT-LIFT",
                    ),
                    Asset(
                        id="A-LIFT-2",
                        code="NAC-02",
                        label="Nacelle 02",
                        asset_type_id="AT-LIFT",
                    ),
                ]
            )
            session.add_all(
                [
                    Competency(id="C-PLC", name="PLC", description="Programmation automate", active=True, sort_order=10),
                    Competency(id="C-SCADA", name="SCADA", description="Supervision industrielle", active=True, sort_order=20),
                    Competency(id="C-MES", name="MES", description="Systèmes d'exécution manufacturière", active=True, sort_order=30),
                ]
            )
            session.add_all(
                [
                    Resource(
                        id="R-ALICE",
                        external_id="EMP-ALICE",
                        name="Alice",
                        email=f"alice{chr(64)}{address_domain}",
                        resource_class="PROGRAMMEUR",
                        competencies="SCADA; MES",
                        active=True,
                        sort_order=10,
                    ),
                    Resource(
                        id="R-BOB",
                        external_id="EMP-BOB",
                        name="Bob",
                        email=f"bob{chr(64)}{address_domain}",
                        resource_class="PROGRAMMEUR",
                        competencies="PLC; SCADA",
                        active=True,
                        sort_order=20,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ResourceCompetency(resource_id="R-ALICE", competency_id="C-SCADA"),
                    ResourceCompetency(resource_id="R-ALICE", competency_id="C-MES"),
                    ResourceCompetency(resource_id="R-BOB", competency_id="C-PLC"),
                    ResourceCompetency(resource_id="R-BOB", competency_id="C-SCADA"),
                ]
            )
            for resource_id, suffix in (("R-ALICE", "ALICE"), ("R-BOB", "BOB")):
                session.add(
                    ResourceAvailabilityRule(
                        id=f"E2E-STD-{suffix}",
                        resource_id=resource_id,
                        availability_type="Horaire standard",
                        weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                        start_time=time(8, 0),
                        end_time=time(16, 0),
                        active=True,
                    )
                )

            users = SqlUserIdentityRepository(session)
            project_manager_contact_id = None
            approval_user_ids: list[str] = []
            manager_user_id = None
            for subject, display_name, roles, employee_external_id, email_local in (
                ("admin", "Administrateur Démo", (ROLE_ADMIN,), None, "admin"),
                ("coordinator", "Coordonnateur Démo", (ROLE_COORDINATOR,), None, "coord"),
                ("project-manager", "Chargé de projet Démo", (ROLE_PROJECT_MANAGER,), "EMP-PM", "pm"),
                ("manager", "Gestionnaire Démo", (ROLE_MANAGER,), None, "manager"),
                ("team-lead", "Team Lead Démo", (ROLE_DELIVERY_CONTRIBUTOR,), None, "lead"),
                ("delivery-technician", "Technicien Delivery Démo", (ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR), None, "delivery"),
                ("technician-a", "Technicien Démo A", (ROLE_TECHNICIAN,), "EMP-ALICE", "alice"),
                ("technician-b", "Technicien Démo B", (ROLE_TECHNICIAN,), "EMP-BOB", "bob"),
            ):
                record = users.upsert(
                    issuer="urn:resourceplanner:e2e-dev",
                    subject=subject,
                    display_name=display_name,
                    email=f"{email_local}{chr(64)}{address_domain}",
                    employee_external_id=employee_external_id,
                    roles=roles,
                    active=True,
                )
                if set(roles).intersection({ROLE_ADMIN, ROLE_COORDINATOR, ROLE_MANAGER}):
                    approval_user_ids.append(record.user_id)
                if ROLE_MANAGER in roles:
                    manager_user_id = record.user_id
                if employee_external_id == "EMP-PM":
                    project_manager_contact_id = record.business_contact_id
                    users.set_business_phone(
                        record.user_id,
                        "-".join(("450", "555", "0199")),
                    )

            session.add(
                ApprovalScope(
                    id="SCOPE-P251-AUTOMATION",
                    code="AUTOMATION",
                    label="Automatisation",
                    active=True,
                    version=1,
                )
            )
            session.flush()
            for user_id in approval_user_ids:
                session.add(
                    ApprovalScopeApprover(
                        approval_scope_id="SCOPE-P251-AUTOMATION",
                        app_user_id=user_id,
                    )
                )
            session.add(
                TaskApprovalScopeMapping(
                    task_catalog_item_id="TASK-P251-210",
                    approval_scope_id="SCOPE-P251-AUTOMATION",
                )
            )

            assert manager_user_id is not None
            session.add(
                ApprovalScope(
                    id="SCOPE-P251-ELECTRICAL",
                    code="ELECTRICAL",
                    label="Installation électrique",
                    active=True,
                    version=1,
                )
            )
            session.flush()
            session.add(
                ApprovalScopeApprover(
                    approval_scope_id="SCOPE-P251-ELECTRICAL",
                    app_user_id=manager_user_id,
                )
            )
            session.add(
                TaskApprovalScopeMapping(
                    task_catalog_item_id="TASK-P251-110",
                    approval_scope_id="SCOPE-P251-ELECTRICAL",
                )
            )

            project = session.get(Project, "P-251-ID")
            assert project is not None
            assert project_manager_contact_id is not None
            project.project_manager_contact_id = project_manager_contact_id
    finally:
        engine.dispose()


def build_app(database_path: Path, frontend_dist: Path):
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    _seed(database_url)
    transport = FakePlaywrightCommunicationTransport()
    smtp_client = FakePlaywrightSmtpClient()
    smtp_cipher = FernetSecretCipher(Fernet.generate_key().decode("ascii"))
    dev_runtime = DevUserSwitcherRuntime(bootstrap_principal=_bootstrap_principal())
    dev_resolver = dev_user_switcher_auth_resolver(dev_runtime)
    app = create_api_app(
        database_url,
        auth_resolver=_combined_auth_resolver(dev_resolver),
        dev_user_switcher_runtime=dev_runtime,
        communication_transport=transport,
        smtp_cipher=smtp_cipher,
        smtp_client=smtp_client,
    )
    attach_frontend(app, frontend_dist, required=True)
    app.state.e2e_transport = transport
    app.state.e2e_smtp_client = smtp_client
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local RessourcePlanner Playwright fixture")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / ".e2e" / "resourceplanner-playwright.db",
    )
    parser.add_argument(
        "--frontend-dist",
        type=Path,
        default=ROOT / "frontend" / "dist",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = build_app(args.database.resolve(), args.frontend_dist.resolve())
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
