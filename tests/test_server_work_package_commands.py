from __future__ import annotations

from functools import partial

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.infrastructure.sql import (
    Base,
    CommandIdempotencyReceipt,
    Project,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkPackage,
    WorkPackageAudit,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.server import create_api_app
from app.infrastructure.sql.identity_models import AppUser


from tests.approval_test_support import TEST_ADMIN_USER_ID
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class ServerWorkPackageCommandTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add_all(
            [
                Project(id="P1", number="P-1", name="Projet 1", status="Actif"),
                Project(id="P2", number="P-2", name="Projet 2", status="Actif"),
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
                    id="TASK-P1-210",
                    project_number="P-1",
                    task_code="210",
                    label="AUTOMATISATION",
                    status="Actif",
                    active=True,
                    workforce_eligible=True,
                ),
                TaskCatalogEntry(
                    id="TASK-P1-211",
                    project_number="P-1",
                    task_code="211",
                    label="MISE EN SERVICE",
                    status="Actif",
                    active=True,
                    workforce_eligible=True,
                ),
                TaskCatalogEntry(
                    id="TASK-P2-210",
                    project_number="P-2",
                    task_code="210",
                    label="AUTOMATISATION P2",
                    status="Actif",
                    active=True,
                    workforce_eligible=True,
                ),
                TaskCatalogEntry(
                    id="TASK-P2-999",
                    project_number="P-2",
                    task_code="999",
                    label="INELIGIBLE",
                    status="Actif",
                    active=True,
                    workforce_eligible=False,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                WorkPackage(
                    id="WP-LINKED",
                    project_id="P1",
                    code="DEV",
                    name="Développement",
                    start_date=date(2026, 9, 14),
                    end_date=date(2026, 9, 25),
                    status="planned",
                    legacy_effort_id="EFF-LINKED",
                ),
                WorkPackage(
                    id="WP-FREE",
                    project_id="P1",
                    code="PREP",
                    name="Préparation",
                    status="planned",
                    legacy_effort_id="EFF-FREE",
                ),
            ]
        )
        session.flush()
        session.add(
            WorkforceRequest(
                id="REQ-1",
                legacy_demand_number="DMO-2026-0001",
                project_id="P1",
                work_package_id="WP-LINKED",
                desired_start=date(2026, 9, 14),
                status="Brouillon",
            )
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="work-package-commands.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _count(database_url: str, model) -> int:
        engine = create_sql_engine(database_url)
        factory = create_session_factory(engine)
        try:
            with factory() as session:
                value = session.scalar(select(func.count()).select_from(model))
                return int(value or 0)
        finally:
            engine.dispose()

    def test_create_is_idempotent_and_read_model_reflects_result(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            body = {
                "project_number": "P-2",
                "task_catalog_item_id": "TASK-P2-210",
                "code": "MES",
                "name": "Architecture MES",
                "description": "Interfaces et modèle de données",
                "start_date": "2026-09-21",
                "end_date": "2026-10-02",
                "planned_hours": 40,
                "status": "planned",
            }
            headers = {"Idempotency-Key": "wp-create-1"}

            with TestClient(app, raise_server_exceptions=False) as client:
                first = client.post("/api/v1/work-packages", json=body, headers=headers)
                second = client.post("/api/v1/work-packages", json=body, headers=headers)
                rows = client.get(
                    "/api/v1/work-packages?project_number=P-2&active_only=false"
                )

            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(second.status_code, 201, second.text)
            self.assertEqual(second.json(), first.json())
            reference = first.json()["reference"]
            created = [row for row in rows.json() if row["reference"] == reference]
            self.assertEqual(len(created), 1, rows.text)
            self.assertEqual(created[0]["name"], "Architecture MES")
            self.assertEqual(created[0]["planned_hours"], 40.0)
            self.assertEqual(created[0]["task_catalog_item_id"], "TASK-P2-210")
            self.assertEqual(created[0]["task_code"], "210")
            self.assertEqual(created[0]["version"], 1)
            self.assertEqual(self._count(database_url, WorkPackage), 3)
            self.assertEqual(self._count(database_url, CommandIdempotencyReceipt), 1)

    def test_patch_updates_editable_fields_and_can_clear_optional_values(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.patch(
                    "/api/v1/work-packages/EFF-FREE",
                    json={
                        "expected_version": 1,
                        "code": None,
                        "name": "Préparation révisée",
                        "description": "Nouvelle portée",
                        "start_date": "2026-09-28",
                        "end_date": "2026-10-09",
                        "planned_hours": 24,
                        "status": "active",
                    },
                )
                rows = client.get(
                    "/api/v1/work-packages?project_number=P-1&active_only=false"
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json(),
                {"reference": "EFF-FREE", "action": "updated", "version": 2},
            )
            edited = next(row for row in rows.json() if row["reference"] == "EFF-FREE")
            self.assertIsNone(edited["code"])
            self.assertEqual(edited["name"], "Préparation révisée")
            self.assertEqual(edited["description"], "Nouvelle portée")
            self.assertEqual(edited["start_date"], "2026-09-28")
            self.assertEqual(edited["end_date"], "2026-10-09")
            self.assertEqual(edited["planned_hours"], 24.0)
            self.assertEqual(edited["status"], "active")

    def test_date_window_is_validated_by_application_layer(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    "/api/v1/work-packages",
                    json={
                        "project_number": "P-1",
                        "task_catalog_item_id": "TASK-P1-210",
                        "name": "Fenêtre invalide",
                        "start_date": "2026-10-10",
                        "end_date": "2026-10-01",
                    },
                )

            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(
                response.json()["error"]["code"],
                "work_package_date_window_invalid",
            )

    def test_linked_work_package_cannot_move_to_another_project(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.patch(
                    "/api/v1/work-packages/EFF-LINKED",
                    json={"expected_version": 1, "project_number": "P-2"},
                )

            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(
                response.json()["error"]["code"],
                "work_package_project_change_linked_demands",
            )

    def test_new_work_package_requires_valid_same_project_workforce_task(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            base = {
                "project_number": "P-2",
                "name": "Nouveau lot",
            }
            with TestClient(app, raise_server_exceptions=False) as client:
                missing = client.post(
                    "/api/v1/work-packages",
                    headers={"Idempotency-Key": "wp-task-missing"},
                    json=base,
                )
                wrong_project = client.post(
                    "/api/v1/work-packages",
                    headers={"Idempotency-Key": "wp-task-wrong-project"},
                    json={**base, "task_catalog_item_id": "TASK-P1-210"},
                )
                ineligible = client.post(
                    "/api/v1/work-packages",
                    headers={"Idempotency-Key": "wp-task-ineligible"},
                    json={**base, "task_catalog_item_id": "TASK-P2-999"},
                )

            self.assertEqual(missing.status_code, 422, missing.text)
            self.assertEqual(wrong_project.status_code, 422, wrong_project.text)
            self.assertEqual(
                wrong_project.json()["error"]["code"],
                "work_package_task_project_mismatch",
            )
            self.assertEqual(ineligible.status_code, 422, ineligible.text)
            self.assertEqual(
                ineligible.json()["error"]["code"],
                "work_package_task_ineligible",
            )

    def test_historical_regularization_then_in_use_task_change_is_blocked(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                regularized = client.patch(
                    "/api/v1/work-packages/EFF-LINKED",
                    json={
                        "expected_version": 1,
                        "task_catalog_item_id": "TASK-P1-210",
                    },
                )
                changed = client.patch(
                    "/api/v1/work-packages/EFF-LINKED",
                    json={
                        "expected_version": 2,
                        "task_catalog_item_id": "TASK-P1-211",
                    },
                )

            self.assertEqual(regularized.status_code, 200, regularized.text)
            self.assertEqual(regularized.json()["version"], 2)
            self.assertEqual(changed.status_code, 409, changed.text)
            self.assertEqual(
                changed.json()["error"]["code"],
                "work_package_task_change_in_use",
            )

    def test_stale_version_rolls_back_and_audit_is_atomic(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                first = client.patch(
                    "/api/v1/work-packages/EFF-FREE",
                    json={"expected_version": 1, "name": "Version 2"},
                )
                stale = client.patch(
                    "/api/v1/work-packages/EFF-FREE",
                    json={"expected_version": 1, "name": "Version périmée"},
                )
                rows = client.get(
                    "/api/v1/work-packages?project_number=P-1&active_only=false"
                )

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["version"], 2)
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(
                stale.json()["error"]["code"],
                "work_package_version_conflict",
            )
            row = next(item for item in rows.json() if item["reference"] == "EFF-FREE")
            self.assertEqual(row["name"], "Version 2")
            self.assertEqual(row["version"], 2)
            self.assertEqual(self._count(database_url, WorkPackageAudit), 1)

    def test_unlinked_work_package_can_move_to_another_project(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.patch(
                    "/api/v1/work-packages/EFF-FREE",
                    json={"expected_version": 1, "project_number": "P-2"},
                )
                rows = client.get(
                    "/api/v1/work-packages?project_number=P-2&active_only=false"
                )

            self.assertEqual(response.status_code, 200, response.text)
            moved = [row for row in rows.json() if row["reference"] == "EFF-FREE"]
            self.assertEqual(len(moved), 1, rows.text)
            self.assertEqual(moved[0]["project_number"], "P-2")


if __name__ == "__main__":
    unittest.main()
