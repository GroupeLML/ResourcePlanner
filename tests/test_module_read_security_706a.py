from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.security import (
    AuthPrincipal, ROLE_ADMIN, ROLE_DELIVERY_CONTRIBUTOR,
    ROLE_PROJECT_MANAGER, ROLE_TECHNICIAN,
)
from app.infrastructure.sql import Base, create_sql_engine
from app.server import create_api_app
from app.server.security import static_auth_resolver


def actor(*roles: str) -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id="U-706A", issuer="urn:test", subject="706a",
        display_name="Acteur 706A", email=None, roles=roles, auth_mode="test",
    )


class ModuleReadSecurity706ATests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.url = f"sqlite+pysqlite:///{(Path(self.temp.name) / 'module-reads.db').as_posix()}"
        engine = create_sql_engine(self.url)
        Base.metadata.create_all(engine)
        engine.dispose()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def client(self, *roles: str) -> TestClient:
        return TestClient(create_api_app(self.url, auth_resolver=static_auth_resolver(actor(*roles))))

    def test_technician_rejected_before_module_data_lookup(self) -> None:
        urls = (
            "/api/v1/projects", "/api/v1/projects/UNKNOWN/managers",
            "/api/v1/task-catalog", "/api/v1/business-contacts",
            "/api/v1/work-packages", "/api/v1/work-packages/UNKNOWN",
            "/api/v1/demands", "/api/v1/demands/UNKNOWN/detail",
            "/api/v1/demands/UNKNOWN/history",
            "/api/v1/demands/UNKNOWN/periods",
            "/api/v1/demands/UNKNOWN/approval-state",
            "/api/v1/demands/UNKNOWN/plan-delta",
            "/api/v1/demands/UNKNOWN/workflow-actions",
            "/api/v1/medium-term/budget",
            "/api/v1/medium-term/unlinked-segments",
            "/api/v1/coordinator-dashboard",
            "/api/v1/delivery/plans/UNKNOWN",
            "/api/v1/delivery/work-packages/UNKNOWN/summary",
            "/api/v1/verification/work-packages/UNKNOWN/documents/traceability.csv",
            "/api/v1/assets/requirements",
            "/api/v1/assets/requirements/UNKNOWN/operator-candidates",
            "/api/v1/segments/UNKNOWN/history",
            "/api/v1/shifts/UNKNOWN/history",
            "/api/v1/allocations/UNKNOWN/operational-responsibility",
        )
        with self.client(ROLE_TECHNICIAN) as client:
            for url in urls:
                with self.subTest(url=url):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["error"]["code"], "permission_denied")
                    self.assertIn(
                        response.json()["error"]["context"]["required_permission"],
                        ("read_projects", "read_work_packages", "read_demands"),
                    )

    def test_scope_and_direct_identifiers_do_not_bypass_permissions(self) -> None:
        with self.client(ROLE_TECHNICIAN) as client:
            for scope in (None, "mine", "global"):
                params = {} if scope is None else {"scope": scope}
                for path in ("/api/v1/projects", "/api/v1/demands/UNKNOWN", "/api/v1/work-packages"):
                    with self.subTest(path=path, scope=scope):
                        self.assertEqual(client.get(path, params=params).status_code, 403)

    def test_technician_cannot_modify_module_data(self) -> None:
        with self.client(ROLE_TECHNICIAN) as client:
            for method, path in (
                ("POST", "/api/v1/demands"),
                ("PATCH", "/api/v1/demands/UNKNOWN"),
                ("POST", "/api/v1/demands/UNKNOWN/approve"),
                ("POST", "/api/v1/work-packages"),
                ("PATCH", "/api/v1/work-packages/UNKNOWN"),
                ("POST", "/api/v1/work-packages/UNKNOWN/close"),
            ):
                with self.subTest(method=method, path=path):
                    response = client.request(method, path)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["error"]["code"], "permission_denied")

    def test_multirole_keeps_explicit_catalogue_access(self) -> None:
        with self.client(ROLE_TECHNICIAN, ROLE_ADMIN) as client:
            for path in ("/api/v1/projects", "/api/v1/work-packages", "/api/v1/demands"):
                self.assertEqual(client.get(path).status_code, 200)
        with self.client(ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR) as client:
            self.assertEqual(client.get("/api/v1/projects?scope=mine").status_code, 200)
            self.assertEqual(client.get("/api/v1/work-packages?scope=mine").status_code, 200)
            self.assertEqual(client.get("/api/v1/demands/UNKNOWN").status_code, 403)
        with self.client(ROLE_TECHNICIAN, ROLE_PROJECT_MANAGER) as client:
            self.assertNotEqual(client.get("/api/v1/projects?scope=mine").status_code, 403)

    def test_personal_schedule_is_not_module_guarded(self) -> None:
        with self.client(ROLE_TECHNICIAN) as client:
            self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
            response = client.get("/api/v1/me/schedule?start=2026-10-05&end=2026-10-11")
            self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
