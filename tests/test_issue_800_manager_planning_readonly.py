from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application.security import (
    PERMISSION_APPROVE_DEMANDS,
    PERMISSION_MANAGE_DEMANDS,
    PERMISSION_MANAGE_PLANNING,
    PERMISSION_MANAGE_RESOURCES,
    PERMISSION_OVERRIDE_PLANNING_WINDOW,
    PERMISSION_READ,
    PERMISSION_READ_DEMANDS,
    PERMISSION_READ_PROJECTS,
    PERMISSION_READ_WORK_PACKAGES,
    ROLE_COORDINATOR,
    ROLE_MANAGER,
    AuthPrincipal,
    permissions_for_roles,
)
from app.infrastructure.sql import (
    ResourceRequirement,
    Shift,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.sqlite_test_template import SqliteDatabaseTemplate
from tests.test_planning_visibility_security_acceptance import (
    DAY_1,
    DAY_2,
    PlanningVisibilitySecurityAcceptanceTests,
)


class Issue800ManagerPlanningReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="issue-800-manager-planning-readonly.db",
            seed=PlanningVisibilitySecurityAcceptanceTests._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database_url = self._database_template.copy_to(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _principal(*roles: str) -> AuthPrincipal:
        return AuthPrincipal.from_roles(
            local_user_id="U-800-MANAGER",
            issuer="urn:resourceplanner:test",
            subject="issue-800-manager",
            display_name="Gestionnaire #800",
            email=None,
            roles=roles,
            auth_mode="test",
        )

    def _client(self, *roles: str) -> TestClient:
        return TestClient(
            create_api_app(
                self.database_url,
                auth_resolver=static_auth_resolver(self._principal(*roles)),
            ),
            raise_server_exceptions=False,
        )

    def _create_known_shift(self, *, key: str) -> str:
        with self._client(ROLE_COORDINATOR) as client:
            created = client.post(
                "/api/v1/quick-shifts",
                headers={"Idempotency-Key": key},
                json={
                    "project_number": "P-100",
                    "project_name": "Projet visible du chargé",
                    "technician": "Ressource visible PM",
                    "day": DAY_1.isoformat(),
                    "hours": 2,
                    "outside_standard_hours": True,
                    "note": "Issue 800 canonical Manager visibility fixture",
                },
            )
        self.assertEqual(created.status_code, 201, created.text)
        allocation_id = created.json()["allocation_id"]
        self.assertTrue(allocation_id)
        return str(allocation_id)

    def _durable_counts(self) -> tuple[int, int]:
        engine = create_sql_engine(self.database_url)
        try:
            factory = create_session_factory(engine)
            with factory() as session:
                requirement_count = int(
                    session.scalar(select(func.count()).select_from(ResourceRequirement)) or 0
                )
                shift_count = int(
                    session.scalar(select(func.count()).select_from(Shift)) or 0
                )
            return requirement_count, shift_count
        finally:
            engine.dispose()

    def test_manager_permission_contract_is_read_without_planning_mutation(self) -> None:
        permissions = set(permissions_for_roles((ROLE_MANAGER,)))

        self.assertEqual(
            permissions,
            {
                PERMISSION_READ,
                PERMISSION_READ_DEMANDS,
                PERMISSION_READ_PROJECTS,
                PERMISSION_READ_WORK_PACKAGES,
                PERMISSION_APPROVE_DEMANDS,
            },
        )
        self.assertTrue(
            {
                PERMISSION_MANAGE_PLANNING,
                PERMISSION_OVERRIDE_PLANNING_WINDOW,
                PERMISSION_MANAGE_DEMANDS,
                PERMISSION_MANAGE_RESOURCES,
            }.isdisjoint(permissions)
        )

    def test_manager_policy_and_real_planning_reads_are_global_and_nonempty(self) -> None:
        known_allocation_id = self._create_known_shift(
            key="issue-800-manager-visible",
        )
        params = {
            "start": DAY_1.isoformat(),
            "end": DAY_2.isoformat(),
            "scope": "global",
        }
        with self._client(ROLE_MANAGER) as client:
            policy = client.get("/api/v1/me/planning-policy")
            snapshot = client.get("/api/v1/planning/snapshot", params=params)
            actions = client.get("/api/v1/planning/actions", params=params)
            capacity = client.get("/api/v1/planning/capacity-grid", params=params)

        self.assertEqual(policy.status_code, 200, policy.text)
        self.assertEqual(policy.json()["available_scopes"], ["mine", "global"])
        self.assertEqual(policy.json()["default_scope"], "global")

        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        payload = snapshot.json()
        self.assertGreater(len(payload["resources"]), 0)
        known_shift = next(
            (
                row
                for row in payload["shifts"]
                if row["allocation_id"] == known_allocation_id
            ),
            None,
        )
        self.assertIsNotNone(known_shift, snapshot.text)
        self.assertEqual(known_shift["project_number"], "P-100")
        self.assertEqual(known_shift["work_date"], DAY_1.isoformat())

        self.assertEqual(actions.status_code, 200, actions.text)
        self.assertIsInstance(actions.json(), list)
        self.assertEqual(capacity.status_code, 200, capacity.text)

    def test_manager_direct_quick_shift_is_403_and_durable_state_is_unchanged(self) -> None:
        known_allocation_id = self._create_known_shift(
            key="issue-800-manager-denial-baseline",
        )
        params = {
            "start": DAY_1.isoformat(),
            "end": DAY_2.isoformat(),
            "scope": "global",
        }
        before_counts = self._durable_counts()

        with self._client(ROLE_MANAGER) as client:
            before = client.get("/api/v1/planning/snapshot", params=params)
            self.assertIn(
                known_allocation_id,
                {row["allocation_id"] for row in before.json()["shifts"]},
            )
            denied = client.post(
                "/api/v1/quick-shifts",
                headers={"Idempotency-Key": "issue-800-manager-denied"},
                json={
                    "project_number": "P-100",
                    "project_name": "Projet visible du chargé",
                    "technician": "Ressource visible PM",
                    "day": DAY_1.isoformat(),
                    "hours": 1,
                    "outside_standard_hours": True,
                    "note": "must never persist",
                },
            )
            after = client.get("/api/v1/planning/snapshot", params=params)

        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "permission_denied")
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(after.json(), before.json())
        self.assertEqual(self._durable_counts(), before_counts)

    def test_manager_coordinator_permissions_are_additive(self) -> None:
        manager = set(permissions_for_roles((ROLE_MANAGER,)))
        coordinator = set(permissions_for_roles((ROLE_COORDINATOR,)))
        combined = set(permissions_for_roles((ROLE_MANAGER, ROLE_COORDINATOR)))

        self.assertEqual(combined, manager | coordinator)
        self.assertIn(PERMISSION_MANAGE_PLANNING, combined)
        self.assertIn(PERMISSION_OVERRIDE_PLANNING_WINDOW, combined)

        with self._client(ROLE_MANAGER, ROLE_COORDINATOR) as client:
            principal = client.get("/api/v1/auth/me")
            policy = client.get("/api/v1/me/planning-policy")

        self.assertEqual(principal.status_code, 200, principal.text)
        self.assertEqual(set(principal.json()["roles"]), {ROLE_MANAGER, ROLE_COORDINATOR})
        self.assertIn(PERMISSION_MANAGE_PLANNING, principal.json()["permissions"])
        self.assertEqual(policy.status_code, 200, policy.text)
        self.assertEqual(policy.json()["default_scope"], "global")


if __name__ == "__main__":
    unittest.main()
