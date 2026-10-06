from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.errors import ApplicationConflictError, ApplicationValidationError
from app.application.task_catalog import TaskCatalogItem
from app.infrastructure.sql import (
    AppUser,
    Base,
    Resource,
    SqlTaskCatalogRepository,
    TaskCatalogEntry,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.models import TaskCatalogPreferredResourceAudit
from app.server import create_api_app
from tests.approval_test_support import TEST_ADMIN_USER_ID
from tests.http_test_auth import (
    TEST_ADMIN_AUTH_RESOLVER,
    TEST_PROJECT_MANAGER_AUTH_RESOLVER,
)


class TaskPreferredResourceAdminTests(unittest.TestCase):
    def _database(self, directory: str) -> tuple[str, object]:
        path = Path(directory) / "preferred-resource.db"
        url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    AppUser(
                        id=TEST_ADMIN_USER_ID,
                        issuer="urn:resourceplanner:test",
                        subject="617a-admin",
                        display_name="617A Admin",
                        roles_json=json.dumps(["ADMIN"]),
                        active=True,
                    ),
                    Resource(
                        id="R-A",
                        name="Alice",
                        active=True,
                        erp_active=True,
                    ),
                    Resource(
                        id="R-B",
                        name="Bob",
                        active=True,
                        erp_active=True,
                    ),
                    Resource(
                        id="R-LOCAL-INACTIVE",
                        name="Inactive locale",
                        active=False,
                        erp_active=True,
                    ),
                    Resource(
                        id="R-ERP-INACTIVE",
                        name="Inactive ERP",
                        active=True,
                        erp_active=False,
                    ),
                    TaskCatalogEntry(
                        id="T-P1-210",
                        project_number="P-1",
                        task_code="210",
                        label="Automatisation P1",
                        active=True,
                    ),
                    TaskCatalogEntry(
                        id="T-P2-210",
                        project_number="P-2",
                        task_code="210",
                        label="Automatisation P2",
                        active=True,
                    ),
                ]
            )
        return url, factory

    def test_http_admin_sets_project_scoped_preference_with_cas_and_audit(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, factory = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_ADMIN_AUTH_RESOLVER,
            )
            with TestClient(app) as client:
                initial = client.get(
                    "/api/v1/task-catalog",
                    params={"project_number": "P-1"},
                )
                self.assertEqual(initial.status_code, 200, initial.text)
                self.assertIsNone(initial.json()[0]["preferred_resource_id"])
                self.assertEqual(initial.json()[0]["preferred_resource_version"], 1)

                assigned = client.patch(
                    "/api/v1/task-catalog/T-P1-210/preferred-resource",
                    json={"resource_id": "R-A", "expected_version": 1},
                )
                self.assertEqual(assigned.status_code, 200, assigned.text)
                self.assertEqual(
                    assigned.json(),
                    {
                        "task_catalog_item_id": "T-P1-210",
                        "preferred_resource_id": "R-A",
                        "version": 2,
                        "action": "TASK_PREFERRED_RESOURCE_SET",
                    },
                )

                second_project = client.patch(
                    "/api/v1/task-catalog/T-P2-210/preferred-resource",
                    json={"resource_id": "R-B", "expected_version": 1},
                )
                self.assertEqual(second_project.status_code, 200, second_project.text)

                stale = client.patch(
                    "/api/v1/task-catalog/T-P1-210/preferred-resource",
                    json={"resource_id": "R-B", "expected_version": 1},
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["error"]["code"],
                    "task_preferred_resource_version_conflict",
                )

                inactive_local = client.patch(
                    "/api/v1/task-catalog/T-P1-210/preferred-resource",
                    json={
                        "resource_id": "R-LOCAL-INACTIVE",
                        "expected_version": 2,
                    },
                )
                self.assertEqual(inactive_local.status_code, 422, inactive_local.text)
                self.assertEqual(
                    inactive_local.json()["error"]["code"],
                    "task_preferred_resource_inactive",
                )

                inactive_erp = client.patch(
                    "/api/v1/task-catalog/T-P1-210/preferred-resource",
                    json={
                        "resource_id": "R-ERP-INACTIVE",
                        "expected_version": 2,
                    },
                )
                self.assertEqual(inactive_erp.status_code, 422, inactive_erp.text)
                self.assertEqual(
                    inactive_erp.json()["error"]["code"],
                    "task_preferred_resource_erp_inactive",
                )

                cleared = client.patch(
                    "/api/v1/task-catalog/T-P1-210/preferred-resource",
                    json={"resource_id": None, "expected_version": 2},
                )
                self.assertEqual(cleared.status_code, 200, cleared.text)
                self.assertEqual(cleared.json()["version"], 3)
                self.assertIsNone(cleared.json()["preferred_resource_id"])

            with factory() as session:
                p1 = session.get(TaskCatalogEntry, "T-P1-210")
                p2 = session.get(TaskCatalogEntry, "T-P2-210")
                assert p1 is not None and p2 is not None
                self.assertIsNone(p1.preferred_resource_id)
                self.assertEqual(p1.preferred_resource_version, 3)
                self.assertEqual(p2.preferred_resource_id, "R-B")
                self.assertEqual(p2.preferred_resource_version, 2)
                audits = list(
                    session.scalars(
                        select(TaskCatalogPreferredResourceAudit)
                        .where(
                            TaskCatalogPreferredResourceAudit.task_catalog_item_id
                            == "T-P1-210"
                        )
                        .order_by(TaskCatalogPreferredResourceAudit.resulting_version)
                    ).all()
                )
                self.assertEqual(
                    [
                        (
                            row.action,
                            row.old_resource_id,
                            row.new_resource_id,
                            row.resulting_version,
                            row.actor_user_id,
                        )
                        for row in audits
                    ],
                    [
                        (
                            "TASK_PREFERRED_RESOURCE_SET",
                            None,
                            "R-A",
                            2,
                            TEST_ADMIN_USER_ID,
                        ),
                        (
                            "TASK_PREFERRED_RESOURCE_CLEARED",
                            "R-A",
                            None,
                            3,
                            TEST_ADMIN_USER_ID,
                        ),
                    ],
                )

    def test_sync_and_resource_deactivation_preserve_nomination(self) -> None:
        with TemporaryDirectory() as directory:
            _database_url, factory = self._database(directory)
            with factory.begin() as session:
                repository = SqlTaskCatalogRepository(session)
                result = repository.set_preferred_resource(
                    "T-P1-210",
                    "R-A",
                    actor_user_id=TEST_ADMIN_USER_ID,
                    expected_version=1,
                )
                self.assertEqual(result.version, 2)

            with factory.begin() as session:
                outcome = SqlTaskCatalogRepository(session).upsert(
                    TaskCatalogItem(
                        id="ignored-transport-id",
                        project_number="P-1",
                        code="210",
                        label="Automatisation synchronisée",
                        active=True,
                        erp_task_id="ERP-TASK-P1-210",
                        workforce_eligible=True,
                    )
                )
                self.assertEqual(outcome, "updated")
                resource = session.get(Resource, "R-A")
                assert resource is not None
                resource.active = False
                resource.erp_active = False

            with factory() as session:
                task = session.get(TaskCatalogEntry, "T-P1-210")
                assert task is not None
                self.assertEqual(task.preferred_resource_id, "R-A")
                self.assertEqual(task.preferred_resource_version, 2)
                self.assertEqual(task.label, "Automatisation synchronisée")

    def test_invalid_nomination_does_not_consume_version(self) -> None:
        with TemporaryDirectory() as directory:
            _database_url, factory = self._database(directory)
            with factory.begin() as session:
                repository = SqlTaskCatalogRepository(session)
                with self.assertRaises(ApplicationValidationError):
                    repository.set_preferred_resource(
                        "T-P1-210",
                        "R-LOCAL-INACTIVE",
                        actor_user_id=TEST_ADMIN_USER_ID,
                        expected_version=1,
                    )
            with factory() as session:
                task = session.get(TaskCatalogEntry, "T-P1-210")
                assert task is not None
                self.assertEqual(task.preferred_resource_version, 1)
                self.assertIsNone(task.preferred_resource_id)

    def test_task_catalog_mutation_requires_manage_resources(self) -> None:
        with TemporaryDirectory() as directory:
            database_url, _factory = self._database(directory)
            app = create_api_app(
                database_url,
                auth_resolver=TEST_PROJECT_MANAGER_AUTH_RESOLVER,
            )
            with TestClient(app) as client:
                response = client.patch(
                    "/api/v1/task-catalog/T-P1-210/preferred-resource",
                    json={"resource_id": "R-A", "expected_version": 1},
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"], "permission_denied")
                self.assertEqual(
                    response.json()["error"]["context"]["required_permission"],
                    "manage_resources",
                )


if __name__ == "__main__":
    unittest.main()
