from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import (
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import (
    TEST_ADMIN_AUTH_RESOLVER,
    TEST_PROJECT_MANAGER_AUTH_RESOLVER,
)
from tests.sqlite_test_template import SqliteDatabaseTemplate


D1 = date(2026, 9, 21)
D2 = date(2026, 9, 22)


class ApprovalStateReadApiTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet 13G"))
        session.add(Resource(id="R1", name="Alice", active=True))
        session.flush()
        session.add(
            ResourceAvailabilityRule(
                id="STD-R1",
                resource_id="R1",
                availability_type="Horaire standard",
                weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                start_time=time(8, 0),
                end_time=time(20, 0),
                active=True,
            )
        )
        seed_test_approval_routing(session, map_existing_tasks=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="approval-state-13g.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _create_and_approve(client: TestClient) -> str:
        created = client.post(
            "/api/v1/demands",
            json=routed_demand_payload({
                "project_number": "P-1",
                "desired_start": D1.isoformat(),
                "desired_end": D1.isoformat(),
                "estimated_hours": 8,
                "proposed_technician": "Alice",
                "submit": True,
            }),
        )
        assert created.status_code == 201, created.text
        number = created.json()["demand_number"]
        approved = client.post(
            f"/api/v1/demands/{number}/approve",
            json={"comment": "Approbation 13G"},
        )
        assert approved.status_code == 200, approved.text
        return number

    def test_read_contract_distinguishes_candidate_approved_and_active(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            admin_app = create_api_app(
                database_url,
                actor_name="admin-13g",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                number = self._create_and_approve(client)
                approved_state = client.get(
                    f"/api/v1/demands/{number}/approval-state"
                )
                self.assertEqual(
                    approved_state.status_code,
                    200,
                    approved_state.text,
                )
                approved = approved_state.json()
                self.assertEqual(approved["approval_reference_status"], "CAPTURED")
                self.assertTrue(approved["active_revision_id"])
                self.assertEqual(
                    approved["candidate_authorization_fingerprint"],
                    approved["authorization_fingerprint"],
                )
                self.assertTrue(approved["candidate_matches_approved"])
                self.assertEqual(approved["envelope_decision"], "WITHIN_ENVELOPE")
                self.assertEqual(approved["active_requirement_count"], 1)
                self.assertEqual(approved["active_planned_hours"], 8.0)
                self.assertTrue(approved["active_matches_approved_revision"])
                active_revision_id = approved["active_revision_id"]
                approved_request_version = approved["approved_request_version"]

            pm_app = create_api_app(
                database_url,
                actor_name="pm-13g",
                auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                changed = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "desired_start": D2.isoformat(),
                        "desired_end": D2.isoformat(),
                        "comment": "Proposition candidate déplacée",
                    },
                )
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertTrue(changed.json()["reapproval_required"])
                self.assertEqual(changed.json()["status"], "Soumise")

                state = client.get(
                    f"/api/v1/demands/{number}/approval-state"
                )
                self.assertEqual(state.status_code, 200, state.text)
                body = state.json()
                self.assertEqual(body["active_revision_id"], active_revision_id)
                self.assertEqual(
                    body["approved_request_version"],
                    approved_request_version,
                )
                self.assertGreater(
                    body["candidate_request_version"],
                    approved_request_version,
                )
                self.assertFalse(body["candidate_matches_approved"])
                self.assertNotEqual(
                    body["candidate_authorization_fingerprint"],
                    body["authorization_fingerprint"],
                )
                self.assertEqual(
                    body["envelope_decision"],
                    "REAPPROVAL_REQUIRED",
                )
                self.assertEqual(body["envelope_reason"], "WINDOW_CHANGED")
                self.assertEqual(body["active_requirement_count"], 1)
                self.assertEqual(body["active_planned_hours"], 8.0)
                self.assertTrue(body["active_matches_approved_revision"])

                delta = client.get(
                    f"/api/v1/demands/{number}/plan-delta"
                )
                self.assertEqual(delta.status_code, 200, delta.text)
                preview = delta.json()
                self.assertEqual(
                    preview["active_revision_id"],
                    active_revision_id,
                )
                self.assertEqual(
                    preview["authorization_fingerprint"],
                    body["authorization_fingerprint"],
                )
                self.assertEqual(
                    preview["candidate_authorization_fingerprint"],
                    body["candidate_authorization_fingerprint"],
                )
                self.assertEqual(
                    preview["envelope_decision"],
                    "REAPPROVAL_REQUIRED",
                )

    def test_operational_responsible_reapproval_opens_fresh_line_quorum(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            admin_app = create_api_app(
                database_url,
                actor_name="admin-612a",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                number = self._create_and_approve(client)

                before = client.get(
                    f"/api/v1/demands/{number}/approval-state"
                )
                self.assertEqual(before.status_code, 200, before.text)
                previous_revision_id = before.json()["active_revision_id"]
                self.assertTrue(previous_revision_id)

                contact = client.post(
                    "/api/v1/business-contacts",
                    json={"display_name": "Responsable 612A"},
                )
                self.assertEqual(contact.status_code, 201, contact.text)

                link = client.get(
                    f"/api/v1/demands/{number}/business-contacts"
                )
                self.assertEqual(link.status_code, 200, link.text)
                previous_version = link.json()["aggregate_version"]

                changed = client.patch(
                    f"/api/v1/demands/{number}/operational-responsible",
                    json={
                        "contact_id": contact.json()["id"],
                        "expected_version": previous_version,
                    },
                )
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertTrue(changed.json()["reapproval_required"])
                self.assertEqual(changed.json()["status"], "Soumise")
                self.assertGreater(changed.json()["version"], previous_version)

                detail = client.get(
                    f"/api/v1/demands/{number}/detail"
                )
                self.assertEqual(detail.status_code, 200, detail.text)
                cycle = detail.json()["approval_cycle"]
                self.assertIsNotNone(cycle)
                assert cycle is not None
                self.assertEqual(cycle["state"], "OPEN")
                self.assertFalse(cycle["quorum_complete"])
                self.assertEqual(cycle["satisfied_requirements"], 0)
                self.assertGreater(cycle["total_requirements"], 0)
                self.assertTrue(cycle["actor_approvable_requirement_ids"])
                for requirement in cycle["requirements"]:
                    self.assertFalse(requirement["satisfied"])
                    self.assertEqual(requirement["decisions"], [])

                pending_state = client.get(
                    f"/api/v1/demands/{number}/approval-state"
                )
                self.assertEqual(
                    pending_state.status_code,
                    200,
                    pending_state.text,
                )
                self.assertEqual(
                    pending_state.json()["active_revision_id"],
                    previous_revision_id,
                )

                reapproved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Réapprobation 612A"},
                )
                self.assertEqual(
                    reapproved.status_code,
                    200,
                    reapproved.text,
                )
                self.assertEqual(
                    reapproved.json()["status"],
                    "En planification",
                )

                final_state = client.get(
                    f"/api/v1/demands/{number}/approval-state"
                )
                self.assertEqual(final_state.status_code, 200, final_state.text)
                self.assertNotEqual(
                    final_state.json()["active_revision_id"],
                    previous_revision_id,
                )

    def test_plan_delta_exposes_structured_locked_blocker(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            admin_app = create_api_app(
                database_url,
                actor_name="admin-lock-13g",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(admin_app, raise_server_exceptions=False) as client:
                number = self._create_and_approve(client)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    requirement = session.scalar(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                    )
                    assert requirement is not None
                    shift = session.scalar(
                        select(Shift).where(
                            Shift.resource_requirement_id == requirement.id
                        )
                    )
                    if shift is None:
                        shift = Shift(
                            id="LOCK-13G",
                            resource_requirement_id=requirement.id,
                            resource_id="R1",
                            work_date=D1,
                            hours=Decimal("8"),
                            allocation_type="Manuel",
                            source="MANUAL",
                            locked=True,
                        )
                        session.add(shift)
                    else:
                        shift.locked = True
                        shift.source = "MANUAL"
                    locked_shift_id = shift.id
                    requirement_id = requirement.id
            finally:
                engine.dispose()

            pm_app = create_api_app(
                database_url,
                actor_name="pm-lock-13g",
                auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            )
            with TestClient(pm_app, raise_server_exceptions=False) as client:
                changed = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "desired_start": D2.isoformat(),
                        "desired_end": D2.isoformat(),
                        "comment": "Fenêtre candidate incompatible avec verrou",
                    },
                )
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertTrue(changed.json()["reapproval_required"])

                delta = client.get(
                    f"/api/v1/demands/{number}/plan-delta"
                )
                self.assertEqual(delta.status_code, 200, delta.text)
                body = delta.json()
                self.assertFalse(body["available"])
                self.assertEqual(
                    body["reason"],
                    "LOCKED_SHIFT_OUTSIDE_WINDOW",
                )
                self.assertEqual(len(body["diagnostics"]), 1)
                diagnostic = body["diagnostics"][0]
                self.assertEqual(
                    diagnostic["code"],
                    "LOCKED_SHIFT_OUTSIDE_WINDOW",
                )
                self.assertEqual(
                    diagnostic["requirement_id"],
                    requirement_id,
                )
                self.assertEqual(
                    diagnostic["shift_ids"],
                    [locked_shift_id],
                )
                self.assertIn("quart verrouillé", diagnostic["message"])
                self.assertEqual(
                    body["envelope_decision"],
                    "REAPPROVAL_REQUIRED",
                )


if __name__ == "__main__":
    unittest.main()
