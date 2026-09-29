from __future__ import annotations

from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import (
    Base,
    Project,
    RequestLine,
    TaskCatalogEntry,
    WorkPackage,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.identity_models import AppUser
from app.server import create_api_app
from tests.approval_test_support import TEST_ADMIN_USER_ID
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class WorkPackageRequestLineInvariantTests(unittest.TestCase):
    @staticmethod
    def _database(directory: str) -> tuple[str, object]:
        database = Path(directory) / "wp-request-lines.db"
        url = f"sqlite+pysqlite:///{database.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    Project(id="P1", number="P-1", name="Projet 1", status="Actif"),
                    AppUser(
                        id=TEST_ADMIN_USER_ID,
                        issuer="urn:resourceplanner:test",
                        subject="explicit-test-admin",
                        display_name="Administrateur de test explicite",
                        email=None,
                        roles_json='["ADMIN"]',
                        active=True,
                    ),
                    TaskCatalogEntry(
                        id="TASK-210",
                        project_number="P-1",
                        task_code="210",
                        label="AUTOMATISATION",
                        status="Actif",
                        active=True,
                        workforce_eligible=True,
                    ),
                    TaskCatalogEntry(
                        id="TASK-211",
                        project_number="P-1",
                        task_code="211",
                        label="MISE EN SERVICE",
                        status="Actif",
                        active=True,
                        workforce_eligible=True,
                    ),
                    WorkPackage(
                        id="WP-CLASS",
                        project_id="P1",
                        task_catalog_item_id="TASK-210",
                        version=1,
                        name="Lot classé",
                        status="planned",
                    ),
                    WorkPackage(
                        id="WP-HIST",
                        project_id="P1",
                        task_catalog_item_id=None,
                        version=1,
                        name="Lot historique",
                        status="planned",
                    ),
                ]
            )
        engine.dispose()
        return url, factory

    @staticmethod
    def _lines(database_url: str) -> list[RequestLine]:
        engine = create_sql_engine(database_url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                return list(session.scalars(select(RequestLine).order_by(RequestLine.position)).all())
        finally:
            engine.dispose()

    def test_multiline_work_package_only_derives_stable_task_id(self) -> None:
        with TemporaryDirectory() as directory:
            url, _ = self._database(directory)
            app = create_api_app(url)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/demands",
                    headers={"Idempotency-Key": "wp-line-derived"},
                    json={
                        "project_number": "P-1",
                        "lines": [
                            {
                                "work_package_ref": "WP-CLASS",
                                "desired_start": "2026-10-05",
                                "estimated_hours": 8,
                            }
                        ],
                    },
                )
            self.assertEqual(response.status_code, 201, response.text)
            line = self._lines(url)[0]
            self.assertEqual(line.work_package_id, "WP-CLASS")
            self.assertEqual(line.task_catalog_item_id, "TASK-210")
            self.assertEqual(line.erp_task_code, "210")

    def test_multiline_same_task_is_accepted_and_contradiction_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            url, _ = self._database(directory)
            app = create_api_app(url)
            with TestClient(app, raise_server_exceptions=False) as client:
                same = client.post(
                    "/api/v1/demands",
                    headers={"Idempotency-Key": "wp-line-same"},
                    json={
                        "project_number": "P-1",
                        "lines": [
                            {
                                "work_package_ref": "WP-CLASS",
                                "task_code": "210",
                                "desired_start": "2026-10-05",
                                "estimated_hours": 8,
                            }
                        ],
                    },
                )
                conflicting = client.post(
                    "/api/v1/demands",
                    headers={"Idempotency-Key": "wp-line-conflict"},
                    json={
                        "project_number": "P-1",
                        "lines": [
                            {
                                "work_package_ref": "WP-CLASS",
                                "task_code": "211",
                                "desired_start": "2026-10-05",
                                "estimated_hours": 8,
                            }
                        ],
                    },
                )

            self.assertEqual(same.status_code, 201, same.text)
            self.assertEqual(conflicting.status_code, 422, conflicting.text)
            self.assertEqual(
                conflicting.json()["error"]["code"],
                "request_line_work_package_task_conflict",
            )

    def test_legacy_simple_path_derives_task_from_classified_work_package(self) -> None:
        with TemporaryDirectory() as directory:
            url, _ = self._database(directory)
            app = create_api_app(url)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/demands",
                    headers={"Idempotency-Key": "wp-legacy-derived"},
                    json={
                        "project_number": "P-1",
                        "desired_start": "2026-10-05",
                        "work_package_ref": "WP-CLASS",
                        "estimated_hours": 8,
                    },
                )
            self.assertEqual(response.status_code, 201, response.text)
            line = self._lines(url)[0]
            self.assertEqual(line.work_package_id, "WP-CLASS")
            self.assertEqual(line.task_catalog_item_id, "TASK-210")
            self.assertEqual(line.erp_task_code, "210")

    def test_historical_unclassified_work_package_does_not_invent_task(self) -> None:
        with TemporaryDirectory() as directory:
            url, _ = self._database(directory)
            app = create_api_app(url)
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/demands",
                    headers={"Idempotency-Key": "wp-historical-no-task"},
                    json={
                        "project_number": "P-1",
                        "desired_start": "2026-10-05",
                        "work_package_ref": "WP-HIST",
                        "estimated_hours": 8,
                    },
                )
            self.assertEqual(response.status_code, 201, response.text)
            line = self._lines(url)[0]
            self.assertEqual(line.work_package_id, "WP-HIST")
            self.assertIsNone(line.task_catalog_item_id)
            self.assertIsNone(line.erp_task_code)


if __name__ == "__main__":
    unittest.main()
