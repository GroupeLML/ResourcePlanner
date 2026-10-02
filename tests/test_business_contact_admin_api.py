from __future__ import annotations

from datetime import date
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import (
    Base,
    BusinessContact,
    Project,
    RequestLine,
    Resource,
    ResourceRequirement,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestHistory,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class BusinessContactAdminApiTests(unittest.TestCase):
    def _database(self, directory: str) -> str:
        path = Path(directory) / "business-contact-admin.db"
        url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    BusinessContact(
                        id="C-OLD",
                        display_name="Responsable approuvé",
                        phone="555-0100",
                    ),
                    Project(
                        id="P1",
                        number="P-1",
                        name="Projet 1",
                        project_manager_name="Chargé legacy",
                        project_manager_contact_id="C-OLD",
                    ),
                    Resource(
                        id="R1",
                        name="Ressource 1",
                        active=True,
                    ),
                    TaskCatalogEntry(
                        id="T1",
                        project_number="P-1",
                        task_code="210",
                        label="Automatisation",
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add(
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DMO-289D-1",
                    project_id="P1",
                    status="En planification",
                    aggregate_version=3,
                )
            )
            session.flush()
            session.add(
                RequestLine(
                    id="L1",
                    workforce_request_id="D1",
                    position=0,
                    task_catalog_item_id="T1",
                    erp_task_code="210",
                    erp_task_label="Automatisation",
                    proposed_resource_id="R1",
                    active=True,
                )
            )
            session.add(
                ResourceRequirement(
                    id="REQ1",
                    project_id="P1",
                    workforce_request_id="D1",
                    start_date=date(2026, 9, 21),
                    end_date=date(2026, 9, 21),
                    planned_hours=8,
                    origin="REQUEST",
                    approved_operational_responsible_override_contact_id="C-OLD",
                    approved_request_version=3,
                    approved_contact_context_status="CAPTURED",
                )
            )
        engine.dispose()
        return url

    def test_contact_crud_links_and_demand_override_reapproval(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/business-contacts",
                    json={
                        "display_name": "Sophie",
                        "phone": "555-0200",
                        "external_system": "RESOURCEPLANNER",
                        "external_entity": "EMPLOYEE",
                        "external_id": "EMP-SOPHIE",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                contact = created.json()
                contact_id = contact["id"]
                self.assertEqual(contact["version"], 1)

                updated = client.patch(
                    f"/api/v1/business-contacts/{contact_id}",
                    json={
                        "phone": "555-0201",
                        "expected_version": 1,
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)
                self.assertEqual(updated.json()["version"], 2)

                stale = client.patch(
                    f"/api/v1/business-contacts/{contact_id}",
                    json={
                        "phone": "555-0202",
                        "expected_version": 1,
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["error"]["code"],
                    "business_contact_version_conflict",
                )

                duplicate = client.post(
                    "/api/v1/business-contacts",
                    json={
                        "display_name": "Doublon",
                        "external_system": "RESOURCEPLANNER",
                        "external_entity": "EMPLOYEE",
                        "external_id": "EMP-SOPHIE",
                    },
                )
                self.assertEqual(duplicate.status_code, 409, duplicate.text)

                project = client.patch(
                    "/api/v1/projects/P-1/project-manager-contact",
                    json={"contact_id": contact_id},
                )
                self.assertEqual(project.status_code, 422, project.text)
                self.assertEqual(
                    project.json()["error"]["code"],
                    "project_manager_erp_authoritative",
                )

                task = client.patch(
                    "/api/v1/task-catalog/T1/business-contacts",
                    json={"operational_responsible_contact_id": contact_id},
                )
                self.assertEqual(task.status_code, 200, task.text)
                self.assertEqual(
                    task.json()["operational_responsible_contact_id"],
                    contact_id,
                )
                self.assertIsNone(task.json()["coordinator_contact_id"])

                task_coordinator = client.patch(
                    "/api/v1/task-catalog/T1/business-contacts",
                    json={"coordinator_contact_id": contact_id},
                )
                self.assertEqual(task_coordinator.status_code, 200)
                self.assertEqual(
                    task_coordinator.json()["operational_responsible_contact_id"],
                    contact_id,
                )
                self.assertEqual(
                    task_coordinator.json()["coordinator_contact_id"],
                    contact_id,
                )

                resource = client.patch(
                    "/api/v1/resources/R1/coordinator-contact",
                    json={"contact_id": contact_id},
                )
                self.assertEqual(resource.status_code, 200, resource.text)

                resolution = client.get(
                    "/api/v1/request-lines/L1/contact-resolution"
                )
                self.assertEqual(resolution.status_code, 200, resolution.text)
                self.assertEqual(
                    resolution.json()["operational_responsible"]["contact_id"],
                    contact_id,
                )
                self.assertEqual(
                    resolution.json()["operational_responsible"]["source_type"],
                    "TASK_RESPONSIBLE",
                )
                self.assertEqual(
                    resolution.json()["coordinator"]["contact_id"],
                    contact_id,
                )
                self.assertEqual(
                    resolution.json()["coordinator"]["source_type"],
                    "RESOURCE_COORDINATOR",
                )

                cleared_task_responsible = client.patch(
                    "/api/v1/task-catalog/T1/business-contacts",
                    json={"operational_responsible_contact_id": None},
                )
                self.assertEqual(cleared_task_responsible.status_code, 200)
                self.assertIsNone(
                    cleared_task_responsible.json()[
                        "operational_responsible_contact_id"
                    ]
                )
                self.assertEqual(
                    cleared_task_responsible.json()["coordinator_contact_id"],
                    contact_id,
                )

                resource_link = client.get(
                    "/api/v1/resources/R1/business-contacts"
                )
                self.assertEqual(resource_link.status_code, 200, resource_link.text)
                self.assertEqual(
                    resource_link.json()["coordinator_contact_id"],
                    contact_id,
                )

                demand = client.patch(
                    "/api/v1/demands/DMO-289D-1/operational-responsible",
                    json={
                        "contact_id": contact_id,
                        "expected_version": 3,
                    },
                )
                self.assertEqual(demand.status_code, 200, demand.text)
                payload = demand.json()
                self.assertTrue(payload["changed"])
                self.assertTrue(payload["reapproval_required"])
                self.assertEqual(payload["status"], "Soumise")
                self.assertEqual(payload["version"], 4)
                self.assertEqual(payload["contact_id"], contact_id)

                stale_demand = client.patch(
                    "/api/v1/demands/DMO-289D-1/operational-responsible",
                    json={
                        "contact_id": None,
                        "expected_version": 3,
                    },
                )
                self.assertEqual(stale_demand.status_code, 409, stale_demand.text)

                link = client.get(
                    "/api/v1/demands/DMO-289D-1/business-contacts"
                )
                self.assertEqual(link.status_code, 200, link.text)
                self.assertEqual(
                    link.json()["operational_responsible_override_contact_id"],
                    contact_id,
                )
                self.assertEqual(link.json()["aggregate_version"], 4)

                tasks = client.get(
                    "/api/v1/task-catalog",
                    params={"project_number": "P-1"},
                )
                self.assertEqual(tasks.status_code, 200, tasks.text)
                self.assertEqual(tasks.json()[0]["id"], "T1")
                self.assertIsNone(
                    tasks.json()[0]["operational_responsible_contact_id"]
                )
                self.assertEqual(
                    tasks.json()[0]["coordinator_contact_id"],
                    contact_id,
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    requirement = session.get(ResourceRequirement, "REQ1")
                    assert requirement is not None
                    # The old approved plan must remain tied to its approved context
                    # until the demand is explicitly reapproved.
                    self.assertEqual(
                        requirement.approved_operational_responsible_override_contact_id,
                        "C-OLD",
                    )
                    self.assertEqual(requirement.approved_request_version, 3)
                    self.assertEqual(
                        requirement.approved_contact_context_status,
                        "CAPTURED",
                    )
                    request = session.get(WorkforceRequest, "D1")
                    assert request is not None
                    self.assertEqual(request.status, "Soumise")
                    self.assertEqual(request.aggregate_version, 4)
                    history = session.scalars(
                        select(WorkforceRequestHistory).where(
                            WorkforceRequestHistory.workforce_request_id == "D1"
                        )
                    ).all()
                    self.assertEqual(len(history), 1)
                    self.assertEqual(
                        history[0].action,
                        "Modification responsable opérationnel",
                    )
            finally:
                engine.dispose()

    def test_legacy_project_manager_setter_is_refused_without_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app) as client:
                refused = client.patch(
                    "/api/v1/projects/P-1/project-manager-contact",
                    json={"contact_id": "C-OLD"},
                )
                self.assertEqual(refused.status_code, 422, refused.text)
                self.assertEqual(
                    refused.json()["error"]["code"],
                    "project_manager_erp_authoritative",
                )

                current = client.get(
                    "/api/v1/projects/P-1/business-contacts"
                )
                self.assertEqual(current.status_code, 200)
                self.assertIsNone(
                    current.json()["project_manager_contact_id"]
                )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    project = session.get(Project, "P1")
                    assert project is not None
                    self.assertEqual(project.project_manager_contact_id, "C-OLD")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
