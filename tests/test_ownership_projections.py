from __future__ import annotations

from functools import partial

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.commands import DemandCreateCommand, DemandUpdateCommand
from app.application.security import AuthPrincipal, ROLE_COORDINATOR, ROLE_PROJECT_MANAGER
from app.infrastructure.sql import (
    Base,
    Project,
    Resource,
    ResourceRequirement,
    Shift,
    SqlUserIdentityRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver


DAY = date(2026, 8, 24)
ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class OwnershipProjectionTests(unittest.TestCase):
    def _database(self, directory: str) -> str:
        path = Path(directory) / "ownership.db"
        url = f"sqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with transactional_session(factory) as session:
            session.add(
                Project(
                    id="P1",
                    number="P-1",
                    name="Projet A",
                    client="Client A",
                    project_manager_name="Responsable A",
                    status="Actif",
                )
            )
            session.add(Resource(id="R1", name="Alice", active=True))
            identities = SqlUserIdentityRepository(session)
            coordinator = identities.upsert(
                issuer="urn:test:ownership",
                subject="coordinator",
                display_name="Coordonnateur",
                email=None,
                roles=(ROLE_COORDINATOR,),
            )
            marie = identities.upsert(
                issuer="urn:test:ownership",
                subject="marie",
                display_name="Marie",
                email=None,
                roles=(ROLE_PROJECT_MANAGER,),
            )
            alex = identities.upsert(
                issuer="urn:test:ownership",
                subject="alex",
                display_name="Alex",
                email=None,
                roles=(ROLE_PROJECT_MANAGER,),
            )
            self.coordinator_id = coordinator.user_id
            self.marie_id = marie.user_id
            self.alex_id = alex.user_id
        engine.dispose()
        return url

    def _coordinator_auth(self):
        return static_auth_resolver(
            AuthPrincipal.from_roles(
                local_user_id=self.coordinator_id,
                issuer="urn:test:ownership",
                subject="coordinator",
                display_name="Coordonnateur",
                email=None,
                roles=(ROLE_COORDINATOR,),
                auth_mode="test",
            )
        )

    def test_demand_commands_round_trip_explicit_requester(self) -> None:
        create = DemandCreateCommand.from_mapping(
            {
                "NumeroProjet": "P-1",
                "DateDebutSouhaitee": DAY,
                "Demandeur": "  Marie  ",
            }
        )
        update = DemandUpdateCommand.from_mapping(
            "DMO-1",
            {"Demandeur": "  Alex  "},
        )

        self.assertEqual(create.requester, "Marie")
        self.assertEqual(create.to_repository_values()["Demandeur"], "Marie")
        self.assertEqual(update.requester, "Alex")
        self.assertEqual(update.to_repository_values(), {"Demandeur": "Alex"})

    def test_requester_is_editable_without_reapproving_planning_envelope(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="Coordonnateur",
                auth_resolver=self._coordinator_auth(),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                created = client.post(
                    "/api/v1/demands",
                    json={
                        "project_number": "P-1",
                        "desired_start": DAY.isoformat(),
                        "requester_user_id": self.marie_id,
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                number = created.json()["demand_number"]

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with transactional_session(factory) as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    request.status = "En planification"
            finally:
                engine.dispose()

            with TestClient(app, raise_server_exceptions=False) as client:
                patched = client.patch(
                    f"/api/v1/demands/{number}",
                    json={
                        "requester_user_id": self.alex_id,
                        "comment": "Transfert opérationnel",
                    },
                )
                fetched = client.get(f"/api/v1/demands/{number}")

            self.assertEqual(patched.status_code, 200, patched.text)
            self.assertFalse(patched.json()["reapproval_required"])
            self.assertEqual(fetched.status_code, 200, fetched.text)
            self.assertEqual(fetched.json()["status"], "En planification")
            self.assertEqual(fetched.json()["requester_user_id"], self.alex_id)
            self.assertEqual(fetched.json()["requester"], "Alex")
            self.assertEqual(fetched.json()["project_manager"], "Responsable A")

    def test_sql_segment_and_shift_project_parent_ownership_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(
                database_url,
                actor_name="Coordonnateur",
                auth_resolver=self._coordinator_auth(),
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/demands",
                    json={
                        "project_number": "P-1",
                        "desired_start": DAY.isoformat(),
                        "requester_user_id": self.marie_id,
                    },
                )
                number = created.json()["demand_number"]

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with transactional_session(factory) as session:
                    request = session.scalar(
                        select(WorkforceRequest).where(
                            WorkforceRequest.legacy_demand_number == number
                        )
                    )
                    assert request is not None
                    session.add(
                        ResourceRequirement(
                            id="REQ1",
                            legacy_segment_id="SEG-1",
                            project_id="P1",
                            workforce_request_id=request.id,
                            assigned_resource_id="R1",
                            start_date=DAY,
                            end_date=DAY,
                            planned_hours=Decimal("4"),
                            status="Planifié",
                            origin="REQUEST",
                        )
                    )
                    session.flush()
                    session.add(
                        Shift(
                            id="SHIFT1",
                            legacy_allocation_id="MAN-1",
                            resource_requirement_id="REQ1",
                            resource_id="R1",
                            work_date=DAY,
                            hours=Decimal("4"),
                            source="MANUAL",
                            locked=True,
                        )
                    )
            finally:
                engine.dispose()

            with TestClient(app) as client:
                segment = client.get("/api/v1/segments/SEG-1").json()
                shifts = client.get(
                    f"/api/v1/shifts?start={DAY.isoformat()}&end={DAY.isoformat()}"
                ).json()

            self.assertEqual(segment["project_manager"], "Responsable A")
            self.assertEqual(segment["requester"], "Marie")
            self.assertEqual(shifts[0]["demand_number"], number)
            self.assertEqual(shifts[0]["project_number"], "P-1")
            self.assertEqual(shifts[0]["project_manager"], "Responsable A")
            self.assertEqual(shifts[0]["requester"], "Marie")




if __name__ == "__main__":
    unittest.main()
