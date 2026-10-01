from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from functools import partial
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.infrastructure.sql import (
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceClassConfig,
    TaskCatalogEntry,
    WorkPackage,
    WorkPackageWeeklyLoad,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class MediumTermClassPortfolioTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add_all(
            [
                Project(id="P1", number="P-1", name="Projet 1", status="Actif"),
                Project(id="P2", number="P-2", name="Projet 2", status="Actif"),
                Project(id="P3", number="P-3", name="Projet historique", status="Terminé"),
                ResourceClassConfig(
                    code="PROGRAMMEUR",
                    label="Programmeur",
                    average_hourly_cost_cad=Decimal("100"),
                    active=True,
                ),
                ResourceClassConfig(
                    code="INSTALLATEUR",
                    label="Installateur automatisation",
                    average_hourly_cost_cad=Decimal("90"),
                    active=True,
                ),
                ResourceClassConfig(
                    code="DESIGNER",
                    label="Designer",
                    average_hourly_cost_cad=Decimal("95"),
                    active=True,
                ),
                ResourceClassConfig(
                    code="QA",
                    label="Validation",
                    average_hourly_cost_cad=Decimal("85"),
                    active=True,
                ),
                Resource(
                    id="R-PROG",
                    name="Programmeur",
                    resource_class="PROGRAMMEUR",
                    active=True,
                ),
                Resource(
                    id="R-INST",
                    name="Installateur",
                    resource_class="INSTALLATEUR",
                    active=True,
                ),
                Resource(
                    id="R-UNCLASSIFIED",
                    name="Ressource sans classe",
                    resource_class=None,
                    active=True,
                ),
            ]
        )
        session.flush()

        session.add_all(
            [
                ResourceAvailabilityRule(
                    id="STD-PROG",
                    resource_id="R-PROG",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(12, 0),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="STD-INST",
                    resource_id="R-INST",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(10, 0),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="STD-UNCLASSIFIED",
                    resource_id="R-UNCLASSIFIED",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(9, 0),
                    active=True,
                ),
            ]
        )
        session.add_all(
            [
                TaskCatalogEntry(
                    id="T-P1-PROG",
                    project_number="P-1",
                    task_code="216",
                    label="Programmation P1",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                    budget_hours=Decimal("200"),
                    resource_class_code="PROGRAMMEUR",
                ),
                TaskCatalogEntry(
                    id="T-P1-INST",
                    project_number="P-1",
                    task_code="217",
                    label="Installation P1",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                    budget_hours=Decimal("100"),
                    resource_class_code="INSTALLATEUR",
                ),
                TaskCatalogEntry(
                    id="T-P1-NOCLASS",
                    project_number="P-1",
                    task_code="218",
                    label="Tâche classée ERP seulement",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                    budget_hours=Decimal("50"),
                    resource_class_code="PROGRAMMEUR",
                ),
                TaskCatalogEntry(
                    id="T-P1-DESIGN",
                    project_number="P-1",
                    task_code="219",
                    label="Design sans capacité",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                    budget_hours=Decimal("50"),
                    resource_class_code="DESIGNER",
                ),
                TaskCatalogEntry(
                    id="T-P1-MISSING",
                    project_number="P-1",
                    task_code="220",
                    label="Installation incomplète",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                    budget_hours=Decimal("50"),
                    resource_class_code="INSTALLATEUR",
                ),
                TaskCatalogEntry(
                    id="T-P2-PROG",
                    project_number="P-2",
                    task_code="216",
                    label="Programmation P2",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                    budget_hours=Decimal("100"),
                    resource_class_code="PROGRAMMEUR",
                ),
                TaskCatalogEntry(
                    id="T-P3-PROG",
                    project_number="P-3",
                    task_code="216",
                    label="Programmation historique",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                    budget_hours=Decimal("100"),
                    resource_class_code="PROGRAMMEUR",
                ),
            ]
        )
        session.flush()

        first = date(2026, 10, 5)
        last = date(2026, 10, 30)
        session.add_all(
            [
                WorkPackage(
                    id="WP-P1-PROG",
                    project_id="P1",
                    task_catalog_item_id="T-P1-PROG",
                    resource_class_code="PROGRAMMEUR",
                    name="Programmation",
                    start_date=first,
                    end_date=last,
                    planned_hours=Decimal("74"),
                    status="active",
                    weekly_load_origin="MANUAL",
                ),
                WorkPackage(
                    id="WP-P1-INST",
                    project_id="P1",
                    task_catalog_item_id="T-P1-INST",
                    resource_class_code="INSTALLATEUR",
                    name="Installation",
                    start_date=first,
                    end_date=last,
                    planned_hours=Decimal("24"),
                    status="active",
                    weekly_load_origin="MANUAL",
                ),
                WorkPackage(
                    id="WP-P1-NOCLASS",
                    project_id="P1",
                    task_catalog_item_id="T-P1-NOCLASS",
                    resource_class_code=None,
                    name="Charge sans classe",
                    start_date=first,
                    end_date=last,
                    planned_hours=Decimal("8"),
                    status="active",
                    weekly_load_origin="MANUAL",
                ),
                WorkPackage(
                    id="WP-P1-DESIGN",
                    project_id="P1",
                    task_catalog_item_id="T-P1-DESIGN",
                    resource_class_code="DESIGNER",
                    name="Design",
                    start_date=first,
                    end_date=last,
                    planned_hours=Decimal("20"),
                    status="active",
                    weekly_load_origin="MANUAL",
                ),
                WorkPackage(
                    id="WP-P1-MISSING",
                    project_id="P1",
                    task_catalog_item_id="T-P1-MISSING",
                    resource_class_code="INSTALLATEUR",
                    name="Charge incomplète",
                    start_date=date(2026, 10, 26),
                    end_date=date(2026, 10, 30),
                    planned_hours=Decimal("5"),
                    status="active",
                    weekly_load_origin=None,
                ),
                WorkPackage(
                    id="WP-P2-PROG",
                    project_id="P2",
                    task_catalog_item_id="T-P2-PROG",
                    resource_class_code="PROGRAMMEUR",
                    name="Programmation P2",
                    start_date=first,
                    end_date=last,
                    planned_hours=Decimal("16"),
                    status="active",
                    weekly_load_origin="MANUAL",
                ),
                WorkPackage(
                    id="WP-P3-PROG",
                    project_id="P3",
                    task_catalog_item_id="T-P3-PROG",
                    resource_class_code="PROGRAMMEUR",
                    name="Programmation historique",
                    start_date=first,
                    end_date=last,
                    planned_hours=Decimal("40"),
                    status="active",
                    weekly_load_origin="MANUAL",
                ),
            ]
        )
        session.flush()

        weeks = [
            date(2026, 10, 5),
            date(2026, 10, 12),
            date(2026, 10, 19),
            date(2026, 10, 26),
        ]
        loads = {
            "WP-P1-PROG": [Decimal("16"), Decimal("17"), Decimal("20"), Decimal("21")],
            "WP-P1-INST": [Decimal("6"), Decimal("6"), Decimal("6"), Decimal("6")],
            "WP-P1-NOCLASS": [Decimal("2"), Decimal("2"), Decimal("2"), Decimal("2")],
            "WP-P1-DESIGN": [Decimal("5"), Decimal("5"), Decimal("5"), Decimal("5")],
            "WP-P2-PROG": [Decimal("4"), Decimal("4"), Decimal("4"), Decimal("4")],
            "WP-P3-PROG": [Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10")],
        }
        session.add_all(
            WorkPackageWeeklyLoad(
                work_package_id=work_package_id,
                week_start=week_start,
                hours=hours,
            )
            for work_package_id, values in loads.items()
            for week_start, hours in zip(weeks, values, strict=True)
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="medium-term-class-portfolio.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _class_bucket(week: dict, code: str | None) -> dict:
        return next(
            row
            for row in week["classes"]
            if row["resource_class_code"] == code
        )

    def test_project_capacity_load_utilization_and_states_are_class_scoped(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "project_number": "P-1",
                        "start": "2026-10-05",
                        "end": "2026-10-30",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        weeks = payload["weeks"]

        programmer = [
            self._class_bucket(week, "PROGRAMMEUR")
            for week in weeks
        ]
        self.assertEqual(
            [Decimal(str(row["capacity_hours"])) for row in programmer],
            [Decimal("20.00")] * 4,
        )
        self.assertEqual(
            [Decimal(str(row["work_package_hours"])) for row in programmer],
            [Decimal("16"), Decimal("17"), Decimal("20"), Decimal("21")],
        )
        self.assertEqual(
            [Decimal(str(row["utilization"])) for row in programmer],
            [Decimal("80.00"), Decimal("85.00"), Decimal("100.00"), Decimal("105.00")],
        )
        self.assertEqual(
            [row["state"] for row in programmer],
            ["available", "warning", "warning", "overloaded"],
        )

        installer = self._class_bucket(weeks[0], "INSTALLATEUR")
        self.assertEqual(Decimal(str(installer["capacity_hours"])), Decimal("10.00"))
        self.assertEqual(Decimal(str(installer["work_package_hours"])), Decimal("6"))
        fourth_installer = self._class_bucket(weeks[3], "INSTALLATEUR")
        self.assertIsNone(fourth_installer["work_package_hours"])
        self.assertEqual(fourth_installer["state"], "unavailable")
        self.assertIn("WORK_PACKAGE_LOAD_INCOMPLETE", fourth_installer["diagnostics"])

        unclassified = self._class_bucket(weeks[0], None)
        self.assertEqual(unclassified["resource_class_label"], "Non classé")
        self.assertEqual(Decimal(str(unclassified["capacity_hours"])), Decimal("5.00"))
        self.assertEqual(Decimal(str(unclassified["work_package_hours"])), Decimal("2"))
        self.assertIn("UNCLASSIFIED_WORK_PACKAGE_LOAD", payload["diagnostics"])

        # The ERP task class is PROGRAMMEUR, but the classless WP stays classless.
        classless_task = next(task for task in payload["tasks"] if task["task_catalog_item_id"] == "T-P1-NOCLASS")
        self.assertIsNone(classless_task["work_packages"][0]["resource_class_code"])

        no_capacity = self._class_bucket(weeks[0], "DESIGNER")
        self.assertEqual(Decimal(str(no_capacity["capacity_hours"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(no_capacity["work_package_hours"])), Decimal("5"))
        self.assertIsNone(no_capacity["utilization"])
        self.assertEqual(no_capacity["state"], "overloaded")

    def test_zero_capacity_without_load_is_unavailable_when_class_is_selected(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "resource_class_code": "QA",
                        "start": "2026-10-05",
                        "end": "2026-10-09",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        week = response.json()["weeks"][0]
        self.assertEqual(len(week["classes"]), 1)
        qa = week["classes"][0]
        self.assertEqual(qa["resource_class_code"], "QA")
        self.assertEqual(Decimal(str(qa["capacity_hours"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(qa["work_package_hours"])), Decimal("0.00"))
        self.assertIsNone(qa["utilization"])
        self.assertEqual(qa["state"], "unavailable")

    def test_portfolio_project_task_and_class_filters_preserve_organizational_capacity(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                portfolio = client.get(
                    "/api/v1/medium-term/budget",
                    params={"start": "2026-10-05", "end": "2026-10-09"},
                )
                project = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "project_number": "P-1",
                        "start": "2026-10-05",
                        "end": "2026-10-09",
                    },
                )
                task = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "task_catalog_item_id": "T-P1-PROG",
                        "start": "2026-10-05",
                        "end": "2026-10-09",
                    },
                )
                combined = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "project_number": "P-1",
                        "task_catalog_item_id": "T-P1-PROG",
                        "resource_class_code": "PROGRAMMEUR",
                        "start": "2026-10-05",
                        "end": "2026-10-09",
                    },
                )

        for response in (portfolio, project, task, combined):
            self.assertEqual(response.status_code, 200, response.text)

        portfolio_payload = portfolio.json()
        self.assertIsNone(portfolio_payload["project_number"])
        self.assertEqual(portfolio_payload["project_count"], 2)
        self.assertEqual(
            {row["project_number"] for row in portfolio_payload["task_options"]},
            {"P-1", "P-2"},
        )
        portfolio_programmer = self._class_bucket(
            portfolio_payload["weeks"][0],
            "PROGRAMMEUR",
        )
        self.assertEqual(Decimal(str(portfolio_programmer["capacity_hours"])), Decimal("20.00"))
        self.assertEqual(Decimal(str(portfolio_programmer["work_package_hours"])), Decimal("20"))

        project_programmer = self._class_bucket(project.json()["weeks"][0], "PROGRAMMEUR")
        self.assertEqual(Decimal(str(project_programmer["capacity_hours"])), Decimal("20.00"))
        self.assertEqual(Decimal(str(project_programmer["work_package_hours"])), Decimal("16"))

        task_payload = task.json()
        self.assertEqual(
            [row["task_catalog_item_id"] for row in task_payload["tasks"]],
            ["T-P1-PROG"],
        )
        task_programmer = self._class_bucket(task_payload["weeks"][0], "PROGRAMMEUR")
        self.assertEqual(Decimal(str(task_programmer["capacity_hours"])), Decimal("20.00"))
        self.assertEqual(Decimal(str(task_programmer["work_package_hours"])), Decimal("16"))

        combined_payload = combined.json()
        self.assertEqual(
            [row["resource_class_code"] for row in combined_payload["weeks"][0]["classes"]],
            ["PROGRAMMEUR"],
        )
        self.assertEqual(
            Decimal(str(combined_payload["weeks"][0]["capacity_hours"])),
            Decimal("20.00"),
        )
        self.assertEqual(
            Decimal(str(combined_payload["weeks"][0]["work_package_hours"])),
            Decimal("16"),
        )

    def test_inactive_projects_are_excluded_by_default_and_explicitly_includable(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                active = client.get(
                    "/api/v1/medium-term/budget",
                    params={"start": "2026-10-05", "end": "2026-10-09"},
                )
                history = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "include_inactive_projects": "true",
                        "start": "2026-10-05",
                        "end": "2026-10-09",
                    },
                )

        self.assertEqual(active.status_code, 200, active.text)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(active.json()["project_count"], 2)
        self.assertEqual(history.json()["project_count"], 3)

        active_programmer = self._class_bucket(active.json()["weeks"][0], "PROGRAMMEUR")
        history_programmer = self._class_bucket(history.json()["weeks"][0], "PROGRAMMEUR")
        self.assertEqual(Decimal(str(active_programmer["work_package_hours"])), Decimal("20"))
        self.assertEqual(Decimal(str(history_programmer["work_package_hours"])), Decimal("30"))
        self.assertEqual(
            Decimal(str(active_programmer["capacity_hours"])),
            Decimal(str(history_programmer["capacity_hours"])),
        )


if __name__ == "__main__":
    unittest.main()
