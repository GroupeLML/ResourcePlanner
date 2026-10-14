from __future__ import annotations

from datetime import date, time
import json
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.application.security import (
    ROLE_COORDINATOR,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
    AuthPrincipal,
)
from app.infrastructure.sql import (
    AppUser,
    BusinessContact,
    Resource,
    ResourceAvailabilityRule,
    SqlPlannerQueryRepository,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.resource_class_models import ResourceClassConfig
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.sqlite_test_template import SqliteDatabaseTemplate


START = date(2026, 10, 5)
END = date(2026, 10, 6)
EXCAVATION_IDS = {"R-750-EXC-A1", "R-750-EXC-A2", "R-750-EXC-B"}
ELECTRICAL_ID = "R-750-ELEC"


class PlanningExcavation750Tests(unittest.TestCase):
    """#750 acceptance for the generic resource projection delivered by #749."""

    @staticmethod
    def _seed_database(session: Session) -> None:
        session.add_all([
            ResourceClassConfig(
                code="INSTALL_EXC", label="Installation excavation", active=True,
            ),
            ResourceClassConfig(
                code="INSTALL_ELEC", label="Installation \u00e9lectrique", active=True,
            ),
            ResourceClassConfig(
                code="EMPTY_750", label="Classe sans ressource", active=True,
            ),
        ])
        for owner in ("A", "B", "ELEC"):
            session.add(BusinessContact(
                id=f"C-750-{owner}", display_name=f"Coordination 750 {owner}", active=True,
            ))
        session.flush()
        for owner in ("A", "B", "ELEC"):
            session.add(AppUser(
                id=f"U-750-{owner}",
                issuer="urn:resourceplanner:local",
                subject=f"coord-750-{owner.lower()}",
                display_name=f"Coordination 750 {owner}",
                business_contact_id=f"C-750-{owner}",
                roles_json=json.dumps([ROLE_COORDINATOR]),
                active=True,
            ))
        for resource_id, owner in (
            ("R-750-EXC-A1", "A"),
            ("R-750-EXC-A2", "A"),
            ("R-750-EXC-B", "B"),
        ):
            session.add(Resource(
                id=resource_id,
                name=f"Excavation {resource_id}",
                resource_class="INSTALL_EXC",
                coordinator_contact_id=f"C-750-{owner}",
                active=True,
                erp_active=True,
            ))
        session.add_all([
            Resource(
                id=ELECTRICAL_ID, name="Installation 750 controle",
                resource_class="INSTALL_ELEC", coordinator_contact_id="C-750-ELEC",
                active=True, erp_active=True,
            ),
            Resource(
                id="R-750-INACTIVE", name="Excavation inactive",
                resource_class="INSTALL_EXC", coordinator_contact_id="C-750-A",
                active=False, erp_active=True,
            ),
            Resource(
                id="R-750-ERP-INACTIVE", name="Excavation ERP inactive",
                resource_class="INSTALL_EXC", coordinator_contact_id="C-750-A",
                active=True, erp_active=False,
            ),
        ])
        session.flush()
        # Only the control has a schedule. No excavation resource has a Shift,
        # ResourceProfile, auth identity, or standard schedule to anchor its group.
        session.add(ResourceAvailabilityRule(
            id="STD-750-ELEC", resource_id=ELECTRICAL_ID,
            availability_type="Horaire standard", start_date=START, end_date=END,
            weekdays="Lun,Mar", start_time=time(8, 0), end_time=time(16, 0), active=True,
        ))

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="planning-excavation-750.db", seed=cls._seed_database,
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

    def _client(self, owner: str, role: str = ROLE_COORDINATOR) -> TestClient:
        principal = AuthPrincipal.from_roles(
            local_user_id=f"U-750-{owner}", issuer="urn:resourceplanner:local",
            subject=f"coord-750-{owner.lower()}", display_name=f"750 {owner}",
            email=None, employee_external_id=None, roles=(role,), auth_mode="local",
        )
        return TestClient(create_api_app(
            self.database_url, auth_resolver=static_auth_resolver(principal),
        ))

    def _planning(self, client: TestClient, surface: str, scope: str | None = None):
        params = {"start": START.isoformat(), "end": END.isoformat()}
        if scope is not None:
            params["scope"] = scope
        response = client.get(f"/api/v1/planning/{surface}", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_global_projection_keeps_a_whole_class_without_schedules(self) -> None:
        engine = create_sql_engine(self.database_url)
        try:
            with create_session_factory(engine)() as session:
                # This was the pre-#749 global catalog: the whole excavation class
                # vanished, while the scheduled electrical control remained.
                schedulable = SqlPlannerQueryRepository(session).list_schedulable_resources(
                    start=START, end=END,
                )
                self.assertEqual({row.id for row in schedulable}, {ELECTRICAL_ID})
        finally:
            engine.dispose()

        with self._client("A") as client:
            snapshot = self._planning(client, "snapshot", "global")
            capacity = self._planning(client, "capacity-grid", "global")
            catalog = client.get("/api/v1/resource-classes")
            self.assertEqual(catalog.status_code, 200, catalog.text)

        expected = EXCAVATION_IDS | {ELECTRICAL_ID}
        ids = [row["id"] for row in snapshot["resources"]]
        self.assertCountEqual(ids, expected)  # Also detects duplicated rows.
        self.assertEqual(snapshot["shifts"], [])
        self.assertEqual(snapshot["pending_loads"], [])
        labels = {row["code"]: row["label"] for row in catalog.json()}
        self.assertEqual(labels["INSTALL_EXC"], "Installation excavation")
        self.assertEqual(labels["INSTALL_ELEC"], "Installation \u00e9lectrique")
        self.assertIn("EMPTY_750", labels)
        self.assertEqual(
            {row["resource_class"] for row in snapshot["resources"]},
            {"INSTALL_EXC", "INSTALL_ELEC"},
        )
        self.assertEqual(
            {row["id"] for row in snapshot["resources"] if row["resource_class"] == "INSTALL_EXC"},
            EXCAVATION_IDS,
        )
        self.assertCountEqual([row["resource_id"] for row in capacity["resources"]], expected)
        by_id = {row["resource_id"]: row for row in capacity["resources"]}
        for resource_id in EXCAVATION_IDS:
            with self.subTest(resource_id=resource_id):
                self.assertEqual(by_id[resource_id]["capacity_hours"], 0)
                self.assertTrue(all(
                    not day["available"] and day["reason"] == "Aucun horaire standard"
                    for day in by_id[resource_id]["days"]
                ))
        self.assertEqual(by_id[ELECTRICAL_ID]["capacity_hours"], 16)

    def test_coordinators_only_receive_their_own_same_class_resources(self) -> None:
        for owner, expected in (
            ("A", {"R-750-EXC-A1", "R-750-EXC-A2"}),
            ("B", {"R-750-EXC-B"}),
            ("ELEC", {ELECTRICAL_ID}),
        ):
            with self.subTest(owner=owner), self._client(owner) as client:
                snapshot = self._planning(client, "snapshot", "mine")
                capacity = self._planning(client, "capacity-grid", "mine")
                self.assertCountEqual([row["id"] for row in snapshot["resources"]], expected)
                self.assertCountEqual([row["resource_id"] for row in capacity["resources"]], expected)
                payload = json.dumps({"snapshot": snapshot, "capacity": capacity})
                for hidden in (EXCAVATION_IDS | {ELECTRICAL_ID}) - expected:
                    self.assertNotIn(hidden, payload)
                self.assertNotIn("R-750-INACTIVE", payload)
                self.assertNotIn("R-750-ERP-INACTIVE", payload)
                if owner == "ELEC":
                    self.assertNotIn("INSTALL_EXC", payload)

    def test_class_catalog_never_grants_a_restricted_user_global_planning(self) -> None:
        for role in (ROLE_PROJECT_MANAGER, ROLE_TECHNICIAN):
            with self.subTest(role=role), self._client("UNLINKED", role) as client:
                for surface in ("snapshot", "capacity-grid"):
                    self.assertEqual(self._planning(client, surface)["resources"], [])
                    forbidden = client.get(f"/api/v1/planning/{surface}", params={
                        "start": START.isoformat(), "end": END.isoformat(), "scope": "global",
                    })
                    self.assertEqual(forbidden.status_code, 403, forbidden.text)
                    self.assertEqual(forbidden.json()["error"]["code"], "planning_scope_not_authorized")


if __name__ == "__main__":
    unittest.main()
