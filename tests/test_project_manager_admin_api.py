from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application.project_co_managers import (
    PROJECT_CO_MANAGER_ADDED,
    PROJECT_CO_MANAGER_REMOVED,
)
from app.application.security import (
    ROLE_ADMIN,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    AuthPrincipal,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    Project,
    ProjectCoManager,
    ProjectManagerAudit,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.approval_test_support import TEST_ADMIN_USER_ID
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


def _principal(
    *,
    local_user_id: str | None,
    roles: tuple[str, ...],
    employee_external_id: str | None = None,
) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=local_user_id,
        issuer="urn:resourceplanner:test",
        subject=f"573d-{local_user_id or 'missing'}",
        display_name="Utilisateur 573D",
        email=None,
        employee_external_id=employee_external_id,
        roles=roles,
        auth_mode="test",
    )


class ProjectManagerAdminApiTests(unittest.TestCase):
    def _database(self, directory: str) -> str:
        path = Path(directory) / "project-manager-admin.db"
        url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    BusinessContact(
                        id="C-PRIMARY",
                        display_name="Principal local",
                        active=True,
                        source="LOCAL",
                    ),
                    BusinessContact(
                        id="C-CO",
                        display_name="Co chargé lié",
                        active=True,
                        source="LOCAL",
                    ),
                    BusinessContact(
                        id="C-NOUSER",
                        display_name="Co chargé sans utilisateur",
                        active=True,
                        source="LOCAL",
                    ),
                    BusinessContact(
                        id="C-INACTIVE",
                        display_name="Ancien co chargé",
                        active=False,
                        source="LOCAL",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    AppUser(
                        id=TEST_ADMIN_USER_ID,
                        issuer="urn:resourceplanner:test",
                        subject="explicit-test-admin",
                        display_name="Administrateur de test explicite",
                        email=None,
                        roles_json=json.dumps([ROLE_ADMIN]),
                        active=True,
                    ),
                    AppUser(
                        id="U-PRIMARY",
                        issuer=None,
                        subject=None,
                        display_name="Principal local",
                        email=None,
                        employee_external_id="EMP-PRIMARY",
                        business_contact_id="C-PRIMARY",
                        roles_json=json.dumps([ROLE_PROJECT_MANAGER]),
                        active=True,
                    ),
                    AppUser(
                        id="U-CO",
                        issuer=None,
                        subject=None,
                        display_name="Co chargé lié",
                        email=None,
                        employee_external_id=None,
                        business_contact_id="C-CO",
                        roles_json=json.dumps([ROLE_TECHNICIAN]),
                        active=True,
                    ),
                    AppUser(
                        id="U-NOCONTACT",
                        issuer=None,
                        subject=None,
                        display_name="Principal sans contact",
                        email=None,
                        employee_external_id="EMP-NOCONTACT",
                        business_contact_id=None,
                        roles_json=json.dumps([ROLE_PROJECT_MANAGER]),
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    Project(
                        id="P-1",
                        number="P-1",
                        name="Projet principal",
                        project_manager_external_id="EMP-PRIMARY",
                        project_manager_name="Principal ERP",
                        project_manager_contact_id="C-INACTIVE",
                        co_managers_version=1,
                        status="Actif",
                    ),
                    Project(
                        id="P-2",
                        number="P-2",
                        name="Projet principal non lié",
                        project_manager_external_id="EMP-UNLINKED",
                        project_manager_name="Principal ERP non lié",
                        co_managers_version=1,
                        status="Actif",
                    ),
                    Project(
                        id="P-3",
                        number="P-3",
                        name="Projet sans contact",
                        project_manager_external_id="EMP-NOCONTACT",
                        project_manager_name="Principal ERP sans contact",
                        co_managers_version=1,
                        status="Actif",
                    ),
                ]
            )
        engine.dispose()
        return url

    def _count(self, database_url: str, model: type[object]) -> int:
        engine = create_sql_engine(database_url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                return int(session.scalar(select(func.count()).select_from(model)) or 0)
        finally:
            engine.dispose()

    def test_read_uses_canonical_resolution_and_exact_collection_version(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                project = session.get(Project, "P-1")
                assert project is not None
                project.co_managers_version = 7
                session.add_all(
                    [
                        ProjectCoManager(
                            project_id="P-1",
                            business_contact_id="C-NOUSER",
                            created_by_user_id=TEST_ADMIN_USER_ID,
                        ),
                        ProjectCoManager(
                            project_id="P-1",
                            business_contact_id="C-INACTIVE",
                            created_by_user_id=TEST_ADMIN_USER_ID,
                        ),
                    ]
                )
            engine.dispose()

            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                response = client.get("/api/v1/projects/P-1/managers")

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["project_id"], "P-1")
            self.assertEqual(payload["project_number"], "P-1")
            self.assertEqual(payload["co_managers_version"], 7)
            self.assertEqual(payload["primary"]["sources"], ["ERP"])
            self.assertEqual(payload["primary"]["employee_external_id"], "EMP-PRIMARY")
            self.assertEqual(payload["primary"]["business_contact_id"], "C-PRIMARY")
            self.assertEqual(payload["primary"]["display_name"], "Principal local")
            self.assertEqual(payload["primary"]["resolution_status"], "RESOLVED")

            co_managers = {
                row["business_contact_id"]: row for row in payload["co_managers"]
            }
            self.assertIn("C-NOUSER", co_managers)
            self.assertIn(
                "PROJECT_CO_MANAGER_APP_USER_NOT_LINKED",
                co_managers["C-NOUSER"]["diagnostics"],
            )
            self.assertIn("C-INACTIVE", co_managers)
            self.assertIn(
                "PROJECT_CO_MANAGER_CONTACT_INACTIVE",
                co_managers["C-INACTIVE"]["diagnostics"],
            )

    def test_read_preserves_unresolved_erp_principal_states(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                no_user = client.get("/api/v1/projects/P-2/managers")
                no_contact = client.get("/api/v1/projects/P-3/managers")

            self.assertEqual(no_user.status_code, 200, no_user.text)
            self.assertEqual(
                no_user.json()["primary"]["resolution_status"],
                "UNRESOLVED_USER",
            )
            self.assertEqual(
                no_user.json()["primary"]["display_name"],
                "Principal ERP non lié",
            )
            self.assertIn(
                "ERP_PROJECT_MANAGER_APP_USER_NOT_LINKED",
                no_user.json()["primary"]["diagnostics"],
            )

            self.assertEqual(no_contact.status_code, 200, no_contact.text)
            self.assertEqual(
                no_contact.json()["primary"]["resolution_status"],
                "UNRESOLVED_CONTACT",
            )
            self.assertIn(
                "ERP_PROJECT_MANAGER_BUSINESS_CONTACT_NOT_LINKED",
                no_contact.json()["primary"]["diagnostics"],
            )

    def test_read_deduplicates_historical_double_provenance(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                session.add(
                    ProjectCoManager(
                        project_id="P-1",
                        business_contact_id="C-PRIMARY",
                        created_by_user_id=TEST_ADMIN_USER_ID,
                    )
                )
            engine.dispose()

            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                response = client.get("/api/v1/projects/P-1/managers")

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["primary"]["sources"], ["ERP", "RP"])
            self.assertEqual(response.json()["co_managers"], [])

    def test_read_mine_scope_uses_existing_277_managed_project_policy(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                session.add(
                    ProjectCoManager(
                        project_id="P-1",
                        business_contact_id="C-CO",
                        created_by_user_id=TEST_ADMIN_USER_ID,
                    )
                )
            engine.dispose()

            principal = _principal(
                local_user_id="U-CO",
                roles=(ROLE_PROJECT_MANAGER,),
            )
            app = create_api_app(
                database_url,
                auth_resolver=static_auth_resolver(principal),
            )
            with TestClient(app) as client:
                visible = client.get(
                    "/api/v1/projects/P-1/managers",
                    params={"scope": "mine"},
                )
                hidden = client.get(
                    "/api/v1/projects/P-2/managers",
                    params={"scope": "mine"},
                )

            self.assertEqual(visible.status_code, 200, visible.text)
            self.assertEqual(hidden.status_code, 404, hidden.text)
            self.assertEqual(hidden.json()["error"]["code"], "project_not_found")

            # ADR-030: even a project co-manager with only TECHNICIAN
            # cannot retrieve full project metadata by ID or scope.
            technician = _principal(
                local_user_id="U-CO",
                roles=(ROLE_TECHNICIAN,),
            )
            technician_app = create_api_app(
                database_url,
                auth_resolver=static_auth_resolver(technician),
            )
            with TestClient(technician_app) as client:
                for project in ("P-1", "P-2"):
                    denied = client.get(
                        f"/api/v1/projects/{project}/managers",
                        params={"scope": "mine"},
                    )
                    self.assertEqual(denied.status_code, 403, denied.text)
                    self.assertEqual(
                        denied.json()["error"]["code"], "permission_denied"
                    )

    def test_add_is_cas_audited_idempotent_and_uses_authenticated_actor(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                first = client.put(
                    "/api/v1/projects/P-1/co-managers/C-NOUSER",
                    headers={"Idempotency-Key": "573d-add-no-user"},
                    json={"expected_version": 1},
                )
                replay = client.put(
                    "/api/v1/projects/P-1/co-managers/C-NOUSER",
                    headers={"Idempotency-Key": "573d-add-no-user"},
                    json={"expected_version": 1},
                )
                projection = client.get("/api/v1/projects/P-1/managers")

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["version"], 2)
            self.assertEqual(first.json()["action"], PROJECT_CO_MANAGER_ADDED)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), first.json())
            self.assertEqual(projection.json()["co_managers_version"], 2)
            self.assertEqual(
                projection.json()["co_managers"][0]["business_contact_id"],
                "C-NOUSER",
            )
            self.assertIn(
                "PROJECT_CO_MANAGER_APP_USER_NOT_LINKED",
                projection.json()["co_managers"][0]["diagnostics"],
            )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    project = session.get(Project, "P-1")
                    assignment = session.get(ProjectCoManager, ("P-1", "C-NOUSER"))
                    audits = session.scalars(
                        select(ProjectManagerAudit).where(
                            ProjectManagerAudit.project_id == "P-1"
                        )
                    ).all()
                    assert project is not None and assignment is not None
                    self.assertEqual(project.co_managers_version, 2)
                    self.assertEqual(
                        assignment.created_by_user_id,
                        TEST_ADMIN_USER_ID,
                    )
                    self.assertEqual(len(audits), 1)
                    self.assertEqual(audits[0].actor_user_id, TEST_ADMIN_USER_ID)
                    self.assertEqual(audits[0].action, PROJECT_CO_MANAGER_ADDED)
            finally:
                engine.dispose()

    def test_stale_cas_does_not_mutate_or_audit_second_intention(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                first = client.put(
                    "/api/v1/projects/P-1/co-managers/C-NOUSER",
                    headers={"Idempotency-Key": "573d-cas-a"},
                    json={"expected_version": 1},
                )
                stale = client.put(
                    "/api/v1/projects/P-1/co-managers/C-CO",
                    headers={"Idempotency-Key": "573d-cas-b"},
                    json={"expected_version": 1},
                )

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(
                stale.json()["error"]["code"],
                "project_co_managers_version_conflict",
            )
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    project = session.get(Project, "P-1")
                    assert project is not None
                    self.assertEqual(project.co_managers_version, 2)
                    self.assertIsNone(
                        session.get(ProjectCoManager, ("P-1", "C-CO"))
                    )
                    audits = session.scalars(
                        select(ProjectManagerAudit).where(
                            ProjectManagerAudit.project_id == "P-1"
                        )
                    ).all()
                    self.assertEqual(len(audits), 1)
            finally:
                engine.dispose()

    def test_same_idempotency_key_with_different_payload_is_conflict(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                first = client.put(
                    "/api/v1/projects/P-1/co-managers/C-NOUSER",
                    headers={"Idempotency-Key": "573d-same-key"},
                    json={"expected_version": 1},
                )
                conflict = client.put(
                    "/api/v1/projects/P-1/co-managers/C-CO",
                    headers={"Idempotency-Key": "573d-same-key"},
                    json={"expected_version": 1},
                )

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(
                conflict.json()["error"]["code"],
                "idempotency_key_conflict",
            )
            self.assertEqual(self._count(database_url, ProjectManagerAudit), 1)

    def test_new_inactive_contact_and_primary_are_refused_without_version_advance(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                inactive = client.put(
                    "/api/v1/projects/P-1/co-managers/C-INACTIVE",
                    headers={"Idempotency-Key": "573d-inactive"},
                    json={"expected_version": 1},
                )
                primary = client.put(
                    "/api/v1/projects/P-1/co-managers/C-PRIMARY",
                    headers={"Idempotency-Key": "573d-primary"},
                    json={"expected_version": 1},
                )

            self.assertEqual(inactive.status_code, 422, inactive.text)
            self.assertEqual(
                inactive.json()["error"]["code"],
                "project_co_manager_contact_inactive",
            )
            self.assertEqual(primary.status_code, 422, primary.text)
            self.assertEqual(
                primary.json()["error"]["code"],
                "project_co_manager_primary_forbidden",
            )
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    project = session.get(Project, "P-1")
                    assert project is not None
                    self.assertEqual(project.co_managers_version, 1)
                    self.assertEqual(
                        int(
                            session.scalar(
                                select(func.count()).select_from(ProjectCoManager)
                            )
                            or 0
                        ),
                        0,
                    )
            finally:
                engine.dispose()
            self.assertEqual(self._count(database_url, ProjectManagerAudit), 0)

    def test_remove_existing_inactive_contact_is_allowed(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                session.add(
                    ProjectCoManager(
                        project_id="P-1",
                        business_contact_id="C-INACTIVE",
                        created_by_user_id=TEST_ADMIN_USER_ID,
                    )
                )
            engine.dispose()

            app = create_api_app(database_url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                removed = client.request(
                    "DELETE",
                    "/api/v1/projects/P-1/co-managers/C-INACTIVE",
                    headers={"Idempotency-Key": "573d-remove-inactive"},
                    json={"expected_version": 1},
                )

            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertEqual(removed.json()["version"], 2)
            self.assertEqual(removed.json()["action"], PROJECT_CO_MANAGER_REMOVED)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    self.assertIsNone(
                        session.get(ProjectCoManager, ("P-1", "C-INACTIVE"))
                    )
                    audit = session.scalar(
                        select(ProjectManagerAudit).where(
                            ProjectManagerAudit.project_id == "P-1"
                        )
                    )
                    assert audit is not None
                    self.assertEqual(audit.action, PROJECT_CO_MANAGER_REMOVED)
            finally:
                engine.dispose()

    def test_mutation_requires_manage_resources_even_for_project_manager(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            principal = _principal(
                local_user_id="U-PRIMARY",
                roles=(ROLE_PROJECT_MANAGER,),
                employee_external_id="EMP-PRIMARY",
            )
            app = create_api_app(
                database_url,
                auth_resolver=static_auth_resolver(principal),
            )
            with TestClient(app) as client:
                refused = client.put(
                    "/api/v1/projects/P-1/co-managers/C-NOUSER",
                    headers={"Idempotency-Key": "573d-rbac"},
                    json={"expected_version": 1},
                )

            self.assertEqual(refused.status_code, 403, refused.text)
            self.assertEqual(
                refused.json()["error"]["context"]["required_permission"],
                "manage_resources",
            )
            self.assertEqual(self._count(database_url, ProjectManagerAudit), 0)

    def test_mutation_fails_closed_without_local_actor_identity(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            principal = _principal(local_user_id=None, roles=(ROLE_ADMIN,))
            app = create_api_app(
                database_url,
                auth_resolver=static_auth_resolver(principal),
            )
            with TestClient(app) as client:
                refused = client.put(
                    "/api/v1/projects/P-1/co-managers/C-NOUSER",
                    headers={"Idempotency-Key": "573d-no-actor"},
                    json={"expected_version": 1},
                )

            self.assertEqual(refused.status_code, 403, refused.text)
            self.assertEqual(
                refused.json()["error"]["code"],
                "project_co_manager_actor_required",
            )
            self.assertEqual(self._count(database_url, ProjectManagerAudit), 0)


if __name__ == "__main__":
    unittest.main()
