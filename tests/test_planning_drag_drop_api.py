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
from tests.sqlite_test_template import SqliteDatabaseTemplate

create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

class PlanningDragDropApiTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        project = Project(
            id="P-275",
            number="P-275",
            name="Projet drag drop",
            status="active",
        )
        alice = Resource(
            id="R-ALICE-275",
            name="Alice DnD",
            resource_class="Programmation",
            active=True,
            sort_order=10,
        )
        bob = Resource(
            id="R-BOB-275",
            name="Bob DnD",
            resource_class="Programmation",
            active=True,
            sort_order=20,
        )
        carol = Resource(
            id="R-CAROL-275",
            name="Carol DnD",
            resource_class="Programmation",
            active=True,
            sort_order=30,
        )
        session.add_all([project, alice, bob, carol])
        session.flush()

        for resource, suffix in ((alice, "ALICE"), (bob, "BOB"), (carol, "CAROL")):
            session.add(
                ResourceAvailabilityRule(
                    id=f"SCH-{suffix}-275",
                    resource_id=resource.id,
                    availability_type="Horaire standard",
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 12, 31),
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(7, 0),
                    end_time=time(15, 0),
                    active=True,
                )
            )
        session.add(
            ResourceAvailabilityRule(
                id="VAC-BOB-275",
                resource_id=bob.id,
                availability_type="Vacances",
                start_date=date(2026, 9, 23),
                end_date=date(2026, 9, 23),
                active=True,
            )
        )

        requirement = ResourceRequirement(
            id="REQ-275",
            legacy_segment_id="SEG-275",
            project_id=project.id,
            assigned_resource_id=alice.id,
            start_date=date(2026, 9, 21),
            end_date=date(2026, 9, 25),
            planned_hours=Decimal("16"),
            status="Planifié",
            confirmation="Confirmée",
            origin="AD_HOC",
        )
        session.add(requirement)
        session.flush()
        session.add(
            Shift(
                id="SHIFT-275",
                resource_requirement_id=requirement.id,
                resource_id=alice.id,
                work_date=date(2026, 9, 21),
                hours=Decimal("8"),
                allocation_type="Flexible",
                source="AUTO",
                locked=False,
                outside_standard_hours=False,
                note="Quart généré",
            )
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="planning-drag-drop.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    def test_move_contract_reassigns_day_and_locks_explicit_decision(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Coordonnateur DnD")
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/allocations/SHIFT-275/move",
                    json={"resource_id": "R-BOB-275", "day": "2026-09-22"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["action"], "moved")

                shifts = client.get(
                    "/api/v1/shifts",
                    params={"start": "2026-09-21", "end": "2026-09-27"},
                ).json()
                history = client.get("/api/v1/shifts/SHIFT-275/history").json()
                segment = client.get("/api/v1/segments/SEG-275").json()

            moved = next(row for row in shifts if row["allocation_id"] == "SHIFT-275")
            self.assertEqual(moved["resource_name"], "Bob DnD")
            self.assertEqual(moved["work_date"], "2026-09-22")
            self.assertEqual(moved["hours"], 8.0)
            self.assertEqual(moved["source"], "MANUAL")
            self.assertTrue(moved["locked"])
            self.assertFalse(moved["outside_standard_hours"])
            self.assertEqual(moved["note"], "Quart généré")
            self.assertEqual(segment["automatic_target_resource_id"], "R-ALICE-275")
            self.assertEqual(segment["automatic_target_resource_name"], "Alice DnD")
            self.assertEqual(segment["resource_name"], "Alice DnD")
            self.assertTrue(any(row["action"] == "Déplacement quart" for row in history))

    def test_explicit_segment_assignment_changes_only_automatic_target_by_id(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Coordonnateur DnD")
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/segments/SEG-275/assign",
                    json={"resource_id": "R-BOB-275"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                segment = client.get("/api/v1/segments/SEG-275").json()

            self.assertEqual(segment["automatic_target_resource_id"], "R-BOB-275")
            self.assertEqual(segment["automatic_target_resource_name"], "Bob DnD")
            self.assertEqual(segment["resource_name"], "Bob DnD")

    def test_move_a_to_b_then_explicit_target_c_preserves_b_and_retargets_only_remainder(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory), actor_name="Coordonnateur DnD")
            with TestClient(app) as client:
                moved = client.post(
                    "/api/v1/allocations/SHIFT-275/move",
                    json={"resource_id": "R-BOB-275", "day": "2026-09-22"},
                )
                self.assertEqual(moved.status_code, 200, moved.text)

                after_move = client.get("/api/v1/segments/SEG-275").json()
                self.assertEqual(after_move["automatic_target_resource_id"], "R-ALICE-275")
                self.assertEqual(after_move["locked_hours"], 8.0)
                self.assertEqual(after_move["automatic_rebuild_hours"], 8.0)

                shifts_after_move = client.get(
                    "/api/v1/shifts",
                    params={"start": "2026-09-21", "end": "2026-09-25"},
                ).json()
                locked_b = next(row for row in shifts_after_move if row["allocation_id"] == "SHIFT-275")
                self.assertEqual(locked_b["resource_id"], "R-BOB-275")
                self.assertTrue(locked_b["locked"])
                auto_after_move = [
                    row for row in shifts_after_move
                    if not row["locked"] and row["allocation_type"] != "Hors horaire requis"
                ]
                self.assertEqual(sum(row["hours"] for row in auto_after_move), 8.0)
                self.assertTrue(all(row["resource_id"] == "R-ALICE-275" for row in auto_after_move))

                assigned = client.post(
                    "/api/v1/segments/SEG-275/assign",
                    json={"resource_id": "R-CAROL-275"},
                )
                self.assertEqual(assigned.status_code, 200, assigned.text)

                final_segment = client.get("/api/v1/segments/SEG-275").json()
                final_shifts = client.get(
                    "/api/v1/shifts",
                    params={"start": "2026-09-21", "end": "2026-09-25"},
                ).json()

            self.assertEqual(final_segment["automatic_target_resource_id"], "R-CAROL-275")
            self.assertEqual(final_segment["automatic_target_resource_name"], "Carol DnD")
            final_locked_b = next(row for row in final_shifts if row["allocation_id"] == "SHIFT-275")
            self.assertEqual(final_locked_b["resource_id"], "R-BOB-275")
            self.assertTrue(final_locked_b["locked"])
            final_auto = [
                row for row in final_shifts
                if not row["locked"] and row["allocation_type"] != "Hors horaire requis"
            ]
            self.assertEqual(sum(row["hours"] for row in final_auto), 8.0)
            self.assertTrue(all(row["resource_id"] == "R-CAROL-275" for row in final_auto))

    def test_quick_shift_move_cannot_leave_its_single_day_segment(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/quick-shifts",
                    json={
                        "project_number": "P-275",
                        "project_name": "Projet drag drop",
                        "technician": "Alice DnD",
                        "day": "2026-09-24",
                        "hours": 2,
                        "outside_standard_hours": False,
                        "note": "Quick Shift fenêtre #275",
                        "description": "Quick Shift fenêtre",
                        "confirmation": "Confirmée",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                allocation_id = created.json()["allocation_id"]
                segment_id = created.json()["segment_id"]

                segment = client.get(f"/api/v1/segments/{segment_id}").json()
                self.assertEqual(segment["start_date"], "2026-09-24")
                self.assertEqual(segment["end_date"], "2026-09-24")

                response = client.post(
                    f"/api/v1/allocations/{allocation_id}/move",
                    json={"technician": "Bob DnD", "day": "2026-09-25"},
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("fenêtre du segment", response.json()["error"]["message"])

                shifts = client.get(
                    "/api/v1/shifts",
                    params={"start": "2026-09-21", "end": "2026-09-27"},
                ).json()

            original = next(row for row in shifts if row["allocation_id"] == allocation_id)
            self.assertEqual(original["resource_name"], "Alice DnD")
            self.assertEqual(original["work_date"], "2026-09-24")
            self.assertTrue(original["locked"])

    def test_move_rejected_outside_standard_schedule_rolls_back(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/allocations/SHIFT-275/move",
                    json={"technician": "Bob DnD", "day": "2026-09-23"},
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("Vacances", response.json()["error"]["message"])

                shifts = client.get(
                    "/api/v1/shifts",
                    params={"start": "2026-09-21", "end": "2026-09-27"},
                ).json()

            original = next(row for row in shifts if row["allocation_id"] == "SHIFT-275")
            self.assertEqual(original["resource_name"], "Alice DnD")
            self.assertEqual(original["work_date"], "2026-09-21")
            self.assertEqual(original["source"], "AUTO")
            self.assertFalse(original["locked"])


if __name__ == "__main__":
    unittest.main()
