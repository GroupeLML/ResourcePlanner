from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.security import (
    ROLE_COORDINATOR,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    AuthPrincipal,
)
from app.infrastructure.sql import (
    AppUser,
    ApprovalRequirement,
    ApprovalRequirementApprover,
    BusinessContact,
    Project,
    RequestApprovalCycle,
    RequestLine,
    Resource,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.sqlite_test_template import SqliteDatabaseTemplate


DAY_1 = date(2026, 10, 5)
DAY_2 = date(2026, 10, 6)


class PlanningVisibilitySecurityAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add_all(
            [
                BusinessContact(
                    id="C-COORD-A",
                    display_name="Coordonnateur Alpha",
                    active=True,
                ),
                BusinessContact(
                    id="C-COORD-B",
                    display_name="Coordonnateur Beta",
                    active=True,
                ),
                AppUser(
                    id="U-COORD-A",
                    issuer="urn:resourceplanner:local",
                    subject="coord-a",
                    display_name="Coordonnateur Alpha",
                    email=None,
                    business_contact_id="C-COORD-A",
                    roles_json=json.dumps([ROLE_COORDINATOR]),
                    active=True,
                ),
                AppUser(
                    id="U-COORD-B",
                    issuer="urn:resourceplanner:local",
                    subject="coord-b",
                    display_name="Coordonnateur Beta",
                    email=None,
                    business_contact_id="C-COORD-B",
                    roles_json=json.dumps([ROLE_COORDINATOR]),
                    active=True,
                ),
                AppUser(
                    id="U-TECH",
                    issuer="urn:resourceplanner:local",
                    subject="technician",
                    display_name="Technicien",
                    email=None,
                    employee_external_id="EMP-TECH",
                    roles_json=json.dumps([ROLE_TECHNICIAN]),
                    active=True,
                ),
                Project(
                    id="P-PM",
                    number="P-100",
                    name="Projet visible du chargé",
                    project_manager_external_id="EMP-PM",
                    project_manager_name="Chargé sécurité",
                    status="Actif",
                ),
                Project(
                    id="P-OUT",
                    number="P-900",
                    name="Projet hors périmètre",
                    status="Actif",
                ),
                Project(
                    id="P-T1",
                    number="P-201",
                    name="Projet technicien jour 1",
                    status="Actif",
                ),
                Project(
                    id="P-T2",
                    number="P-202",
                    name="Projet technicien jour 2",
                    status="Actif",
                ),
                Project(
                    id="P-T3",
                    number="P-203",
                    name="Projet collègue seulement",
                    status="Actif",
                ),
                Project(
                    id="P-APP",
                    number="P-300",
                    name="Projet approbation",
                    status="Actif",
                ),
                Resource(
                    id="R-PM-VISIBLE",
                    name="Ressource visible PM",
                    active=True,
                    erp_active=True,
                ),
                Resource(
                    id="R-PM-HIDDEN",
                    name="Ressource cachée PM",
                    email="pm-hidden@example.test",
                    note="PM-HIDDEN-RESOURCE-SECRET",
                    active=True,
                    erp_active=True,
                ),
                Resource(
                    id="R-TECH",
                    external_id="EMP-TECH",
                    name="Technicien",
                    active=True,
                    erp_active=True,
                ),
                Resource(
                    id="R-COLLEAGUE",
                    external_id="EMP-COLLEAGUE",
                    name="Collègue",
                    email="colleague-private@example.test",
                    note="COLLEAGUE-PRIVATE-RESOURCE-NOTE",
                    active=True,
                    erp_active=True,
                ),
                Resource(
                    id="R-COORD-A",
                    name="Ressource coordonnée Alpha sans quart",
                    coordinator_contact_id="C-COORD-A",
                    active=True,
                    erp_active=True,
                ),
                Resource(
                    id="R-COORD-B",
                    name="Ressource coordonnée Beta sans quart",
                    coordinator_contact_id="C-COORD-B",
                    active=True,
                    erp_active=True,
                ),
            ]
        )
        session.flush()

        requirements = [
            ResourceRequirement(
                id="REQ-PM-VISIBLE",
                legacy_segment_id="SEG-PM-VISIBLE",
                project_id="P-PM",
                assigned_resource_id="R-PM-VISIBLE",
                start_date=DAY_1,
                end_date=DAY_1,
                planned_hours=Decimal("8"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            ),
            ResourceRequirement(
                id="REQ-PM-HIDDEN",
                legacy_segment_id="SEG-PM-HIDDEN",
                project_id="P-OUT",
                assigned_resource_id="R-PM-HIDDEN",
                start_date=DAY_1,
                end_date=DAY_1,
                planned_hours=Decimal("8"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            ),
            ResourceRequirement(
                id="REQ-T-OWN1",
                legacy_segment_id="SEG-T-OWN1",
                project_id="P-T1",
                assigned_resource_id="R-TECH",
                start_date=DAY_1,
                end_date=DAY_1,
                planned_hours=Decimal("8"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            ),
            ResourceRequirement(
                id="REQ-T-OWN2",
                legacy_segment_id="SEG-T-OWN2",
                project_id="P-T2",
                assigned_resource_id="R-TECH",
                start_date=DAY_2,
                end_date=DAY_2,
                planned_hours=Decimal("8"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            ),
            ResourceRequirement(
                id="REQ-T-N1",
                legacy_segment_id="SEG-T-N1",
                project_id="P-T1",
                assigned_resource_id="R-COLLEAGUE",
                start_date=DAY_1,
                end_date=DAY_1,
                planned_hours=Decimal("8"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            ),
            ResourceRequirement(
                id="REQ-T-N2",
                legacy_segment_id="SEG-T-N2",
                project_id="P-T2",
                assigned_resource_id="R-COLLEAGUE",
                start_date=DAY_2,
                end_date=DAY_2,
                planned_hours=Decimal("8"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            ),
            ResourceRequirement(
                id="REQ-T-CARTESIAN-1",
                legacy_segment_id="SEG-T-CARTESIAN-1",
                project_id="P-T1",
                assigned_resource_id="R-COLLEAGUE",
                start_date=DAY_2,
                end_date=DAY_2,
                planned_hours=Decimal("8"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            ),
            ResourceRequirement(
                id="REQ-T-CARTESIAN-2",
                legacy_segment_id="SEG-T-CARTESIAN-2",
                project_id="P-T2",
                assigned_resource_id="R-COLLEAGUE",
                start_date=DAY_1,
                end_date=DAY_1,
                planned_hours=Decimal("8"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            ),
            ResourceRequirement(
                id="REQ-T-TRANSITIVE",
                legacy_segment_id="SEG-T-TRANSITIVE",
                project_id="P-T3",
                assigned_resource_id="R-COLLEAGUE",
                start_date=DAY_1,
                end_date=DAY_1,
                planned_hours=Decimal("8"),
                status="Planifié",
                confirmation="Confirmée",
                origin="AD_HOC",
            ),
        ]
        session.add_all(requirements)
        session.flush()

        session.add_all(
            [
                Shift(
                    id="SHIFT-PM-VISIBLE",
                    resource_requirement_id="REQ-PM-VISIBLE",
                    resource_id="R-PM-VISIBLE",
                    work_date=DAY_1,
                    hours=Decimal("8"),
                    source="AUTO",
                    confirmation="Confirmée",
                ),
                Shift(
                    id="SHIFT-PM-HIDDEN",
                    resource_requirement_id="REQ-PM-HIDDEN",
                    resource_id="R-PM-HIDDEN",
                    work_date=DAY_1,
                    hours=Decimal("8"),
                    source="AUTO",
                    confirmation="Confirmée",
                    note="PM-HIDDEN-SHIFT-SECRET",
                ),
                Shift(
                    id="SHIFT-T-OWN1",
                    resource_requirement_id="REQ-T-OWN1",
                    resource_id="R-TECH",
                    work_date=DAY_1,
                    hours=Decimal("8"),
                    source="AUTO",
                    confirmation="Confirmée",
                ),
                Shift(
                    id="SHIFT-T-OWN2",
                    resource_requirement_id="REQ-T-OWN2",
                    resource_id="R-TECH",
                    work_date=DAY_2,
                    hours=Decimal("8"),
                    source="AUTO",
                    confirmation="Confirmée",
                ),
                Shift(
                    id="SHIFT-T-N1",
                    resource_requirement_id="REQ-T-N1",
                    resource_id="R-COLLEAGUE",
                    work_date=DAY_1,
                    hours=Decimal("8"),
                    source="AUTO",
                    confirmation="Confirmée",
                    note="NEIGHBOR-DAY1-PRIVATE",
                ),
                Shift(
                    id="SHIFT-T-N2",
                    resource_requirement_id="REQ-T-N2",
                    resource_id="R-COLLEAGUE",
                    work_date=DAY_2,
                    hours=Decimal("8"),
                    source="AUTO",
                    confirmation="Confirmée",
                    note="NEIGHBOR-DAY2-PRIVATE",
                ),
                Shift(
                    id="SHIFT-T-CARTESIAN-1",
                    resource_requirement_id="REQ-T-CARTESIAN-1",
                    resource_id="R-COLLEAGUE",
                    work_date=DAY_2,
                    hours=Decimal("8"),
                    source="AUTO",
                    confirmation="Confirmée",
                    note="CARTESIAN-P1-D2-SECRET",
                ),
                Shift(
                    id="SHIFT-T-CARTESIAN-2",
                    resource_requirement_id="REQ-T-CARTESIAN-2",
                    resource_id="R-COLLEAGUE",
                    work_date=DAY_1,
                    hours=Decimal("8"),
                    source="AUTO",
                    confirmation="Confirmée",
                    note="CARTESIAN-P2-D1-SECRET",
                ),
                Shift(
                    id="SHIFT-T-TRANSITIVE",
                    resource_requirement_id="REQ-T-TRANSITIVE",
                    resource_id="R-COLLEAGUE",
                    work_date=DAY_1,
                    hours=Decimal("8"),
                    source="AUTO",
                    confirmation="Confirmée",
                    note="TRANSITIVE-P3-D1-SECRET",
                ),
            ]
        )

        session.add(
            WorkforceRequest(
                id="D-APPROVAL",
                legacy_demand_number="DMO-APPROVAL",
                project_id="P-APP",
                status="Soumise",
                aggregate_version=1,
                line_mode=True,
            )
        )
        session.flush()
        session.add(
            RequestLine(
                id="L-APPROVAL",
                workforce_request_id="D-APPROVAL",
                position=0,
                active=True,
            )
        )
        session.flush()
        session.add(
            RequestApprovalCycle(
                id="CYCLE-APPROVAL-A",
                workforce_request_id="D-APPROVAL",
                submitted_request_version=1,
                state="OPEN",
                subject_fingerprint="a" * 64,
            )
        )
        session.flush()
        session.add(
            ApprovalRequirement(
                id="APPROVAL-REQ-A",
                approval_cycle_id="CYCLE-APPROVAL-A",
                request_line_id="L-APPROVAL",
                routing_sources_text='["TEST"]',
            )
        )
        session.flush()
        session.add(
            ApprovalRequirementApprover(
                requirement_id="APPROVAL-REQ-A",
                app_user_id="U-COORD-A",
                sources_text='["TEST"]',
            )
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="planning-visibility-security-acceptance.db",
            seed=cls._seed_database,
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
    def _principal(
        user_id: str,
        role: str,
        *,
        employee_external_id: str | None = None,
    ) -> AuthPrincipal:
        return AuthPrincipal.from_roles(
            local_user_id=user_id,
            issuer="urn:resourceplanner:local",
            subject=user_id.casefold(),
            display_name=user_id,
            email=None,
            employee_external_id=employee_external_id,
            roles=(role,),
            auth_mode="local",
        )

    def _client(
        self,
        user_id: str,
        role: str,
        *,
        employee_external_id: str | None = None,
    ) -> TestClient:
        return TestClient(
            create_api_app(
                self.database_url,
                auth_resolver=static_auth_resolver(
                    self._principal(
                        user_id,
                        role,
                        employee_external_id=employee_external_id,
                    )
                ),
            )
        )

    def _planning_snapshot(
        self,
        client: TestClient,
        *,
        scope: str | None = None,
    ):
        params = {
            "start": DAY_1.isoformat(),
            "end": DAY_2.isoformat(),
        }
        if scope is not None:
            params["scope"] = scope
        return client.get("/api/v1/planning/snapshot", params=params)

    def _demand_numbers(self, user_id: str) -> set[str]:
        with self._client(user_id, ROLE_COORDINATOR) as client:
            response = client.get("/api/v1/demands", params={"scope": "mine"})
        self.assertEqual(response.status_code, 200, response.text)
        return {row["number"] for row in response.json()}

    def _replace_approval_cycle_with_beta(self) -> None:
        engine = create_sql_engine(self.database_url)
        try:
            factory = create_session_factory(engine)
            with factory.begin() as session:
                first_cycle = session.get(
                    RequestApprovalCycle,
                    "CYCLE-APPROVAL-A",
                )
                self.assertIsNotNone(first_cycle)
                first_cycle.state = "COMPLETED"

                session.add(
                    RequestApprovalCycle(
                        id="CYCLE-APPROVAL-B",
                        workforce_request_id="D-APPROVAL",
                        submitted_request_version=1,
                        state="OPEN",
                        subject_fingerprint="b" * 64,
                    )
                )
                session.flush()
                session.add(
                    ApprovalRequirement(
                        id="APPROVAL-REQ-B",
                        approval_cycle_id="CYCLE-APPROVAL-B",
                        request_line_id="L-APPROVAL",
                        routing_sources_text='["TEST"]',
                    )
                )
                session.flush()
                session.add(
                    ApprovalRequirementApprover(
                        requirement_id="APPROVAL-REQ-B",
                        app_user_id="U-COORD-B",
                        sources_text='["TEST"]',
                    )
                )
        finally:
            engine.dispose()

    def test_project_manager_omitted_scope_is_fail_closed_and_global_is_rejected(
        self,
    ) -> None:
        with self._client(
            "U-PM",
            ROLE_PROJECT_MANAGER,
            employee_external_id="EMP-PM",
        ) as client:
            default_scope = self._planning_snapshot(client)
            forbidden_global = self._planning_snapshot(client, scope="global")

        self.assertEqual(default_scope.status_code, 200, default_scope.text)
        payload = default_scope.json()
        self.assertEqual(
            {row["project_number"] for row in payload["shifts"]},
            {"P-100"},
        )
        self.assertEqual(
            {row["name"] for row in payload["resources"]},
            {"Ressource visible PM"},
        )
        for forbidden_value in (
            "P-900",
            "SHIFT-PM-HIDDEN",
            "PM-HIDDEN-SHIFT-SECRET",
            "PM-HIDDEN-RESOURCE-SECRET",
            "pm-hidden@example.test",
        ):
            self.assertNotIn(forbidden_value, default_scope.text)

        self.assertEqual(forbidden_global.status_code, 403, forbidden_global.text)
        self.assertEqual(
            forbidden_global.json()["error"]["code"],
            "planning_scope_not_authorized",
        )

    def test_technician_neighbors_are_exact_project_day_pairs_without_transitive_leak(
        self,
    ) -> None:
        with self._client(
            "U-TECH",
            ROLE_TECHNICIAN,
            employee_external_id="EMP-TECH",
        ) as client:
            response = self._planning_snapshot(client)
            direct_neighbor_history = client.get(
                "/api/v1/shifts/SHIFT-T-N1/history",
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        shifts = payload["shifts"]
        self.assertEqual(
            {
                (row["project_number"], row["work_date"])
                for row in shifts
            },
            {
                ("P-201", DAY_1.isoformat()),
                ("P-202", DAY_2.isoformat()),
            },
        )
        neighbors = [row for row in shifts if row["source"] == "SCOPE_NEIGHBOR"]
        self.assertEqual(len(neighbors), 2)
        self.assertEqual(
            {
                (row["project_number"], row["work_date"])
                for row in neighbors
            },
            {
                ("P-201", DAY_1.isoformat()),
                ("P-202", DAY_2.isoformat()),
            },
        )
        for row in neighbors:
            self.assertTrue(row["allocation_id"].startswith("scope-neighbor:"))
            self.assertEqual(row["segment_id"], "")
            self.assertIsNone(row["requirement_id"])
            self.assertIsNone(row["demand_id"])
            self.assertIsNone(row["demand_number"])
            self.assertIsNone(row["project_id"])
            self.assertIsNone(row["note"])

        for forbidden_value in (
            "P-203",
            "SHIFT-T-N1",
            "SHIFT-T-N2",
            "SHIFT-T-CARTESIAN-1",
            "SHIFT-T-CARTESIAN-2",
            "SHIFT-T-TRANSITIVE",
            "NEIGHBOR-DAY1-PRIVATE",
            "NEIGHBOR-DAY2-PRIVATE",
            "CARTESIAN-P1-D2-SECRET",
            "CARTESIAN-P2-D1-SECRET",
            "TRANSITIVE-P3-D1-SECRET",
            "colleague-private@example.test",
            "COLLEAGUE-PRIVATE-RESOURCE-NOTE",
        ):
            self.assertNotIn(forbidden_value, response.text)

        self.assertEqual(
            direct_neighbor_history.status_code,
            404,
            direct_neighbor_history.text,
        )
        self.assertEqual(
            direct_neighbor_history.json()["error"]["code"],
            "shift_not_found",
        )

    def test_two_coordinators_have_distinct_mine_resources_even_without_shifts(
        self,
    ) -> None:
        with self._client("U-COORD-A", ROLE_COORDINATOR) as alpha_client:
            alpha = self._planning_snapshot(alpha_client, scope="mine")
        with self._client("U-COORD-B", ROLE_COORDINATOR) as beta_client:
            beta = self._planning_snapshot(beta_client, scope="mine")

        self.assertEqual(alpha.status_code, 200, alpha.text)
        self.assertEqual(beta.status_code, 200, beta.text)
        self.assertEqual(
            {row["name"] for row in alpha.json()["resources"]},
            {"Ressource coordonnée Alpha sans quart"},
        )
        self.assertEqual(
            {row["name"] for row in beta.json()["resources"]},
            {"Ressource coordonnée Beta sans quart"},
        )
        self.assertEqual(alpha.json()["shifts"], [])
        self.assertEqual(beta.json()["shifts"], [])
        self.assertNotIn("Ressource coordonnée Beta sans quart", alpha.text)
        self.assertNotIn("Ressource coordonnée Alpha sans quart", beta.text)

    def test_approval_visibility_tracks_current_open_cycle_and_reapproval(
        self,
    ) -> None:
        self.assertIn("DMO-APPROVAL", self._demand_numbers("U-COORD-A"))
        self.assertNotIn("DMO-APPROVAL", self._demand_numbers("U-COORD-B"))

        with self._client("U-COORD-B", ROLE_COORDINATOR) as beta_client:
            hidden_direct = beta_client.get(
                "/api/v1/demands/DMO-APPROVAL",
                params={"scope": "mine"},
            )
        self.assertEqual(hidden_direct.status_code, 404, hidden_direct.text)
        self.assertEqual(
            hidden_direct.json()["error"]["code"],
            "demand_not_found",
        )

        self._replace_approval_cycle_with_beta()

        self.assertNotIn("DMO-APPROVAL", self._demand_numbers("U-COORD-A"))
        self.assertIn("DMO-APPROVAL", self._demand_numbers("U-COORD-B"))

        with self._client("U-COORD-A", ROLE_COORDINATOR) as alpha_client:
            former_approver_direct = alpha_client.get(
                "/api/v1/demands/DMO-APPROVAL",
                params={"scope": "mine"},
            )
        with self._client("U-COORD-B", ROLE_COORDINATOR) as beta_client:
            current_approver_direct = beta_client.get(
                "/api/v1/demands/DMO-APPROVAL",
                params={"scope": "mine"},
            )

        self.assertEqual(
            former_approver_direct.status_code,
            404,
            former_approver_direct.text,
        )
        self.assertEqual(
            current_approver_direct.status_code,
            200,
            current_approver_direct.text,
        )


if __name__ == "__main__":
    unittest.main()
