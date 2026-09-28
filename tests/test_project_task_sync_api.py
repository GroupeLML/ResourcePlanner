from __future__ import annotations

from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application import TaskCatalogItem, TaskCatalogProjectSnapshot
from app.application.security import PERMISSION_SYNC_PROJECTS
from app.infrastructure.sql import (
    Base,
    Project,
    ResourceClassConfig,
    TaskCatalogEntry,
    TaskClassStandard,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.security import required_permission
from tests.http_test_auth import (
    TEST_ADMIN_AUTH_RESOLVER,
    TEST_PROJECT_MANAGER_AUTH_RESOLVER,
)


class StubProjectTaskSource:
    def __init__(self, snapshot: TaskCatalogProjectSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, str]] = []

    def fetch_project_snapshot(
        self,
        *,
        project_external_id: str,
        project_number: str,
    ) -> TaskCatalogProjectSnapshot:
        self.calls.append((project_external_id, project_number))
        return self.snapshot


class ProjectTaskSyncApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        database = f"{self.directory.name}/project-task-sync.db"
        self.database_url = f"sqlite+pysqlite:///{database}"
        engine = create_sql_engine(self.database_url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    Project(
                        id="PROJECT-ERP",
                        erp_external_id="5469",
                        number="5118",
                        name="Projet ERP",
                        status="Actif",
                    ),
                    Project(
                        id="PROJECT-LOCAL",
                        number="LOCAL-1",
                        name="Projet local",
                        status="Actif",
                    ),
                    ResourceClassConfig(
                        code="PROGRAMMEUR",
                        label="Programmeur",
                        average_hourly_cost_cad=Decimal("125.0000"),
                        active=True,
                        version=1,
                    ),
                    TaskClassStandard(
                        task_code="216",
                        resource_class_code="PROGRAMMEUR",
                        active=True,
                        version=1,
                    ),
                ]
            )
        engine.dispose()

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def _snapshot() -> TaskCatalogProjectSnapshot:
        return TaskCatalogProjectSnapshot(
            project_number="5118",
            source_rows=1,
            rejected_rows=0,
            items=(
                TaskCatalogItem(
                    project_number="5118",
                    code="216",
                    label="Programmation",
                    status="Actif",
                    active=True,
                    erp_task_id="9001",
                    account_group="DEPMO",
                    budget_amount_cad=Decimal("1000.0000000000"),
                    budget_actual_cad=Decimal("250.0000000000"),
                ),
            ),
        )

    def test_dynamic_sync_path_requires_sync_projects_permission(self) -> None:
        path = "/api/v1/integrations/acumatica/projects/PROJECT-ERP/tasks/sync"
        self.assertEqual(required_permission("POST", path), PERMISSION_SYNC_PROJECTS)
        self.assertNotEqual(required_permission("POST", path), "__unassigned_mutation__")

        source = StubProjectTaskSource(self._snapshot())
        app = create_api_app(
            self.database_url,
            auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            project_task_source=source,
        )
        with TestClient(app) as client:
            response = client.post(path)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")
        self.assertEqual(source.calls, [])

    def test_project_errors_fail_closed_before_external_read(self) -> None:
        source = StubProjectTaskSource(self._snapshot())
        app = create_api_app(
            self.database_url,
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            project_task_source=source,
        )
        with TestClient(app) as client:
            missing = client.post(
                "/api/v1/integrations/acumatica/projects/MISSING/tasks/sync"
            )
            local = client.post(
                "/api/v1/integrations/acumatica/projects/PROJECT-LOCAL/tasks/sync"
            )

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "project_not_found")
        self.assertEqual(local.status_code, 422)
        self.assertEqual(
            local.json()["error"]["code"],
            "task_catalog_project_external_id_required",
        )
        self.assertEqual(source.calls, [])

    def test_configured_project_without_task_source_returns_503(self) -> None:
        app = create_api_app(
            self.database_url,
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/integrations/acumatica/projects/PROJECT-ERP/tasks/sync"
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "acumatica_project_task_not_configured",
        )

    def test_sync_uses_erp_identity_persists_workforce_projection_and_replays(self) -> None:
        source = StubProjectTaskSource(self._snapshot())
        app = create_api_app(
            self.database_url,
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            project_task_source=source,
        )
        path = "/api/v1/integrations/acumatica/projects/PROJECT-ERP/tasks/sync"

        with TestClient(app) as client:
            first = client.post(path)
            second = client.post(path)
            metadata = client.get(
                "/api/v1/integrations/acumatica/projects/PROJECT-ERP/tasks/sync-metadata"
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_body = first.json()
        second_body = second.json()
        self.assertEqual(first_body["project_number"], "5118")
        self.assertEqual(first_body["source_rows"], 1)
        self.assertEqual(first_body["task_count"], 1)
        self.assertEqual(first_body["rejected_rows"], 0)
        self.assertEqual((first_body["created"], first_body["updated"]), (1, 0))
        self.assertEqual(
            (second_body["created"], second_body["updated"], second_body["unchanged"]),
            (0, 0, 1),
        )
        self.assertEqual(source.calls, [("5469", "5118"), ("5469", "5118")])

        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.json()["project_number"], "5118")
        self.assertEqual(metadata.json()["task_count"], 1)
        self.assertIsNone(metadata.json()["last_error_code"])

        engine = create_sql_engine(self.database_url)
        factory = create_session_factory(engine)
        with factory() as session:
            rows = session.scalars(
                select(TaskCatalogEntry).where(TaskCatalogEntry.erp_task_id == "9001")
            ).all()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.project_number, "5118")
            self.assertEqual(row.budget_amount_cad, Decimal("1000.0000000000"))
            self.assertEqual(row.resource_class_code, "PROGRAMMEUR")
            self.assertEqual(row.average_hourly_cost_cad, Decimal("125.0000"))
            self.assertEqual(row.budget_hours, Decimal("8.000000000000000000"))
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
