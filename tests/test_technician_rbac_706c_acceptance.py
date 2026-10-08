"""Transversal RBAC/API acceptance for ADR-030, after 706A and 706B.

The fixture deliberately contains a real project assigned to the technician,
a real demand, a WorkPackage and private Planning data. A 403 must never
serialize their contents, even when a valid identifier or scope is supplied.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.security import (
    AuthPrincipal,
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_DELIVERY_CONTRIBUTOR,
    ROLE_MANAGER,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
)
from app.infrastructure.sql import WorkPackage, create_session_factory, create_sql_engine
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.sqlite_test_template import SqliteDatabaseTemplate
from tests.test_planning_visibility_security_acceptance import (
    DAY_1,
    DAY_2,
    PlanningVisibilitySecurityAcceptanceTests,
)


class TechnicianRbac706CAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _seed(session) -> None:
        PlanningVisibilitySecurityAcceptanceTests._seed_database(session)
        session.add(
            WorkPackage(
                id="WP-706C",
                project_id="P-T1",
                code="SEC-706C",
                name="WP-SECRET-706C",
                description="WP-PRIVATE-DESCRIPTION-706C",
                status="planned",
                legacy_effort_id="EFF-706C",
            )
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._template = SqliteDatabaseTemplate(
            filename="technician-rbac-706c.db", seed=cls._seed
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._template.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_url = self._template.copy_to(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def client(self, *roles: str, user_id: str = "U-TECH",
               employee_external_id: str | None = "EMP-TECH") -> TestClient:
        principal = AuthPrincipal.from_roles(
            local_user_id=user_id,
            issuer="urn:resourceplanner:local",
            subject=user_id.lower(),
            display_name="Actor 706C",
            email=None,
            employee_external_id=employee_external_id,
            roles=roles,
            auth_mode="test",
        )
        return TestClient(create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(principal),
        ))

    def assert_private_module_denied(self, response) -> None:
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers["content-type"], "application/json")
        data = response.json()
        self.assertEqual(set(data), {"error"})
        self.assertEqual(data["error"]["code"], "permission_denied")
        self.assertIn(
            data["error"]["context"]["required_permission"],
            {"read_demands", "read_projects", "read_work_packages"},
        )
        # Verify the COMPLETE serialized JSON, not only a filtered field list.
        for secret in (
            "WP-SECRET-706C", "WP-PRIVATE-DESCRIPTION-706C",
            "DMO-APPROVAL", "Projet technicien jour 1",
            "COLLEAGUE-PRIVATE-RESOURCE-NOTE", "NEIGHBOR-DAY1-PRIVATE",
        ):
            self.assertNotIn(secret, response.text)

    def test_direct_and_indirect_reads_reject_real_and_unknown_ids_all_scopes(self) -> None:
        paths = (
            "/api/v1/projects",
            "/api/v1/projects/P-T1/managers",
            "/api/v1/task-catalog",
            "/api/v1/business-contacts",
            "/api/v1/integrations/acumatica/projects",
            "/api/v1/demands",
            "/api/v1/demands/DMO-APPROVAL",
            "/api/v1/demands/DMO-APPROVAL/detail",
            "/api/v1/demands/DMO-APPROVAL/history",
            "/api/v1/demands/DMO-APPROVAL/periods",
            "/api/v1/demands/DMO-APPROVAL/approval-state",
            "/api/v1/demands/DMO-APPROVAL/plan-delta",
            "/api/v1/demands/DMO-APPROVAL/workflow-actions",
            "/api/v1/demands/NOT-FOUND",
            "/api/v1/work-packages",
            "/api/v1/work-packages/EFF-706C/weekly-loads",
            "/api/v1/work-packages/NOT-FOUND",
            "/api/v1/medium-term/budget",
            "/api/v1/medium-term/unlinked-segments",
            "/api/v1/coordinator-dashboard",
            "/api/v1/delivery/plans/PLAN-UNKNOWN",
            "/api/v1/delivery/work-packages/WP-706C/summary",
            "/api/v1/verification/work-packages/WP-706C/documents/traceability.csv",
            "/api/v1/assets/requirements",
            "/api/v1/assets/requirements/NOT-FOUND/operator-candidates",
            "/api/v1/segments/SEG-T-OWN1/history",
            "/api/v1/shifts/SHIFT-T-OWN1/history",
            "/api/v1/allocations/SHIFT-T-OWN1/operational-responsibility",
        )
        with self.client(ROLE_TECHNICIAN) as client:
            for path in paths:
                # An alleged Planning provenance cannot authorize a module read.
                for scope in (None, "mine", "global"):
                    params = {"source": "planning", "project_id": "P-T1"}
                    if scope is not None:
                        params["scope"] = scope
                    with self.subTest(path=path, scope=scope):
                        self.assert_private_module_denied(client.get(path, params=params))

    def test_mutations_blocked_even_with_known_ids_and_no_state_change(self) -> None:
        mutations = (
            ("POST", "/api/v1/demands"),
            ("PATCH", "/api/v1/demands/DMO-APPROVAL"),
            ("PUT", "/api/v1/demands/DMO-APPROVAL/periods"),
            ("POST", "/api/v1/demands/DMO-APPROVAL/approve"),
            ("POST", "/api/v1/demands/DMO-APPROVAL/cancel"),
            ("POST", "/api/v1/work-packages"),
            ("PATCH", "/api/v1/work-packages/EFF-706C"),
            ("POST", "/api/v1/work-packages/EFF-706C/close"),
            ("POST", "/api/v1/work-packages/EFF-706C/cancel"),
            ("PUT", "/api/v1/projects/P-T1/co-managers/C-COORD-A"),
            ("DELETE", "/api/v1/projects/P-T1/co-managers/C-COORD-A"),
            # Defense at the middleware boundary also rejects forged DELETEs;
            # the current public Demands/WorkPackages API has no DELETE route.
            ("DELETE", "/api/v1/work-packages/EFF-706C"),
            ("DELETE", "/api/v1/demands/DMO-APPROVAL"),
        )
        with self.client(ROLE_TECHNICIAN) as client:
            for method, path in mutations:
                with self.subTest(method=method, path=path):
                    response = client.request(method, path, json={})
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["error"]["code"], "permission_denied")
        # The refused requests must not have changed existing rows.
        engine = create_sql_engine(self.database_url)
        try:
            factory = create_session_factory(engine)
            with factory() as session:
                wp = session.get(WorkPackage, "WP-706C")
                self.assertIsNotNone(wp)
                self.assertEqual(wp.name, "WP-SECRET-706C")
                self.assertEqual(wp.status, "planned")
        finally:
            engine.dispose()

    def test_multirole_union_preserves_catalogues_but_not_ungranted_demands(self) -> None:
        for role in (ROLE_ADMIN, ROLE_COORDINATOR, ROLE_MANAGER, ROLE_PROJECT_MANAGER):
            with self.subTest(role=role), self.client(
                ROLE_TECHNICIAN, role,
                user_id="U-PM" if role == ROLE_PROJECT_MANAGER else "U-TECH",
                employee_external_id="EMP-PM" if role == ROLE_PROJECT_MANAGER else "EMP-TECH",
            ) as client:
                principal = client.get("/api/v1/auth/me")
                self.assertEqual(principal.status_code, 200)
                permissions = set(principal.json()["permissions"])
                self.assertTrue({"read_demands", "read_projects", "read_work_packages"} <= permissions)
                for path in ("/api/v1/projects", "/api/v1/work-packages", "/api/v1/demands"):
                    response = client.get(path, params={"scope": "mine"})
                    self.assertEqual(response.status_code, 200, (role, path, response.text))

        with self.client(ROLE_TECHNICIAN, ROLE_DELIVERY_CONTRIBUTOR) as client:
            permissions = set(client.get("/api/v1/auth/me").json()["permissions"])
            self.assertIn("read_projects", permissions)
            self.assertIn("read_work_packages", permissions)
            self.assertNotIn("read_demands", permissions)
            self.assertEqual(client.get("/api/v1/projects?scope=mine").status_code, 200)
            self.assertEqual(client.get("/api/v1/work-packages?scope=mine").status_code, 200)
            self.assert_private_module_denied(client.get("/api/v1/demands/DMO-APPROVAL"))

    def test_personal_and_exact_planning_neighbours_do_not_unlock_modules(self) -> None:
        with self.client(ROLE_TECHNICIAN) as client:
            schedule = client.get(
                "/api/v1/me/schedule",
                params={"start": DAY_1.isoformat(), "end": DAY_2.isoformat()},
            )
            snapshot = client.get(
                "/api/v1/planning/snapshot",
                params={"start": DAY_1.isoformat(), "end": DAY_2.isoformat()},
            )
            global_scope = client.get(
                "/api/v1/planning/snapshot",
                params={"start": DAY_1.isoformat(), "end": DAY_2.isoformat(), "scope": "global"},
            )
            self.assertEqual(schedule.status_code, 200, schedule.text)
            self.assertEqual(snapshot.status_code, 200, snapshot.text)
            self.assertEqual(
                {(row["project_number"], row["work_date"]) for row in snapshot.json()["shifts"]},
                {("P-201", DAY_1.isoformat()), ("P-202", DAY_2.isoformat())},
            )
            for secret in (
                "CARTESIAN-P1-D2-SECRET", "CARTESIAN-P2-D1-SECRET",
                "TRANSITIVE-P3-D1-SECRET", "COLLEAGUE-PRIVATE-CONTACT",
                "NEIGHBOR-DAY1-PRIVATE", "WP-SECRET-706C",
            ):
                self.assertNotIn(secret, snapshot.text)
            self.assertEqual(global_scope.status_code, 403)
            self.assertEqual(global_scope.json()["error"]["code"], "planning_scope_not_authorized")
            # A real shift on P-T1 is not authority for the corresponding project or demand.
            self.assert_private_module_denied(client.get("/api/v1/projects", params={"scope": "mine"}))
            self.assert_private_module_denied(client.get("/api/v1/demands/DMO-APPROVAL"))

    def test_technician_without_linked_resource_stays_fail_closed(self) -> None:
        with self.client(
            ROLE_TECHNICIAN, user_id="U-NO-RESOURCE", employee_external_id=None
        ) as client:
            self.assertEqual(client.get("/api/v1/auth/me").status_code, 200)
            schedule = client.get(
                "/api/v1/me/schedule",
                params={"start": DAY_1.isoformat(), "end": DAY_2.isoformat()},
            )
            self.assertEqual(schedule.status_code, 200, schedule.text)
            self.assert_private_module_denied(client.get("/api/v1/projects"))
            self.assert_private_module_denied(client.get("/api/v1/demands"))
            self.assert_private_module_denied(client.get("/api/v1/work-packages"))


if __name__ == "__main__":
    unittest.main()
