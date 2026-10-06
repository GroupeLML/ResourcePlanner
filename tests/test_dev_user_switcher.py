from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.security import (
    PERMISSION_READ,
    PERMISSIONS,
    ROLE_ADMIN,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    AuthPrincipal,
)
from app.infrastructure.sql import (
    Base,
    BusinessContact,
    Project,
    Resource,
    ResourceRequirement,
    Shift,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.dev_user_switcher import (
    DevUserSwitcherRuntime,
    dev_user_switcher_auth_resolver,
    local_dev_auth_resolver,
)
from app.server.security import static_auth_resolver
from tests.sqlite_test_template import SqliteDatabaseTemplate


DAY = date(2026, 9, 18)


class DevUserSwitcherTests(unittest.TestCase):
    @classmethod
    def _seed_database(cls, session) -> None:
        users = SqlUserIdentityRepository(session)
        admin = users.upsert(
            issuer="urn:test:dev",
            subject="admin",
            display_name="Administrateur Dev",
            email=None,
            roles=(ROLE_ADMIN,),
        )
        project_manager = users.upsert(
            issuer="urn:test:dev",
            subject="project-manager",
            display_name="Chargé de projet",
            email=None,
            employee_external_id="EMP-PM",
            roles=(ROLE_PROJECT_MANAGER,),
        )
        tech_a = users.upsert(
            issuer="urn:test:dev",
            subject="tech-a",
            display_name="Technicien A",
            email=None,
            employee_external_id="EMP-A",
            roles=(ROLE_TECHNICIAN,),
        )
        tech_b = users.upsert(
            issuer="urn:test:dev",
            subject="tech-b",
            display_name="Technicien B",
            email=None,
            employee_external_id="EMP-B",
            roles=(ROLE_TECHNICIAN,),
        )
        inactive = users.upsert(
            issuer="urn:test:dev",
            subject="inactive",
            display_name="Utilisateur inactif",
            email=None,
            roles=(ROLE_TECHNICIAN,),
            active=False,
        )
        cls.user_ids = {
            "admin": admin.user_id,
            "project_manager": project_manager.user_id,
            "tech_a": tech_a.user_id,
            "tech_b": tech_b.user_id,
            "inactive": inactive.user_id,
        }

        session.add_all(
            [
                BusinessContact(
                    id="C-CO",
                    display_name="Co-chargé Dev",
                    active=True,
                ),
                BusinessContact(
                    id="C-RESP",
                    display_name="Responsable Dev",
                    active=True,
                ),
                Project(
                    id="P-DEV",
                    number="P-DEV",
                    name="Projet identités dev",
                    project_manager_external_id="EMP-PM",
                    status="Actif",
                ),
            ]
        )
        session.add_all(
            [
                Resource(
                    id="R-A",
                    external_id="EMP-A",
                    name="Ressource A",
                    active=True,
                    sort_order=10,
                ),
                Resource(
                    id="R-B",
                    external_id="EMP-B",
                    name="Ressource B",
                    active=True,
                    sort_order=20,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ResourceRequirement(
                    id="REQ-A",
                    legacy_segment_id="DEV-SEG-A",
                    project_id="P-DEV",
                    assigned_resource_id="R-A",
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("8"),
                    status="Planifié",
                    origin="AD_HOC",
                ),
                ResourceRequirement(
                    id="REQ-B",
                    legacy_segment_id="DEV-SEG-B",
                    project_id="P-DEV",
                    assigned_resource_id="R-B",
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=Decimal("4"),
                    status="Planifié",
                    origin="AD_HOC",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Shift(
                    id="SHIFT-A",
                    legacy_allocation_id="DEV-ALLOC-A",
                    resource_requirement_id="REQ-A",
                    resource_id="R-A",
                    work_date=DAY,
                    hours=Decimal("8"),
                    source="MANUAL",
                    locked=True,
                ),
                Shift(
                    id="SHIFT-B",
                    legacy_allocation_id="DEV-ALLOC-B",
                    resource_requirement_id="REQ-B",
                    resource_id="R-B",
                    work_date=DAY,
                    hours=Decimal("4"),
                    source="MANUAL",
                    locked=True,
                ),
            ]
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="switcher.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_url = self._database_template.copy_to(self.temp.name)

        self.bootstrap = AuthPrincipal.from_roles(
            local_user_id=None,
            issuer="urn:resourceplanner:local",
            subject="bootstrap",
            display_name="Administrateur bootstrap",
            email=None,
            roles=(ROLE_ADMIN,),
            auth_mode="local",
        )
        self.runtime = DevUserSwitcherRuntime(bootstrap_principal=self.bootstrap)
        self.app = create_api_app(
            self.database_url,
            auth_resolver=dev_user_switcher_auth_resolver(self.runtime),
            dev_user_switcher_runtime=self.runtime,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bootstrap_lists_only_active_app_users(self) -> None:
        with TestClient(self.app) as client:
            current = client.get("/api/v1/auth/me")
            switcher = client.get("/api/v1/dev/user-switcher")

        self.assertEqual(current.status_code, 200)
        self.assertIsNotNone(current.json()["local_user_id"])
        self.assertEqual(current.json()["roles"], [ROLE_ADMIN])
        self.assertEqual(switcher.status_code, 200)
        payload = switcher.json()
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["bootstrap"]["display_name"], "Administrateur bootstrap")
        user_ids = {row["user_id"] for row in payload["users"]}
        self.assertIn(self.user_ids["admin"], user_ids)
        self.assertIn(self.user_ids["tech_a"], user_ids)
        self.assertNotIn(self.user_ids["inactive"], user_ids)

    def test_static_local_actor_covers_project_and_responsibility_mutations(self) -> None:
        runtime = DevUserSwitcherRuntime(bootstrap_principal=self.bootstrap)
        app = create_api_app(
            self.database_url,
            auth_resolver=local_dev_auth_resolver(runtime),
        )
        with TestClient(app) as client:
            current = client.get("/api/v1/auth/me")
            actor_user_id = current.json()["local_user_id"]

            co_manager = client.put(
                "/api/v1/projects/P-DEV/co-managers/C-CO",
                headers={"Idempotency-Key": "616a-co-manager"},
                json={"expected_version": 1},
            )
            project_responsible = client.patch(
                "/api/v1/projects/P-DEV/operational-responsible",
                headers={"Idempotency-Key": "616a-project-responsible"},
                json={"contact_id": "C-RESP", "expected_version": 1},
            )
            segment_responsible = client.patch(
                "/api/v1/segments/DEV-SEG-A/operational-responsible",
                headers={"Idempotency-Key": "616a-segment-responsible"},
                json={
                    "contact_id": "C-RESP",
                    "expected_planning_version": 1,
                },
            )
            shift_responsible = client.patch(
                "/api/v1/allocations/DEV-ALLOC-A/operational-responsible",
                headers={"Idempotency-Key": "616a-shift-responsible"},
                json={
                    "contact_id": "C-RESP",
                    "expected_planning_version": 2,
                },
            )

        self.assertIsNotNone(actor_user_id)
        self.assertEqual(co_manager.status_code, 200, co_manager.text)
        self.assertEqual(project_responsible.status_code, 200, project_responsible.text)
        self.assertEqual(segment_responsible.status_code, 200, segment_responsible.text)
        self.assertEqual(shift_responsible.status_code, 200, shift_responsible.text)

        engine = create_sql_engine(self.database_url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                record = SqlUserIdentityRepository(session).get_by_external_identity(
                    "urn:resourceplanner:local",
                    "bootstrap",
                )
                assert record is not None
                self.assertEqual(record.user_id, actor_user_id)
                self.assertTrue(record.active)
        finally:
            engine.dispose()

    def test_switching_changes_canonical_principal_and_rbac_without_restart(self) -> None:
        with TestClient(self.app) as client:
            selected = client.post(
                "/api/v1/dev/user-switcher/select",
                json={"user_id": self.user_ids["tech_a"]},
            )
            technician = client.get("/api/v1/auth/me")
            forbidden = client.post("/api/v1/integrations/acumatica/projects/sync")

            self.assertEqual(selected.status_code, 200)
            self.assertEqual(technician.json()["roles"], [ROLE_TECHNICIAN])
            self.assertEqual(technician.json()["permissions"], [PERMISSION_READ])
            self.assertEqual(technician.json()["auth_mode"], "local")
            self.assertEqual(forbidden.status_code, 403)

            selected_admin = client.post(
                "/api/v1/dev/user-switcher/select",
                json={"user_id": self.user_ids["admin"]},
            )
            admin = client.get("/api/v1/auth/me")
            integration = client.post("/api/v1/integrations/acumatica/projects/sync")

        self.assertEqual(selected_admin.status_code, 200)
        self.assertEqual(admin.json()["roles"], [ROLE_ADMIN])
        self.assertEqual(tuple(admin.json()["permissions"]), PERMISSIONS)
        self.assertEqual(integration.status_code, 503)
        self.assertEqual(integration.json()["error"]["code"], "acumatica_not_configured")

    def test_project_manager_switcher_resolves_managed_context_without_resource(self) -> None:
        with TestClient(self.app) as client:
            client.post(
                "/api/v1/dev/user-switcher/select",
                json={"user_id": self.user_ids["project_manager"]},
            )
            context = client.get("/api/v1/me/context")

        self.assertEqual(context.status_code, 200)
        payload = context.json()
        self.assertEqual(payload["resource"]["link_status"], "RESOURCE_NOT_FOUND")
        self.assertEqual(payload["relations"]["managed_project_count"], 1)
        self.assertEqual(payload["relations"]["participating_project_count"], 0)
        self.assertEqual(payload["relations"]["personal_project_count"], 1)
        self.assertEqual(payload["view_policy"]["default_scope"], "mine")

    def test_inactive_user_cannot_be_selected(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                "/api/v1/dev/user-switcher/select",
                json={"user_id": self.user_ids["inactive"]},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "dev_user_inactive")

    def test_two_technicians_resolve_distinct_linked_schedules(self) -> None:
        with TestClient(self.app) as client:
            client.post(
                "/api/v1/dev/user-switcher/select",
                json={"user_id": self.user_ids["tech_a"]},
            )
            schedule_a = client.get(
                "/api/v1/me/schedule",
                params={"start": DAY.isoformat(), "end": DAY.isoformat()},
            )

            client.post(
                "/api/v1/dev/user-switcher/select",
                json={"user_id": self.user_ids["tech_b"]},
            )
            schedule_b = client.get(
                "/api/v1/me/schedule",
                params={"start": DAY.isoformat(), "end": DAY.isoformat()},
            )

        self.assertEqual(schedule_a.status_code, 200)
        self.assertEqual(schedule_b.status_code, 200)
        self.assertEqual(schedule_a.json()["link_status"], "LINKED")
        self.assertEqual(schedule_b.json()["link_status"], "LINKED")
        self.assertEqual(schedule_a.json()["resource"]["name"], "Ressource A")
        self.assertEqual(schedule_b.json()["resource"]["name"], "Ressource B")
        self.assertEqual(schedule_a.json()["shifts"][0]["hours"], 8.0)
        self.assertEqual(schedule_b.json()["shifts"][0]["hours"], 4.0)

    def test_reset_returns_to_bootstrap_admin(self) -> None:
        with TestClient(self.app) as client:
            client.post(
                "/api/v1/dev/user-switcher/select",
                json={"user_id": self.user_ids["tech_a"]},
            )
            reset = client.post("/api/v1/dev/user-switcher/reset")
            current = client.get("/api/v1/auth/me")

        self.assertEqual(reset.status_code, 200)
        self.assertIsNotNone(current.json()["local_user_id"])
        self.assertEqual(current.json()["display_name"], "Administrateur bootstrap")
        self.assertEqual(current.json()["roles"], [ROLE_ADMIN])

    def test_dev_routes_do_not_exist_when_switcher_is_not_composed(self) -> None:
        app = create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(self.bootstrap),
        )
        with TestClient(app) as client:
            response = client.get("/api/v1/dev/user-switcher")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
