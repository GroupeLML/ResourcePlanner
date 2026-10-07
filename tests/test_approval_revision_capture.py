from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import (
    Base,
    Project,
    RequestApprovalCycle,
    RequestApprovalReference,
    RequestApprovalRevision,
    ResourceRequirement,
    WorkforceRequest,
    WorkforceRequestPeriod,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.infrastructure.sql.approval_revision_repository import (
    SqlRequestApprovalRevisionRepository,
)
from app.server import create_api_app
from tests.approval_test_support import routed_demand_payload, seed_test_approval_routing
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


D1 = date(2026, 9, 21)
D2 = date(2026, 9, 22)


class ApprovalRevisionCaptureTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(Project(id="P1", number="P-1", name="Projet approbation"))
        seed_test_approval_routing(session, map_existing_tasks=True)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="approval-revisions.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _create_submitted_request(client: TestClient) -> tuple[str, str]:
        response = client.post(
            "/api/v1/demands",
            json=routed_demand_payload({
                "project_number": "P-1",
                "site_client": "Usine A",
                "location": "Zone 1",
                "description": "Demande avec alternatives",
                "submit": True,
                "lines": [
                    {
                        "desired_start": D1.isoformat(),
                        "desired_end": D2.isoformat(),
                        "estimated_hours": 8,
                        "confirmation": "Tentative",
                    }
                ],
            }),
        )
        assert response.status_code == 201, response.text
        number = response.json()["demand_number"]
        demand = client.get(f"/api/v1/demands/{number}")
        assert demand.status_code == 200, demand.text
        line_id = demand.json()["lines"][0]["line_id"]
        return number, line_id

    def test_approval_captures_all_alternatives_and_binds_materialized_requirement(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="coord-13b",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number, line_id = self._create_submitted_request(client)
                request_version = client.get(
                    f"/api/v1/demands/{number}"
                ).json()["version"]
                replaced = client.put(
                    f"/api/v1/demands/{number}/lines/{line_id}/periods",
                    json={
                        "expected_request_version": request_version,
                        "periods": [
                            {
                                "period_id": "OPT-A",
                                "start_date": D1.isoformat(),
                                "end_date": D1.isoformat(),
                                "hours": 8,
                                "kind": "ALTERNATIVE",
                                "alternative_group": "VISITE",
                                "confirmation": "Confirmée",
                                "resource_count": 1,
                            },
                            {
                                "period_id": "OPT-B",
                                "start_date": D2.isoformat(),
                                "end_date": D2.isoformat(),
                                "hours": 6,
                                "kind": "ALTERNATIVE",
                                "alternative_group": "VISITE",
                                "confirmation": "Tentative",
                                "resource_count": 1,
                            },
                        ]
                    },
                )
                self.assertEqual(replaced.status_code, 200, replaced.text)
                selected = client.put(
                    f"/api/v1/demands/{number}/lines/{line_id}/alternative-groups/VISITE/selection",
                    json={"period_id": "OPT-A"},
                )
                self.assertEqual(selected.status_code, 200, selected.text)

                approved = client.post(
                    f"/api/v1/demands/{number}/approve",
                    json={"comment": "Approbation #13B"},
                )
                self.assertEqual(approved.status_code, 200, approved.text)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    reference = session.get(RequestApprovalReference, request.id)
                    self.assertIsNotNone(reference)
                    assert reference is not None
                    self.assertEqual(reference.status, "CAPTURED")
                    self.assertIsNotNone(reference.active_revision_id)

                    revision = session.get(
                        RequestApprovalRevision,
                        reference.active_revision_id,
                    )
                    self.assertIsNotNone(revision)
                    assert revision is not None
                    completed_cycle = session.scalar(
                        select(RequestApprovalCycle).where(
                            RequestApprovalCycle.approved_revision_id == revision.id
                        )
                    )
                    self.assertIsNotNone(completed_cycle)
                    assert completed_cycle is not None
                    self.assertEqual(
                        revision.request_version,
                        completed_cycle.submitted_request_version,
                    )
                    self.assertEqual(revision.approved_by_name, "Administrateur de test explicite")
                    payload = json.loads(revision.payload_text)
                    self.assertEqual(payload["format_version"], 3)
                    self.assertEqual(
                        payload["request"]["request_id"],
                        request.id,
                    )
                    entries = payload["authorization"]["entries"]
                    self.assertEqual(len(entries), 2)
                    by_period = {
                        json.loads(row["identity"])[2]: row
                        for row in entries
                    }
                    self.assertEqual(set(by_period), {"OPT-A", "OPT-B"})
                    self.assertTrue(by_period["OPT-A"]["selected"])
                    self.assertFalse(by_period["OPT-B"]["selected"])
                    self.assertEqual(by_period["OPT-A"]["slot_count"], 1)
                    self.assertEqual(by_period["OPT-B"]["slot_count"], 1)
                    self.assertEqual(
                        by_period["OPT-A"]["confirmation_mode"],
                        "EXPLICIT",
                    )
                    self.assertEqual(
                        by_period["OPT-A"]["proposed_resource_mode"],
                        "EXPLICIT",
                    )
                    self.assertEqual(
                        by_period["OPT-A"]["location"],
                        "Zone 1",
                    )
                    self.assertTrue(by_period["OPT-A"]["source_period_id"])
                    self.assertTrue(by_period["OPT-B"]["source_period_id"])
                    self.assertNotEqual(
                        by_period["OPT-A"]["source_period_id"],
                        by_period["OPT-B"]["source_period_id"],
                    )

                    period_rows = session.scalars(
                        select(WorkforceRequestPeriod).where(
                            WorkforceRequestPeriod.workforce_request_id
                            == request.id,
                            WorkforceRequestPeriod.active.is_(True),
                        )
                    ).all()
                    self.assertEqual(
                        {row.id for row in period_rows},
                        {
                            by_period["OPT-A"]["source_period_id"],
                            by_period["OPT-B"]["source_period_id"],
                        },
                    )

                    requirements = session.scalars(
                        select(ResourceRequirement).where(
                            ResourceRequirement.workforce_request_id
                            == request.id,
                            ResourceRequirement.status != "Annulé",
                        )
                    ).all()
                    self.assertEqual(len(requirements), 1)
                    requirement = requirements[0]
                    self.assertEqual(
                        requirement.approval_revision_id,
                        revision.id,
                    )
                    self.assertEqual(
                        requirement.approval_reference_status,
                        "CAPTURED",
                    )
                    self.assertEqual(
                        requirement.approved_entry_key,
                        by_period["OPT-A"]["identity"],
                    )
                    self.assertEqual(
                        revision.authorization_fingerprint,
                        payload["authorization"]["authorization_fingerprint"],
                    )
            finally:
                engine.dispose()

    def test_revision_and_active_reference_roll_back_together(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="coord-rollback",
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                number, _line_id = self._create_submitted_request(client)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with self.assertRaisesRegex(RuntimeError, "forced rollback"):
                    with transactional_session(factory) as session:
                        request = session.scalar(
                            select(WorkforceRequest).where(
                                WorkforceRequest.legacy_demand_number == number
                            )
                        )
                        assert request is not None
                        repository = SqlRequestApprovalRevisionRepository(session)
                        revision = repository.create_revision(request)
                        repository.activate_revision(request, revision)
                        raise RuntimeError("forced rollback")

                with factory() as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    revisions = session.scalars(
                        select(RequestApprovalRevision).where(
                            RequestApprovalRevision.workforce_request_id
                            == request.id
                        )
                    ).all()
                    self.assertEqual(revisions, [])
                    self.assertIsNone(
                        session.get(RequestApprovalReference, request.id)
                    )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
