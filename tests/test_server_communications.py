from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.application.security import AuthPrincipal, ROLE_COORDINATOR, ROLE_TECHNICIAN
from app.infrastructure.sql import Base, Resource, create_sql_engine
from app.server import create_api_app
from app.server.security import static_auth_resolver


class ServerCommunicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        path = Path(self.temp.name) / "communications.db"
        self.database_url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                Resource.__table__.insert().values(
                    id="R1",
                    external_id="EMP-1",
                    name="Technicien test",
                    email="tech" + chr(64) + "example.test",
                    active=True,
                    sort_order=0,
                )
            )
        engine.dispose()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _app(self, role: str):
        principal = AuthPrincipal.from_roles(
            local_user_id="U1",
            issuer="issuer",
            subject="subject",
            display_name="Utilisateur test",
            email=None,
            roles=(role,),
            auth_mode="test",
        )
        return create_api_app(
            self.database_url,
            auth_resolver=static_auth_resolver(principal),
        )

    def test_coordinator_can_read_contacts_and_preview(self) -> None:
        with TestClient(self._app(ROLE_COORDINATOR)) as client:
            contacts = client.get("/api/v1/communications/contacts")
            preview = client.get(
                "/api/v1/communications/preview?week_start=2026-09-21"
            )
        self.assertEqual(contacts.status_code, 200)
        self.assertEqual(contacts.json()[0]["recipient_id"], "resource:R1")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["mode"], "weekly_plan")

    def test_technician_cannot_read_contact_directory(self) -> None:
        with TestClient(self._app(ROLE_TECHNICIAN)) as client:
            response = client.get("/api/v1/communications/contacts")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")
        self.assertEqual(
            response.json()["error"]["context"]["required_permission"],
            "manage_communications",
        )

    def test_technician_cannot_download_project_draft_copy(self) -> None:
        with TestClient(self._app(ROLE_TECHNICIAN)) as client:
            response = client.get(
                "/api/v1/communications/project-batches/missing/draft-download"
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")
        self.assertEqual(
            response.json()["error"]["context"]["required_permission"],
            "manage_communications",
        )


if __name__ == "__main__":
    unittest.main()
