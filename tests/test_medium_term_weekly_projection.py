from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from functools import partial
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.infrastructure.sql import (
    ORIGIN_REQUEST,
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkPackage,
    WorkPackageWeeklyLoad,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class MediumTermWeeklyProjectionTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add_all(
            [
                Project(id="P1", number="P-1", name="Projet 1", status="Actif"),
                Project(id="P2", number="P-2", name="Projet 2", status="Actif"),
                TaskCatalogEntry(
                    id="T1",
                    project_number="P-1",
                    task_code="216",
                    label="Automatisation",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                    budget_hours=Decimal("100.00"),
                ),
                TaskCatalogEntry(
                    id="T2",
                    project_number="P-2",
                    task_code="216",
                    label="Automatisation P2",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                    budget_hours=Decimal("200.00"),
                ),
                Resource(
                    id="R1",
                    name="Alice",
                    resource_class="Programmation",
                    active=True,
                ),
                Resource(
                    id="R2",
                    name="Bob",
                    resource_class="Programmation",
                    active=True,
                ),
                Resource(
                    id="R3",
                    name="Inactive",
                    resource_class="Programmation",
                    active=False,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ResourceAvailabilityRule(
                    id="STD-R1",
                    resource_id="R1",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="STD-R2",
                    resource_id="R2",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(12, 0),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="STD-R3",
                    resource_id="R3",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="VAC-R1",
                    resource_id="R1",
                    availability_type="Vacances",
                    start_date=date(2026, 9, 14),
                    end_date=date(2026, 9, 14),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="HOL-1",
                    resource_id=None,
                    availability_type="Jour férié",
                    start_date=date(2026, 9, 21),
                    end_date=date(2026, 9, 21),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="HOL-ZERO",
                    resource_id=None,
                    availability_type="Jour férié",
                    start_date=date(2026, 9, 28),
                    end_date=date(2026, 10, 4),
                    active=True,
                ),
            ]
        )
        session.add_all(
            [
                WorkPackage(
                    id="WP-ACTIVE",
                    project_id="P1",
                    task_catalog_item_id="T1",
                    name="Actif",
                    start_date=date(2026, 9, 14),
                    end_date=date(2026, 9, 25),
                    planned_hours=Decimal("50.00"),
                    status="active",
                    weekly_load_origin="MANUAL",
                ),
                WorkPackage(
                    id="WP-CLOSED",
                    project_id="P1",
                    task_catalog_item_id="T1",
                    name="Fermé",
                    start_date=date(2026, 9, 14),
                    end_date=date(2026, 9, 25),
                    planned_hours=Decimal("20.00"),
                    status="closed",
                    weekly_load_origin="MANUAL",
                ),
                WorkPackage(
                    id="WP-CANCELLED",
                    project_id="P1",
                    task_catalog_item_id="T1",
                    name="Annulé",
                    start_date=date(2026, 9, 14),
                    end_date=date(2026, 9, 25),
                    planned_hours=Decimal("10.00"),
                    status="cancelled",
                    weekly_load_origin="MANUAL",
                ),
                WorkPackage(
                    id="WP-MISSING",
                    project_id="P1",
                    task_catalog_item_id="T1",
                    name="Répartition manquante",
                    start_date=date(2026, 9, 28),
                    end_date=date(2026, 10, 2),
                    planned_hours=Decimal("10.00"),
                    status="planned",
                    weekly_load_origin=None,
                ),
                WorkPackage(
                    id="WP-P2",
                    project_id="P2",
                    task_catalog_item_id="T2",
                    name="Charge autre projet",
                    start_date=date(2026, 9, 14),
                    end_date=date(2026, 9, 18),
                    planned_hours=Decimal("100.00"),
                    status="active",
                    weekly_load_origin="MANUAL",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                WorkPackageWeeklyLoad(
                    work_package_id="WP-ACTIVE",
                    week_start=date(2026, 9, 14),
                    hours=Decimal("30.00"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-ACTIVE",
                    week_start=date(2026, 9, 21),
                    hours=Decimal("20.00"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-CLOSED",
                    week_start=date(2026, 9, 14),
                    hours=Decimal("10.00"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-CLOSED",
                    week_start=date(2026, 9, 21),
                    hours=Decimal("10.00"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-CANCELLED",
                    week_start=date(2026, 9, 14),
                    hours=Decimal("5.00"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-CANCELLED",
                    week_start=date(2026, 9, 21),
                    hours=Decimal("5.00"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-P2",
                    week_start=date(2026, 9, 14),
                    hours=Decimal("100.00"),
                ),
            ]
        )

        session.add(
            WorkforceRequest(
                id="D1",
                legacy_demand_number="DMO-1",
                project_id="P1",
                status="En planification",
                confirmation="Confirmée",
                desired_start=date(2026, 9, 14),
                desired_end=date(2026, 9, 18),
            )
        )
        session.flush()
        session.add(
            ResourceRequirement(
                id="REQ1",
                project_id="P1",
                workforce_request_id="D1",
                assigned_resource_id="R1",
                start_date=date(2026, 9, 14),
                end_date=date(2026, 9, 18),
                planned_hours=Decimal("16.00"),
                status="Planifié",
                confirmation="Confirmée",
                origin=ORIGIN_REQUEST,
            )
        )
        session.flush()
        session.add(
            Shift(
                id="S1",
                resource_requirement_id="REQ1",
                resource_id="R1",
                work_date=date(2026, 9, 15),
                hours=Decimal("16.00"),
                source="MANUAL",
                locked=True,
            )
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="medium-term-weekly.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    def test_weekly_load_capacity_statuses_and_project_filter_are_backend_authoritative(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "project_number": "P-1",
                        "start": "2026-09-14",
                        "end": "2026-10-02",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        task = payload["tasks"][0]

        self.assertEqual(Decimal(str(task["planned_wp_hours"])), Decimal("80.00"))
        self.assertEqual(Decimal(str(task["remaining_budget_hours"])), Decimal("20.00"))

        packages = {row["id"]: row for row in task["work_packages"]}
        self.assertTrue(packages["WP-ACTIVE"]["current_load_included"])
        self.assertFalse(packages["WP-CLOSED"]["current_load_included"])
        self.assertTrue(packages["WP-CLOSED"]["budget_included"])
        self.assertFalse(packages["WP-CANCELLED"]["current_load_included"])
        self.assertFalse(packages["WP-CANCELLED"]["budget_included"])
        self.assertEqual(
            packages["WP-MISSING"]["weekly_load_diagnostic"],
            "WEEKLY_LOAD_MISSING",
        )

        weeks = {row["week_start"]: row for row in payload["weeks"]}
        first = weeks["2026-09-14"]
        second = weeks["2026-09-21"]
        third = weeks["2026-09-28"]

        self.assertEqual(Decimal(str(first["work_package_hours"])), Decimal("30.00"))
        self.assertEqual(Decimal(str(first["capacity_hours"])), Decimal("52.00"))
        self.assertEqual(Decimal(str(first["utilization"])), Decimal("57.69"))
        self.assertEqual(first["diagnostics"], [])

        self.assertEqual(Decimal(str(second["work_package_hours"])), Decimal("20.00"))
        self.assertEqual(Decimal(str(second["capacity_hours"])), Decimal("48.00"))
        self.assertEqual(Decimal(str(second["utilization"])), Decimal("41.67"))

        self.assertIsNone(third["work_package_hours"])
        self.assertEqual(Decimal(str(third["capacity_hours"])), Decimal("0.00"))
        self.assertIsNone(third["utilization"])
        self.assertEqual(
            set(third["diagnostics"]),
            {"WORK_PACKAGE_LOAD_INCOMPLETE", "WORKFORCE_CAPACITY_ZERO"},
        )

        self.assertIn("WEEKLY_LOAD_INCOMPLETE", payload["weekly_diagnostics"])
        self.assertIn("WORKFORCE_CAPACITY_ZERO", payload["weekly_diagnostics"])

        # P2 carries 100h in the first week, but project filtering affects load only.
        self.assertEqual(Decimal(str(first["work_package_hours"])), Decimal("30.00"))
        # A 16h Shift exists on R1; medium-term capacity remains gross workforce availability.
        self.assertEqual(Decimal(str(first["capacity_hours"])), Decimal("52.00"))

    def test_other_project_changes_load_without_creating_a_project_capacity_pool(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "project_number": "P-2",
                        "start": "2026-09-14",
                        "end": "2026-09-18",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        week = response.json()["weeks"][0]
        self.assertEqual(Decimal(str(week["work_package_hours"])), Decimal("100.00"))
        self.assertEqual(Decimal(str(week["capacity_hours"])), Decimal("52.00"))
        self.assertEqual(Decimal(str(week["utilization"])), Decimal("192.31"))

    def test_window_requires_both_dates(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={"project_number": "P-1", "start": "2026-09-14"},
                )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["error"]["code"],
            "medium_term_window_pair_required",
        )


if __name__ == "__main__":
    unittest.main()
