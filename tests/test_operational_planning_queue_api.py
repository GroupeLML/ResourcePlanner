from __future__ import annotations

from functools import partial

from datetime import date, time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.infrastructure.sql import (
    Base,
    Competency,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceCompetency,
    ResourceRequirement,
    ResourceRequirementCompetency,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestCompetency,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app


from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class OperationalPlanningQueueApiTests(unittest.TestCase):
    def _database(self, directory: str) -> str:
        path = Path(directory) / "planning-queue.db"
        url = f"sqlite+pysqlite:///{path.as_posix()}"
        engine = create_sql_engine(url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)

        with factory.begin() as session:
            session.add(
                Project(
                    id="P-273",
                    number="P-273",
                    name="Projet file opérationnelle",
                    project_manager_name="CP Test",
                    status="active",
                )
            )
            session.add(
                Competency(
                    id="C-PLC",
                    name="PLC",
                    description="Programmation automate",
                    active=True,
                    sort_order=10,
                )
            )
            session.add_all(
                [
                    Resource(
                        id="R-ALICE",
                        name="Alice",
                        resource_class="Programmation",
                        competencies="PLC",
                        active=True,
                        sort_order=10,
                    ),
                    Resource(
                        id="R-BOB",
                        name="Bob",
                        resource_class="Installation",
                        competencies="Installation",
                        active=True,
                        sort_order=20,
                    ),
                ]
            )
            session.flush()
            session.add(
                TaskCatalogEntry(
                    id="T-APPROVED",
                    project_number="P-273",
                    task_code="310",
                    label="Programmation",
                    active=True,
                    workforce_eligible=True,
                    preferred_resource_id="R-ALICE",
                )
            )
            session.add(
                ResourceCompetency(resource_id="R-ALICE", competency_id="C-PLC")
            )
            for resource_id in ("R-ALICE", "R-BOB"):
                session.add(
                    ResourceAvailabilityRule(
                        id=f"SCH-{resource_id}",
                        resource_id=resource_id,
                        availability_type="Horaire standard",
                        start_date=date(2026, 1, 1),
                        end_date=date(2027, 12, 31),
                        weekdays="Lun,Mar,Mer,Jeu,Ven",
                        start_time=time(7, 0),
                        end_time=time(15, 0),
                        active=True,
                    )
                )

            submitted = WorkforceRequest(
                id="WR-SUBMITTED",
                legacy_demand_number="DMO-2026-0273",
                project_id="P-273",
                erp_task_code="253",
                erp_task_label="Installation",
                requester_name="Chargé test",
                priority="Haute",
                confirmation="Tentative",
                desired_start=date(2026, 9, 21),
                desired_end=date(2026, 9, 25),
                resource_count=1,
                required_competencies="PLC",
                estimated_hours=Decimal("16"),
                status="Soumise",
            )
            approved = WorkforceRequest(
                id="WR-APPROVED",
                legacy_demand_number="DMO-2026-0274",
                project_id="P-273",
                erp_task_code="310",
                erp_task_label="Programmation",
                requester_name="Chargé test",
                priority="Haute",
                confirmation="Confirmée",
                desired_start=date(2026, 9, 21),
                desired_end=date(2026, 9, 25),
                resource_count=1,
                required_competencies="PLC",
                estimated_hours=Decimal("24"),
                status="En planification",
                approved_by_name="Coordonnateur test",
            )
            session.add_all([submitted, approved])
            session.flush()
            session.add_all(
                [
                    WorkforceRequestCompetency(
                        workforce_request_id=submitted.id,
                        competency_id="C-PLC",
                    ),
                    WorkforceRequestCompetency(
                        workforce_request_id=approved.id,
                        competency_id="C-PLC",
                    ),
                ]
            )

            target = ResourceRequirement(
                id="REQ-TARGET",
                legacy_segment_id="SEG-2026-0273",
                project_id="P-273",
                workforce_request_id=approved.id,
                approved_task_catalog_item_id="T-APPROVED",
                approval_reference_status="CAPTURED",
                assigned_resource_id=None,
                start_date=date(2026, 9, 21),
                end_date=date(2026, 9, 25),
                planned_hours=Decimal("24"),
                status="À assigner",
                required_resource_class="Programmation",
                required_competency="PLC",
                required_competency_id="C-PLC",
                priority="Haute",
                confirmation="Confirmée",
                origin="REQUEST",
            )
            covered = ResourceRequirement(
                id="REQ-COVERED",
                legacy_segment_id="SEG-COVERED",
                project_id="P-273",
                workforce_request_id=approved.id,
                assigned_resource_id=None,
                start_date=date(2026, 9, 21),
                end_date=date(2026, 9, 25),
                planned_hours=Decimal("8"),
                status="À assigner",
                required_competency="PLC",
                required_competency_id="C-PLC",
                priority="Normale",
                confirmation="Confirmée",
                origin="REQUEST",
            )
            load = ResourceRequirement(
                id="REQ-LOAD",
                legacy_segment_id="SEG-2026-0272",
                project_id="P-273",
                workforce_request_id=approved.id,
                assigned_resource_id="R-ALICE",
                start_date=date(2026, 9, 21),
                end_date=date(2026, 9, 25),
                planned_hours=Decimal("8"),
                status="Planifié",
                required_competency="PLC",
                required_competency_id="C-PLC",
                priority="Normale",
                confirmation="Confirmée",
                origin="REQUEST",
            )
            session.add_all([target, covered, load])
            session.flush()
            session.add(
                ResourceRequirementCompetency(
                    resource_requirement_id=target.id,
                    competency_id="C-PLC",
                )
            )
            session.add_all(
                [
                    Shift(
                        id="SHIFT-TARGET-LOCK",
                        resource_requirement_id=target.id,
                        resource_id="R-ALICE",
                        work_date=date(2026, 9, 22),
                        hours=Decimal("8"),
                        source="MANUAL",
                        locked=True,
                        outside_standard_hours=False,
                    ),
                    Shift(
                        id="SHIFT-COVERED",
                        resource_requirement_id=covered.id,
                        resource_id="R-BOB",
                        work_date=date(2026, 9, 22),
                        hours=Decimal("8"),
                        source="MANUAL",
                        locked=True,
                        outside_standard_hours=False,
                    ),
                ]
            )
            session.add(
                Shift(
                    id="SHIFT-ALICE",
                    resource_requirement_id=load.id,
                    resource_id="R-ALICE",
                    work_date=date(2026, 9, 21),
                    hours=Decimal("8"),
                    source="AUTO",
                    locked=False,
                    outside_standard_hours=False,
                )
            )

        engine.dispose()
        return url

    def test_queue_restores_approval_and_unassigned_work(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/planning/actions",
                    params={"start": "2026-09-21", "end": "2026-09-27"},
                )

            self.assertEqual(response.status_code, 200, response.text)
            rows = response.json()
            approvals = [row for row in rows if row["kind"] == "APPROVAL"]
            assignments = [row for row in rows if row["kind"] == "ASSIGNMENT"]

            self.assertEqual([row["demand_number"] for row in approvals], ["DMO-2026-0273"])
            self.assertEqual([row["segment_id"] for row in assignments], ["SEG-2026-0273"])

            approval = approvals[0]
            self.assertEqual(approval["task_code"], "253")
            self.assertEqual(approval["task_label"], "Installation")
            self.assertEqual(approval["required_competency"], "PLC")
            self.assertEqual(approval["priority"], "Haute")

            assignment = assignments[0]
            self.assertEqual(assignment["demand_number"], "DMO-2026-0274")
            self.assertEqual(assignment["task_code"], "310")
            self.assertEqual(assignment["required_competency_id"], "C-PLC")
            self.assertEqual(assignment["planned_hours"], 16.0)

    def test_recommendations_prioritize_competency_then_capacity(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/segments/SEG-2026-0273/resource-recommendations"
                )

            self.assertEqual(response.status_code, 200, response.text)
            rows = response.json()
            self.assertEqual([row["resource_name"] for row in rows], ["Alice", "Bob"])

            alice = rows[0]
            self.assertTrue(alice["recommended"])
            self.assertTrue(alice["preferred"])
            self.assertEqual(alice["preferred_resource_id"], "R-ALICE")
            self.assertEqual(alice["preferred_resource_name"], "Alice")
            self.assertEqual(alice["preferred_resource_status"], "ELIGIBLE")
            self.assertEqual(alice["recommendation_category"], 1)
            self.assertEqual(alice["competency_state"], "SATISFIED")
            self.assertEqual(alice["missing_competency_ids"], [])
            self.assertFalse(alice["fallback_requires_confirmation"])
            self.assertTrue(alice["competency_match"])
            self.assertTrue(alice["class_match"])
            self.assertEqual(alice["required_class"], "Programmation")
            self.assertEqual(alice["capacity_hours"], 40.0)
            self.assertEqual(alice["confirmed_hours"], 16.0)
            self.assertEqual(alice["tentative_hours"], 0.0)
            self.assertEqual(alice["prudent_free"], 24.0)
            self.assertEqual(alice["overtime_needed"], 0.0)

            bob = rows[1]
            self.assertFalse(bob["preferred"])
            self.assertFalse(bob["competency_match"])
            self.assertFalse(bob["class_match"])
            self.assertEqual(bob["recommendation_category"], 7)
            self.assertEqual(bob["prudent_free"], 32.0)
            self.assertGreater(alice["score"], bob["score"])

    def test_recommendations_do_not_infer_class_and_keep_canonical_skill_ids(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                target = session.get(ResourceRequirement, "REQ-TARGET")
                assert target is not None
                target.required_resource_class = None
                target.required_competency = "Texte historique non canonique"
            engine.dispose()

            app = create_api_app(url)
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/segments/SEG-2026-0273/resource-recommendations"
                )

            self.assertEqual(response.status_code, 200, response.text)
            rows = response.json()
            alice, bob = rows
            self.assertIsNone(alice["required_class"])
            self.assertTrue(alice["class_match"])
            self.assertTrue(bob["class_match"])
            self.assertEqual(alice["required_competency"], "PLC")
            self.assertEqual(alice["competency_state"], "SATISFIED")
            self.assertEqual(bob["competency_state"], "MISSING")
            self.assertEqual(bob["recommendation_category"], 3)

    def test_preferred_resource_never_bypasses_real_qualification(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                task = session.get(TaskCatalogEntry, "T-APPROVED")
                assert task is not None
                task.preferred_resource_id = "R-BOB"
            engine.dispose()

            app = create_api_app(url)
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/segments/SEG-2026-0273/resource-recommendations"
                )

            self.assertEqual(response.status_code, 200, response.text)
            rows = response.json()
            alice, bob = rows
            self.assertEqual(alice["resource_id"], "R-ALICE")
            self.assertEqual(alice["recommendation_category"], 2)
            self.assertTrue(alice["recommended"])
            self.assertFalse(alice["preferred"])
            self.assertEqual(bob["resource_id"], "R-BOB")
            self.assertTrue(bob["preferred"])
            self.assertEqual(bob["recommendation_category"], 7)
            self.assertFalse(bob["recommended"])

    def test_inactive_preferred_resource_is_diagnostic_not_candidate(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                alice = session.get(Resource, "R-ALICE")
                assert alice is not None
                alice.active = False
            engine.dispose()

            app = create_api_app(url)
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/segments/SEG-2026-0273/resource-recommendations"
                )

            self.assertEqual(response.status_code, 200, response.text)
            rows = response.json()
            self.assertEqual([row["resource_id"] for row in rows], ["R-BOB"])
            self.assertFalse(rows[0]["preferred"])
            self.assertEqual(rows[0]["preferred_resource_id"], "R-ALICE")
            self.assertEqual(rows[0]["preferred_resource_name"], "Alice")
            self.assertEqual(rows[0]["preferred_resource_status"], "INACTIVE_LOCAL")

    def test_legacy_unknown_task_context_never_invents_preference(self) -> None:
        with TemporaryDirectory() as directory:
            url = self._database(directory)
            engine = create_sql_engine(url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                target = session.get(ResourceRequirement, "REQ-TARGET")
                assert target is not None
                target.approval_reference_status = "LEGACY_UNKNOWN"
            engine.dispose()

            app = create_api_app(url)
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/segments/SEG-2026-0273/resource-recommendations"
                )

            self.assertEqual(response.status_code, 200, response.text)
            rows = response.json()
            self.assertFalse(rows[0]["preferred"])
            self.assertIsNone(rows[0]["preferred_resource_id"])
            self.assertEqual(rows[0]["preferred_resource_status"], "UNRESOLVED_CONTEXT")
            self.assertEqual(rows[0]["recommendation_category"], 2)
            self.assertTrue(rows[0]["recommended"])


if __name__ == "__main__":
    unittest.main()
