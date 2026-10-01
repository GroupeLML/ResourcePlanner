from __future__ import annotations

from decimal import Decimal
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application import (
    TaskCatalogItem,
    TaskCatalogPortfolioSnapshot,
    TaskCatalogProjectSnapshot,
)
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


class MultiProjectTaskSource:
    def __init__(self, snapshots: dict[str, TaskCatalogProjectSnapshot]) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[str, str]] = []

    def fetch_project_snapshot(
        self,
        *,
        project_external_id: str,
        project_number: str,
    ) -> TaskCatalogProjectSnapshot:
        self.calls.append((project_external_id, project_number))
        return self.snapshots[project_number]


class BatchProjectTaskSource(MultiProjectTaskSource):
    def __init__(
        self,
        snapshots: dict[str, TaskCatalogProjectSnapshot],
        *,
        source_pages: int = 3,
        source_rows: int = 5,
    ) -> None:
        super().__init__(snapshots)
        self.source_pages = source_pages
        self.source_rows = source_rows
        self.batch_calls: list[tuple[tuple[str, str], ...]] = []

    def fetch_project_snapshots(
        self,
        *,
        project_targets: tuple[tuple[str, str], ...],
    ) -> TaskCatalogPortfolioSnapshot:
        targets = tuple(project_targets)
        self.batch_calls.append(targets)
        return TaskCatalogPortfolioSnapshot(
            source_rows=self.source_rows,
            source_pages=self.source_pages,
            snapshots=tuple(
                self.snapshots[project_number]
                for _project_external_id, project_number in targets
            ),
        )


