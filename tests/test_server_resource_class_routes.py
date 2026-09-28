from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.application.security import (
    AuthPrincipal,
    ROLE_ADMIN,
    ROLE_MANAGER,
    ROLE_TECHNICIAN,
)
from app.infrastructure.sql import Base, Project
from app.infrastructure.sql.resource_class_models import ResourceClassConfig
from app.server import create_api_app
from app.server.security import static_auth_resolver


def principal(role: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id=f"{role.lower()}-1",
        issuer="urn:test",
        subject=f"subject-{role.lower()}",
        display_name=role,
        email=None,
        roles=(role,),
        auth_mode="test",
    )


class ServerResourceClassRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self._temp.name) / "resource-classes.db"
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _app(self, role: str = ROLE_ADMIN):
        app = create_api_app(
            "sqlite+pysqlite:///" + self.database_path.as_posix(),
            auth_resolver=static_auth_resolver(principal(role)),
        )
        factory = app.state.session_factory
        Base.metadata.create_all(factory.kw["bind"])
        with factory() as session, session.begin():
            session.add(
                Project(
                    id="project-1",
                    number="P-1",
                    name="Projet 1",
                )
            )
        return app

    def test_admin_can_configure_standard_override_and_projection(self) -> None:
        app = self._app()
        with TestClient(app) as client:
            programmer = client.post(
                "/api/v1/admin/resource-classes",
                json={
                    "code": "PROGRAMMEUR",
                    "label": "Programmeur",
                    "average_hourly_cost_cad": "125.00",
                    "active": True,
                },
            )
            automation_installer = client.post(
                "/api/v1/admin/resource-classes",
                json={
                    "code": "INSTALLATEUR_AUTOMATISATION",
                    "label": "Installateur automatisation",
                    "average_hourly_cost_cad": "100.00",
                    "active": True,
                },
            )
            standard = client.post(
                "/api/v1/admin/resource-classes/task-standards",
                json={
                    "task_code": "216",
                    "resource_class_code": "PROGRAMMEUR",
                    "active": True,
                },
            )
            baseline = client.get(
                "/api/v1/admin/resource-classes/"
                "projects/project-1/tasks/216/resolution"
            )
            projection = client.post(
                "/api/v1/admin/resource-classes/"
                "projects/project-1/tasks/216/budget-projection",
                json={"budget_amount_cad": "1000.00"},
            )
            override = client.put(
                "/api/v1/admin/resource-classes/"
                "projects/project-1/task-overrides/216",
                json={
                    "resource_class_code": (
                        "INSTALLATEUR_AUTOMATISATION"
                    ),
                    "excluded": False,
                },
            )
            rules = client.get(
                "/api/v1/admin/resource-classes/"
                "projects/project-1/task-rules"
            )
            restored = client.request(
                "DELETE",
                "/api/v1/admin/resource-classes/"
                "projects/project-1/task-overrides/216",
                json={
                    "expected_version": override.json()["version"],
                },
            )

        self.assertEqual(programmer.status_code, 201)
        self.assertEqual(
            programmer.json()["average_hourly_cost_cad"],
            "125.0000",
        )
        self.assertEqual(automation_installer.status_code, 201)
        self.assertEqual(standard.status_code, 201)
        self.assertEqual(
            baseline.json()["resource_class_code"],
            "PROGRAMMEUR",
        )
        self.assertEqual(
            projection.json()["budget_hours"],
            "8",
        )
        self.assertEqual(
            override.json()["resource_class_code"],
            "INSTALLATEUR_AUTOMATISATION",
        )
        self.assertEqual(
            rules.json()[0]["resolution"]["resource_class_code"],
            "INSTALLATEUR_AUTOMATISATION",
        )
        self.assertEqual(
            restored.json()["resource_class_code"],
            "PROGRAMMEUR",
        )

    def test_active_class_requires_positive_cost(self) -> None:
        app = self._app()
        with TestClient(app) as client:
            missing = client.post(
                "/api/v1/admin/resource-classes",
                json={
                    "code": "NO_COST",
                    "label": "Sans coût",
                    "active": True,
                },
            )
            zero = client.post(
                "/api/v1/admin/resource-classes",
                json={
                    "code": "ZERO",
                    "label": "Zéro",
                    "average_hourly_cost_cad": "0",
                    "active": True,
                },
            )
            staged = client.post(
                "/api/v1/admin/resource-classes",
                json={
                    "code": "STAGED",
                    "label": "À compléter",
                    "average_hourly_cost_cad": None,
                    "active": False,
                },
            )

        self.assertEqual(missing.status_code, 422)
        self.assertEqual(
            missing.json()["error"]["code"],
            "resource_class_active_cost_required",
        )
        self.assertEqual(zero.status_code, 422)
        self.assertEqual(staged.status_code, 201)

    def test_read_only_role_can_read_operational_catalog_without_admin_fields(self) -> None:
        app = self._app(role=ROLE_TECHNICIAN)
        factory = app.state.session_factory
        with factory() as session, session.begin():
            session.add_all(
                [
                    ResourceClassConfig(
                        code="PROGRAMMEUR",
                        label="Programmeur",
                        average_hourly_cost_cad="125.00",
                        active=True,
                        version=3,
                    ),
                    ResourceClassConfig(
                        code="HISTORIQUE",
                        label="Classe historique",
                        average_hourly_cost_cad="90.00",
                        active=False,
                        version=2,
                    ),
                ]
            )

        with TestClient(app) as client:
            response = client.get("/api/v1/resource-classes")
            admin_read = client.get("/api/v1/admin/resource-classes")
            admin_mutation = client.post(
                "/api/v1/admin/resource-classes",
                json={
                    "code": "INTERDIT",
                    "label": "Interdit",
                    "average_hourly_cost_cad": "100.00",
                    "active": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "code": "HISTORIQUE",
                    "label": "Classe historique",
                    "active": False,
                },
                {
                    "code": "PROGRAMMEUR",
                    "label": "Programmeur",
                    "active": True,
                },
            ],
        )
        self.assertNotIn(
            "average_hourly_cost_cad",
            response.json()[0],
        )
        self.assertNotIn("version", response.json()[0])
        self.assertEqual(admin_read.status_code, 403)
        self.assertEqual(admin_mutation.status_code, 403)
        for denied in (admin_read, admin_mutation):
            self.assertEqual(
                denied.json()["error"]["context"][
                    "required_permission"
                ],
                "admin_settings",
            )

    def test_non_admin_is_denied_admin_surface(self) -> None:
        app = self._app(role=ROLE_MANAGER)
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/admin/resource-classes"
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["error"]["context"][
                "required_permission"
            ],
            "admin_settings",
        )


if __name__ == "__main__":
    unittest.main()
