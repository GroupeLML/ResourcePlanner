from __future__ import annotations

from functools import partial

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application import (
    ApplicationOperationError,
    ExternalEmployeeRecord,
    ExternalProjectRecord,
)
from app.infrastructure.acumatica import ODataProjectSource, ODataProjectSourceSettings
from app.infrastructure.sql import (
    Base,
    Project,
    Resource,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)
from app.performance_diagnostics import read_performance_samples
from app.server import create_api_app
from tests.sqlite_test_template import SqliteDatabaseTemplate


from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class StubProjectSource:
    def __init__(self, rows: list[ExternalProjectRecord] | None = None) -> None:
        self.rows = rows or [
            ExternalProjectRecord(
                external_id="ERP-1",
                number="P-100",
                name="Projet Acumatica",
                client="Client A",
                project_manager_name="Alice",
                status="Active",
            )
        ]

    def list_projects(self):
        return tuple(self.rows)


class StubEmployeeSource:
    def __init__(self) -> None:
        self.rows = [
            ExternalEmployeeRecord(
                external_id="EMP-100",
                display_name="Employé Acumatica",
                email="employee100" + chr(64) + "example.invalid",
                erp_status="Actif",
                erp_active=True,
                department_description="Automatisation",
                department_code="AUTO",
                employee_class="GENERAL",
                supervisor_external_id=None,
                telephone=None,
                branch_code="210",
                contact_id=900100,
            )
        ]

    def list_employees(self):
        return tuple(self.rows)


class StubProjectTaskSource:
    def fetch_project_snapshot(self, *, project_external_id: str, project_number: str):
        raise AssertionError("Le statut d'intégration ne doit pas lire RP_ProjectTasks.")


class FailingProjectSource:
    def list_projects(self):
        raise ApplicationOperationError(
            "Impossible de lire les projets depuis Acumatica.",
            code="acumatica_project_read_failed",
            context={
                "failure_kind": "timeout",
                "retryable": True,
                "protocol": "odata",
                "feed_path": "/oDATA/RP_Projects",
            },
        )


class ServerAcumaticaRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="acumatica.db",
            seed=lambda session: None,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    def test_unconfigured_integration_reports_status_and_returns_503_for_sync(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                status = client.get("/api/v1/integrations/acumatica")
                sync = client.post("/api/v1/integrations/acumatica/projects/sync")
                employee_sync = client.post("/api/v1/integrations/acumatica/employees/sync")

            self.assertEqual(status.status_code, 200)
            self.assertEqual(
                status.json(),
                {
                    "configured": False,
                    "project_tasks_configured": False,
                },
            )
            self.assertEqual(sync.status_code, 503)
            self.assertEqual(sync.json()["error"]["code"], "acumatica_not_configured")
            self.assertEqual(employee_sync.status_code, 503)
            self.assertEqual(
                employee_sync.json()["error"]["code"],
                "acumatica_employee_not_configured",
            )

    def test_configured_sync_persists_projects_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            source = StubProjectSource()
            performance_log = Path(directory) / "performance.jsonl"
            app = create_api_app(
                self._database(directory),
                project_source=source,
                acumatica_info={
                    "protocol": "odata",
                    "feed_path": "/oDATA/RP_Projects",
                },
                performance_log_path=performance_log,
            )
            with TestClient(app) as client:
                status = client.get("/api/v1/integrations/acumatica")
                first = client.post("/api/v1/integrations/acumatica/projects/sync")
                second = client.post("/api/v1/integrations/acumatica/projects/sync")
                projects = client.get("/api/v1/projects")

            self.assertEqual(status.status_code, 200)
            self.assertEqual(
                status.json(),
                {
                    "configured": True,
                    "project_tasks_configured": False,
                    "protocol": "odata",
                    "feed_path": "/oDATA/RP_Projects",
                },
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(
                first.json(),
                {"received": 1, "created": 1, "updated": 0, "unchanged": 0},
            )
            self.assertEqual(
                second.json(),
                {"received": 1, "created": 0, "updated": 0, "unchanged": 1},
            )
            self.assertEqual(projects.status_code, 200)
            self.assertEqual(projects.json()[0]["number"], "P-100")
            self.assertEqual(projects.json()[0]["erp_external_id"], "ERP-1")
            self.assertEqual(projects.json()[0]["project_manager"], "Alice")

            samples = read_performance_samples(path=performance_log, limit=20)
            sync_samples = [
                sample
                for sample in samples
                if sample.get("operation")
                == "http POST /api/v1/integrations/acumatica/projects/sync"
            ]
            self.assertEqual(len(sync_samples), 2)
            for sample in sync_samples:
                self.assertEqual(sample["external_call_count"], 1)
                self.assertEqual(sample["external_item_count"], 1)
                self.assertGreater(sample["db_query_count"], 0)
                self.assertGreaterEqual(sample["external_seconds"], 0.0)
                self.assertGreaterEqual(sample["compute_seconds"], 0.0)

    def test_project_task_source_sets_project_tasks_configured_status(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(
                self._database(directory),
                project_task_source=StubProjectTaskSource(),
            )
            with TestClient(app) as client:
                status = client.get("/api/v1/integrations/acumatica")

            self.assertEqual(status.status_code, 200)
            self.assertEqual(
                status.json(),
                {
                    "configured": True,
                    "project_tasks_configured": True,
                },
            )

    def test_employee_sync_requires_local_activation_and_respects_erp_state(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            source = StubEmployeeSource()
            app = create_api_app(database_url, employee_source=source)

            with TestClient(app) as client:
                first = client.post("/api/v1/integrations/acumatica/employees/sync")
                second = client.post("/api/v1/integrations/acumatica/employees/sync")
                hidden = client.get("/api/v1/resources")
                all_resources = client.get("/api/v1/resources?active_only=false")

                self.assertEqual(
                    first.json(),
                    {
                        "received": 1,
                        "created": 1,
                        "updated": 0,
                        "unchanged": 0,
                        "errors": 0,
                    },
                )
                self.assertEqual(
                    second.json(),
                    {
                        "received": 1,
                        "created": 0,
                        "updated": 0,
                        "unchanged": 1,
                        "errors": 0,
                    },
                )
                self.assertEqual(hidden.json(), [])
                imported = all_resources.json()[0]
                self.assertFalse(imported["active"])
                self.assertTrue(imported["erp_active"])
                self.assertEqual(imported["erp_status"], "Actif")
                self.assertEqual(imported["erp_department_code"], "AUTO")
                self.assertEqual(imported["erp_branch_code"], "210")

                activation = client.patch(
                    f"/api/v1/resources/{imported['id']}",
                    json={"active": True},
                )
                self.assertEqual(activation.status_code, 200, activation.text)
                visible = client.get("/api/v1/resources")
                self.assertEqual(len(visible.json()), 1)

                source.rows[0] = ExternalEmployeeRecord(
                    external_id="EMP-100",
                    display_name="Employé Acumatica",
                    email="employee100" + chr(64) + "example.invalid",
                    erp_status="Inactif",
                    erp_active=False,
                    department_description="Automatisation",
                    department_code="AUTO",
                    employee_class="GENERAL",
                    branch_code="210",
                    contact_id=900100,
                )
                changed = client.post("/api/v1/integrations/acumatica/employees/sync")
                self.assertEqual(changed.json()["updated"], 1)
                self.assertEqual(client.get("/api/v1/resources").json(), [])

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    row = session.scalar(
                        select(Resource).where(Resource.external_id == "EMP-100")
                    )
                    assert row is not None
                    self.assertTrue(row.active)
                    self.assertFalse(row.erp_active)
            finally:
                engine.dispose()

    def test_failed_external_read_keeps_external_metrics_coherent(self) -> None:
        with TemporaryDirectory() as directory:
            performance_log = Path(directory) / "performance.jsonl"
            app = create_api_app(
                self._database(directory),
                project_source=FailingProjectSource(),
                performance_log_path=performance_log,
            )
            with TestClient(app) as client:
                response = client.post("/api/v1/integrations/acumatica/projects/sync")

            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json()["error"]["code"], "acumatica_project_read_failed")
            self.assertEqual(
                response.json()["error"]["context"]["failure_kind"],
                "timeout",
            )

            samples = read_performance_samples(path=performance_log, limit=10)
            sample = next(
                item
                for item in samples
                if item.get("operation")
                == "http POST /api/v1/integrations/acumatica/projects/sync"
            )
            self.assertEqual(sample["status"], "500")
            self.assertEqual(sample["external_call_count"], 1)
            self.assertEqual(sample["external_item_count"], 0)
            self.assertGreaterEqual(sample["external_seconds"], 0.0)

    def test_invalid_late_feed_entry_does_not_apply_partial_acumatica_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with transactional_session(factory) as session:
                    session.add(
                        Project(
                            id="EXISTING",
                            erp_external_id="101",
                            number="P-EXISTING",
                            name="Nom local conservé",
                            status="Active",
                        )
                    )
            finally:
                engine.dispose()

            payload = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  <entry><content type="application/xml"><m:properties>
    <d:ProjectId m:type="Edm.Int32">101</d:ProjectId>
    <d:ProjectCode>P-EXISTING</d:ProjectCode>
    <d:ProjectName>Nom partiel a ne pas appliquer</d:ProjectName>
  </m:properties></content></entry>
  <entry><content type="application/xml"><m:properties>
    <d:ProjectId m:type="Edm.Int32">102</d:ProjectId>
    <d:ProjectCode>P-FIRST</d:ProjectCode>
  </m:properties></content></entry>
</feed>"""

            source = ODataProjectSource(
                ODataProjectSourceSettings(
                    base_url="https://erp.example.test/Instance",
                ),
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, content=payload)
                ),
            )
            app = create_api_app(database_url, project_source=source)
            with TestClient(app) as client:
                sync = client.post("/api/v1/integrations/acumatica/projects/sync")
                projects = client.get("/api/v1/projects")

            self.assertEqual(sync.status_code, 500)
            self.assertEqual(
                sync.json()["error"]["context"]["failure_kind"],
                "invalid_payload",
            )
            self.assertEqual(
                sync.json()["error"]["context"]["reason"],
                "required_field_missing",
            )
            self.assertEqual(len(projects.json()), 1)
            self.assertEqual(projects.json()[0]["number"], "P-EXISTING")
            self.assertEqual(projects.json()[0]["name"], "Nom local conservé")

    def test_failed_later_odata_page_does_not_apply_first_page(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with transactional_session(factory) as session:
                    session.add(
                        Project(
                            id="EXISTING",
                            erp_external_id="101",
                            number="P-EXISTING",
                            name="Nom local conservé",
                            status="Active",
                        )
                    )
            finally:
                engine.dispose()

            first_page = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  <entry><content type="application/xml"><m:properties>
    <d:ProjectId m:type="Edm.Int32">101</d:ProjectId>
    <d:ProjectCode>P-EXISTING</d:ProjectCode>
    <d:ProjectName>Nom distant non applique</d:ProjectName>
  </m:properties></content></entry>
</feed>"""

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.params.get("$skip") == "0":
                    return httpx.Response(200, content=first_page)
                return httpx.Response(503, text="upstream unavailable")

            source = ODataProjectSource(
                ODataProjectSourceSettings(
                    base_url="https://erp.example.test/Instance",
                    page_size=1,
                ),
                transport=httpx.MockTransport(handler),
            )
            app = create_api_app(database_url, project_source=source)
            with TestClient(app) as client:
                sync = client.post("/api/v1/integrations/acumatica/projects/sync")
                projects = client.get("/api/v1/projects")

            self.assertEqual(sync.status_code, 500)
            self.assertEqual(
                sync.json()["error"]["context"]["failure_kind"],
                "upstream_5xx",
            )
            self.assertEqual(len(projects.json()), 1)
            self.assertEqual(projects.json()[0]["number"], "P-EXISTING")
            self.assertEqual(projects.json()[0]["name"], "Nom local conservé")

    def test_mid_sync_conflict_rolls_back_every_project_written_by_that_pull(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with transactional_session(factory) as session:
                    session.add(
                        Project(
                            id="EXISTING",
                            erp_external_id="ERP-OLD",
                            number="P-CONFLICT",
                            name="Projet existant",
                            status="Active",
                        )
                    )
            finally:
                engine.dispose()

            source = StubProjectSource(
                [
                    ExternalProjectRecord(
                        external_id="ERP-FIRST",
                        number="P-FIRST",
                        name="Devrait être rollback",
                    ),
                    ExternalProjectRecord(
                        external_id="ERP-NEW",
                        number="P-CONFLICT",
                        name="Conflit",
                    ),
                ]
            )
            app = create_api_app(database_url, project_source=source)
            with TestClient(app) as client:
                sync = client.post("/api/v1/integrations/acumatica/projects/sync")
                projects = client.get("/api/v1/projects")

            self.assertEqual(sync.status_code, 409)
            self.assertEqual(
                sync.json()["error"]["code"],
                "project_sync_external_id_conflict",
            )
            numbers = {row["number"] for row in projects.json()}
            self.assertEqual(numbers, {"P-CONFLICT"})
            self.assertNotIn("P-FIRST", numbers)

            verification_engine = create_sql_engine(database_url)
            verification_factory = create_session_factory(verification_engine)
            try:
                with verification_factory() as session:
                    rows = session.scalars(select(Project)).all()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].erp_external_id, "ERP-OLD")
            finally:
                verification_engine.dispose()


if __name__ == "__main__":
    unittest.main()