class BlockingBatchProjectTaskSource(BatchProjectTaskSource):
    def __init__(self, snapshots: dict[str, TaskCatalogProjectSnapshot]) -> None:
        super().__init__(snapshots)
        self.started = Event()
        self.release = Event()

    def fetch_project_snapshots(
        self,
        *,
        project_targets: tuple[tuple[str, str], ...],
    ) -> TaskCatalogPortfolioSnapshot:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test release timeout")
        return super().fetch_project_snapshots(project_targets=project_targets)


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
                        id="PROJECT-ERP-2",
                        erp_external_id="5470",
                        number="5119",
                        name="Projet ERP 2",
                        status="Actif",
                    ),
                    Project(
                        id="PROJECT-LOCAL",
                        number="LOCAL-1",
                        name="Projet local",
                        status="Actif",
                    ),
                    Project(
                        id="PROJECT-INACTIVE",
                        erp_external_id="9999",
                        number="OLD-1",
                        name="Projet terminé",
                        status="Terminé",
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

    def test_global_sync_requires_admin_sync_permission(self) -> None:
        source = MultiProjectTaskSource({"5118": self._snapshot()})
        app = create_api_app(
            self.database_url,
            auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            project_task_source=source,
        )
        path = "/api/v1/integrations/acumatica/projects/tasks/sync"
        self.assertEqual(required_permission("POST", path), PERMISSION_SYNC_PROJECTS)

        with TestClient(app) as client:
            response = client.post(path)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")
        self.assertEqual(source.calls, [])

    @staticmethod
    def _global_snapshots() -> dict[str, TaskCatalogProjectSnapshot]:
        return {
            "5118": TaskCatalogProjectSnapshot(
                project_number="5118",
                source_rows=2,
                rejected_rows=0,
                items=(
                    TaskCatalogItem(
                        project_number="5118",
                        code="216",
                        label="Programmation projet 1",
                        status="Actif",
                        active=True,
                        erp_task_id="9001",
                        account_group="DEPMO",
                        budget_amount_cad=Decimal("1000.00"),
                    ),
                    TaskCatalogItem(
                        project_number="5118",
                        code="999",
                        label="DEPMO sans standard TaskCD",
                        status="Actif",
                        active=True,
                        erp_task_id="9099",
                        account_group="DEPMO",
                        budget_amount_cad=Decimal("500.00"),
                    ),
                ),
            ),
            "5119": TaskCatalogProjectSnapshot(
                project_number="5119",
                source_rows=1,
                rejected_rows=0,
                items=(
                    TaskCatalogItem(
                        project_number="5119",
                        code="216",
                        label="Programmation projet 2",
                        status="Actif",
                        active=True,
                        erp_task_id="9002",
                        account_group="DEPMO",
                        budget_amount_cad=Decimal("625.00"),
                    ),
                ),
            ),
        }

    def test_global_sync_orchestrates_active_erp_projects_and_replays_idempotently(self) -> None:
        source = MultiProjectTaskSource(self._global_snapshots())
        app = create_api_app(
            self.database_url,
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            project_task_source=source,
        )
        path = "/api/v1/integrations/acumatica/projects/tasks/sync"

        with TestClient(app) as client:
            first = client.post(path)
            second = client.post(path)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_body = first.json()
        second_body = second.json()

        self.assertEqual(first_body["projects_inspected"], 4)
        self.assertEqual(first_body["projects_synchronized"], 2)
        self.assertEqual(first_body["projects_ignored"], 1)
        self.assertEqual(first_body["projects_rejected"], 1)
        self.assertEqual(first_body["source_rows_received"], 3)
        self.assertEqual(first_body["source_rows_rejected"], 0)
        self.assertEqual(first_body["tasks_received"], 3)
        self.assertEqual(first_body["tasks_created"], 2)
        self.assertEqual(first_body["tasks_updated"], 0)
        self.assertEqual(first_body["tasks_unchanged"], 0)
        self.assertEqual(first_body["tasks_rejected"], 1)

        self.assertEqual(second_body["tasks_created"], 0)
        self.assertEqual(second_body["tasks_updated"], 0)
        self.assertEqual(second_body["tasks_unchanged"], 2)
        self.assertEqual(second_body["tasks_rejected"], 1)
        self.assertEqual(
            source.calls,
            [
                ("5469", "5118"),
                ("5470", "5119"),
                ("5469", "5118"),
                ("5470", "5119"),
            ],
        )

        outcomes = {
            row["project_number"]: row
            for row in first_body["project_results"]
        }
        self.assertEqual(outcomes["OLD-1"]["status"], "ignored")
        self.assertEqual(outcomes["OLD-1"]["reason_code"], "project_inactive")
        self.assertEqual(outcomes["LOCAL-1"]["status"], "rejected")
        self.assertEqual(
            outcomes["LOCAL-1"]["error_code"],
            "task_catalog_project_external_id_required",
        )

        engine = create_sql_engine(self.database_url)
        factory = create_session_factory(engine)
        with factory() as session:
            rows = session.scalars(
                select(TaskCatalogEntry).order_by(TaskCatalogEntry.project_number)
            ).all()
            self.assertEqual(
                [(row.project_number, row.task_code, row.erp_task_id) for row in rows],
                [
                    ("5118", "216", "9001"),
                    ("5119", "216", "9002"),
                ],
            )
            self.assertTrue(all(row.account_group == "DEPMO" for row in rows))
            self.assertTrue(all(row.workforce_eligible for row in rows))
        engine.dispose()

    def test_global_sync_uses_one_batched_source_read_for_active_erp_projects(self) -> None:
        source = BatchProjectTaskSource(self._global_snapshots())
        app = create_api_app(
            self.database_url,
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            project_task_source=source,
        )

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/integrations/acumatica/projects/tasks/sync"
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            source.batch_calls,
            [(("5469", "5118"), ("5470", "5119"))],
        )
        self.assertEqual(source.calls, [])
        self.assertEqual(body["source_requests"], 3)
        self.assertEqual(body["source_rows_scanned"], 5)
        self.assertIsInstance(body["source_read_duration_ms"], int)
        self.assertIsInstance(body["duration_ms"], int)
        self.assertEqual(body["projects_inspected"], 4)
        self.assertEqual(body["projects_synchronized"], 2)
        self.assertEqual(body["projects_ignored"], 1)
        self.assertEqual(body["projects_rejected"], 1)
        self.assertEqual(body["tasks_created"], 2)
        self.assertEqual(body["tasks_rejected"], 1)

    def test_global_sync_rejects_a_concurrent_second_launch(self) -> None:
        source = BlockingBatchProjectTaskSource(self._global_snapshots())
        app = create_api_app(
            self.database_url,
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            project_task_source=source,
        )
        path = "/api/v1/integrations/acumatica/projects/tasks/sync"
        first_result: dict[str, object] = {}

        def run_first() -> None:
            with TestClient(app) as client:
                first_result["response"] = client.post(path)

        first_thread = Thread(target=run_first)
        first_thread.start()
        self.assertTrue(source.started.wait(timeout=2))
        try:
            with TestClient(app) as client:
                second = client.post(path)
        finally:
            source.release.set()
            first_thread.join(timeout=5)

        self.assertFalse(first_thread.is_alive())
        first = first_result["response"]
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(
            second.json()["error"]["code"],
            "acumatica_project_task_sync_in_progress",
        )
        self.assertEqual(len(source.batch_calls), 1)

    def test_global_sync_rolls_back_only_the_failed_project_transaction(self) -> None:
        snapshots = self._global_snapshots()
        snapshots["5118"] = TaskCatalogProjectSnapshot(
            project_number="5118",
            source_rows=2,
            rejected_rows=0,
            items=(
                TaskCatalogItem(
                    project_number="5118",
                    code="216",
                    label="Première ligne avant échec",
                    status="Actif",
                    active=True,
                    erp_task_id="FAIL-DUPLICATE",
                    account_group="DEPMO",
                    budget_amount_cad=Decimal("100.00"),
                ),
                TaskCatalogItem(
                    project_number="5118",
                    code="216",
                    label="Même identité ERP en double",
                    status="Actif",
                    active=True,
                    erp_task_id="FAIL-DUPLICATE",
                    account_group="DEPMO",
                    budget_amount_cad=Decimal("200.00"),
                ),
            ),
        )
        source = MultiProjectTaskSource(snapshots)
        app = create_api_app(
            self.database_url,
            auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            project_task_source=source,
        )

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/integrations/acumatica/projects/tasks/sync"
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["projects_synchronized"], 1)
        self.assertEqual(body["projects_rejected"], 2)
        outcomes = {
            row["project_number"]: row
            for row in body["project_results"]
        }
        self.assertEqual(outcomes["5118"]["status"], "rejected")
        self.assertEqual(
            outcomes["5118"]["error_code"],
            "task_catalog_duplicate_key",
        )
        self.assertEqual(outcomes["5119"]["status"], "synchronized")

        engine = create_sql_engine(self.database_url)
        factory = create_session_factory(engine)
        with factory() as session:
            failed_rows = session.scalars(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == "5118"
                )
            ).all()
            successful_rows = session.scalars(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.project_number == "5119"
                )
            ).all()
            self.assertEqual(failed_rows, [])
            self.assertEqual(len(successful_rows), 1)
            self.assertEqual(successful_rows[0].erp_task_id, "9002")
        engine.dispose()

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
