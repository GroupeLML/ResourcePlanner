from __future__ import annotations

from datetime import date, time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.infrastructure.sql import (
    Base,
    BusinessContact,
    PlanningMutationState,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER


DAY = date(2026, 10, 6)


class OperationalResponsibilityApiTests(unittest.TestCase):
    def _database(self, directory: str) -> str:
        path = Path(directory) / "operational-responsibility-594d.db"
        url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            session.add_all(
                [
                    BusinessContact(
                        id="C-RESP",
                        display_name="Responsable API",
                        active=True,
                    ),
                    Project(
                        id="P1",
                        number="P-594D",
                        name="Projet 594D",
                    ),
                    Resource(
                        id="R1",
                        name="Alice",
                        active=True,
                    ),
                ]
            )
            session.flush()
            session.add(
                ResourceAvailabilityRule(
                    id="STD-R1",
                    resource_id="R1",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven,Sam,Dim",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                )
            )
            session.add(
                ResourceRequirement(
                    id="REQ1",
                    legacy_segment_id="SEG-594D",
                    project_id="P1",
                    workforce_request_id=None,
                    assigned_resource_id="R1",
                    start_date=DAY,
                    end_date=DAY,
                    planned_hours=8,
                    status="Planifié",
                    planning_type="Flexible",
                    confirmation="Confirmée",
                    origin="AD_HOC",
                )
            )
            session.flush()
            session.add(
                Shift(
                    id="SHIFT1",
                    legacy_allocation_id="ALLOC-594D",
                    resource_requirement_id="REQ1",
                    resource_id="R1",
                    work_date=DAY,
                    hours=8,
                    allocation_type="Flexible",
                    source="AUTO",
                    locked=False,
                    outside_standard_hours=False,
                )
            )
        engine.dispose()
        return url

    def test_project_segment_and_shift_api_follow_canonical_hierarchy_and_versions(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            app = create_api_app(url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                project = client.patch(
                    "/api/v1/projects/P-594D/operational-responsible",
                    headers={"Idempotency-Key": "594d-project"},
                    json={"contact_id": "C-RESP", "expected_version": 1},
                )
                self.assertEqual(project.status_code, 200, project.text)
                self.assertEqual(project.json()["project_override_version"], 2)

                project_read = client.get(
                    "/api/v1/projects/P-594D/operational-responsibility"
                )
                self.assertEqual(project_read.status_code, 200, project_read.text)
                self.assertEqual(project_read.json()["override_version"], 2)
                self.assertEqual(
                    project_read.json()["operational_responsible"]["source_type"],
                    "PROJECT_OVERRIDE",
                )

                segment = client.patch(
                    "/api/v1/segments/SEG-594D/operational-responsible",
                    headers={"Idempotency-Key": "594d-segment"},
                    json={
                        "contact_id": "C-RESP",
                        "expected_planning_version": 1,
                    },
                )
                self.assertEqual(segment.status_code, 200, segment.text)
                self.assertEqual(segment.json()["planning_version"], 2)

                segment_read = client.get(
                    "/api/v1/segments/SEG-594D/operational-responsibility"
                )
                self.assertEqual(segment_read.status_code, 200, segment_read.text)
                self.assertEqual(
                    segment_read.json()["operational_responsible"]["source_type"],
                    "RESOURCE_REQUIREMENT_OVERRIDE",
                )

                shift = client.patch(
                    "/api/v1/allocations/ALLOC-594D/operational-responsible",
                    headers={"Idempotency-Key": "594d-shift"},
                    json={
                        "contact_id": "C-RESP",
                        "expected_planning_version": 2,
                    },
                )
                self.assertEqual(shift.status_code, 200, shift.text)
                self.assertTrue(shift.json()["auto_source_converted"])
                self.assertEqual(shift.json()["planning_version"], 3)

                shift_read = client.get(
                    "/api/v1/allocations/ALLOC-594D/operational-responsibility"
                )
                self.assertEqual(shift_read.status_code, 200, shift_read.text)
                self.assertEqual(
                    shift_read.json()["operational_responsible"]["source_type"],
                    "SHIFT_OVERRIDE",
                )

                cleared = client.patch(
                    "/api/v1/allocations/ALLOC-594D/operational-responsible",
                    headers={"Idempotency-Key": "594d-shift-clear"},
                    json={
                        "contact_id": None,
                        "expected_planning_version": 3,
                    },
                )
                self.assertEqual(cleared.status_code, 200, cleared.text)
                self.assertEqual(cleared.json()["planning_version"], 4)

                inherited = client.get(
                    "/api/v1/allocations/ALLOC-594D/operational-responsibility"
                )
                self.assertEqual(inherited.status_code, 200, inherited.text)
                self.assertEqual(
                    inherited.json()["operational_responsible"]["source_type"],
                    "RESOURCE_REQUIREMENT_OVERRIDE",
                )

            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    persisted = session.get(Shift, "SHIFT1")
                    assert persisted is not None
                    self.assertEqual(persisted.source, "MANUAL")
                    self.assertTrue(persisted.locked)
                    self.assertIsNone(
                        persisted.operational_responsible_override_contact_id
                    )
                    version = session.scalar(
                        select(PlanningMutationState.version).where(
                            PlanningMutationState.id == "GLOBAL"
                        )
                    )
                    self.assertEqual(int(version or 0), 4)
            finally:
                engine.dispose()

    def test_stale_project_cas_is_reported_by_http_contract(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            app = create_api_app(url, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)
            with TestClient(app) as client:
                first = client.patch(
                    "/api/v1/projects/P-594D/operational-responsible",
                    headers={"Idempotency-Key": "594d-project-first"},
                    json={"contact_id": "C-RESP", "expected_version": 1},
                )
                stale = client.patch(
                    "/api/v1/projects/P-594D/operational-responsible",
                    headers={"Idempotency-Key": "594d-project-stale"},
                    json={"contact_id": None, "expected_version": 1},
                )

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(
                stale.json()["error"]["code"],
                "project_operational_responsibility_version_conflict",
            )


if __name__ == "__main__":
    unittest.main()
