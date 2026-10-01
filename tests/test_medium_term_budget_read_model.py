from __future__ import annotations

from decimal import Decimal
from functools import partial
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.infrastructure.sql import (
    Project,
    ResourceClassConfig,
    TaskCatalogEntry,
    WorkPackage,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class MediumTermBudgetReadModelTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add_all(
            [
                Project(id="P1", number="P-1", name="Projet 1", status="Actif"),
                Project(id="P2", number="P-2", name="Projet 2", status="Actif"),
                ResourceClassConfig(
                    code="PROGRAMMEUR",
                    label="Programmeur",
                    average_hourly_cost_cad=Decimal("100"),
                    active=True,
                ),
                ResourceClassConfig(
                    code="INSTALLATEUR_AUTOMATISATION",
                    label="Installateur automatisation",
                    average_hourly_cost_cad=Decimal("90"),
                    active=True,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                TaskCatalogEntry(
                    id="TASK-216",
                    project_number="P-1",
                    task_code="216",
                    label="Programmation",
                    active=True,
                    erp_task_id="ERP-216",
                    account_group=" DEPMO ",
                    workforce_eligible=True,
                    budget_hours=Decimal("240"),
                    resource_class_code="PROGRAMMEUR",
                ),
                TaskCatalogEntry(
                    id="TASK-217",
                    project_number="P-1",
                    task_code="217",
                    label="Installation automatisation",
                    active=True,
                    erp_task_id="ERP-217",
                    account_group="DEPMO",
                    workforce_eligible=True,
                    budget_hours=Decimal("80"),
                    resource_class_code="PROGRAMMEUR",
                ),
                TaskCatalogEntry(
                    id="TASK-218",
                    project_number="P-1",
                    task_code="218",
                    label="Budget inconnu",
                    active=True,
                    erp_task_id="ERP-218",
                    account_group="DEPMO",
                    workforce_eligible=True,
                    budget_hours=None,
                    budget_diagnostic="AVERAGE_HOURLY_COST_UNAVAILABLE",
                ),
                TaskCatalogEntry(
                    id="TASK-219",
                    project_number="P-1",
                    task_code="219",
                    label="À structurer",
                    active=True,
                    erp_task_id="ERP-219",
                    account_group="DEPMO",
                    workforce_eligible=True,
                    budget_hours=Decimal("50"),
                ),
                TaskCatalogEntry(
                    id="TASK-220",
                    project_number="P-1",
                    task_code="220",
                    label="Couverture exacte",
                    active=True,
                    erp_task_id="ERP-220",
                    account_group="DEPMO",
                    workforce_eligible=True,
                    budget_hours=Decimal("16"),
                ),
                TaskCatalogEntry(
                    id="TASK-221",
                    project_number="P-1",
                    task_code="221",
                    label="Charge WP inconnue",
                    active=True,
                    erp_task_id="ERP-221",
                    account_group="DEPMO",
                    workforce_eligible=True,
                    budget_hours=Decimal("40"),
                ),
                TaskCatalogEntry(
                    id="TASK-MATERIAL",
                    project_number="P-1",
                    task_code="500",
                    label="Matériel",
                    active=True,
                    erp_task_id="ERP-MATERIAL",
                    account_group="DEPMAT",
                    workforce_eligible=True,
                    budget_hours=Decimal("999"),
                ),
                TaskCatalogEntry(
                    id="TASK-P2-216",
                    project_number="P-2",
                    task_code="216",
                    label="Programmation P2",
                    active=True,
                    erp_task_id="ERP-P2-216",
                    account_group="DEPMO",
                    workforce_eligible=True,
                    budget_hours=Decimal("100"),
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                WorkPackage(
                    id="WP-216-A",
                    project_id="P1",
                    task_catalog_item_id="TASK-216",
                    name="Lot A",
                    resource_class_code="PROGRAMMEUR",
                    planned_hours=Decimal("120"),
                    status="planned",
                ),
                WorkPackage(
                    id="WP-216-B",
                    project_id="P1",
                    task_catalog_item_id="TASK-216",
                    name="Lot B fermé",
                    planned_hours=Decimal("80"),
                    status="closed",
                ),
                WorkPackage(
                    id="WP-217-A",
                    project_id="P1",
                    task_catalog_item_id="TASK-217",
                    name="Lot suralloué",
                    resource_class_code="INSTALLATEUR_AUTOMATISATION",
                    planned_hours=Decimal("100"),
                    status="active",
                ),
                WorkPackage(
                    id="WP-217-CANCELLED",
                    project_id="P1",
                    task_catalog_item_id="TASK-217",
                    name="Lot annulé",
                    planned_hours=Decimal("50"),
                    status="cancelled",
                ),
                WorkPackage(
                    id="WP-220",
                    project_id="P1",
                    task_catalog_item_id="TASK-220",
                    name="Lot exact",
                    planned_hours=Decimal("16"),
                    status="active",
                ),
                WorkPackage(
                    id="WP-221",
                    project_id="P1",
                    task_catalog_item_id="TASK-221",
                    name="Lot sans charge",
                    planned_hours=None,
                    status="active",
                ),
                WorkPackage(
                    id="WP-HISTORICAL",
                    project_id="P1",
                    task_catalog_item_id=None,
                    name="Historique non classé",
                    planned_hours=Decimal("40"),
                    status="planned",
                    legacy_effort_id="EFF-HISTORICAL",
                ),
                WorkPackage(
                    id="WP-MATERIAL",
                    project_id="P1",
                    task_catalog_item_id="TASK-MATERIAL",
                    name="Lot matériel",
                    planned_hours=Decimal("500"),
                    status="active",
                ),
                WorkPackage(
                    id="WP-P2",
                    project_id="P2",
                    task_catalog_item_id="TASK-P2-216",
                    name="Lot projet 2",
                    planned_hours=Decimal("40"),
                    status="active",
                ),
            ]
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="medium-term-budget.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _task(payload: dict, code: str) -> dict:
        return next(row for row in payload["tasks"] if row["task_code"] == code)

    def test_projection_aggregates_budget_with_stable_backend_diagnostics(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={"project_number": "P-1"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["project_id"], "P1")
        self.assertEqual(payload["project_number"], "P-1")
        self.assertEqual(payload["project_name"], "Projet 1")
        self.assertEqual(
            {task["task_code"] for task in payload["tasks"]},
            {"216", "217", "218", "219", "220", "221"},
        )

        programming = self._task(payload, "216")
        self.assertEqual(Decimal(str(programming["budget_hours"])), Decimal("240"))
        self.assertEqual(Decimal(str(programming["planned_wp_hours"])), Decimal("200"))
        self.assertEqual(
            Decimal(str(programming["remaining_budget_hours"])),
            Decimal("40"),
        )
        self.assertEqual(programming["diagnostic_state"], "PARTIALLY_COVERED")
        self.assertEqual(programming["associated_work_package_count"], 2)
        self.assertEqual(programming["budget_included_work_package_count"], 2)
        classed = next(
            row for row in programming["work_packages"] if row["id"] == "WP-216-A"
        )
        self.assertEqual(classed["resource_class_code"], "PROGRAMMEUR")
        self.assertEqual(classed["resource_class_label"], "Programmeur")
        self.assertTrue(classed["resource_class_active"])
        self.assertEqual(classed["task_resource_class_code"], "PROGRAMMEUR")
        self.assertIsNone(classed["resource_class_diagnostic"])

        closed = next(row for row in programming["work_packages"] if row["id"] == "WP-216-B")
        self.assertTrue(closed["budget_included"])
        self.assertIsNone(closed["resource_class_code"])
        self.assertEqual(closed["task_resource_class_code"], "PROGRAMMEUR")
        self.assertEqual(
            closed["resource_class_diagnostic"],
            "WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE",
        )

        over = self._task(payload, "217")
        self.assertEqual(Decimal(str(over["planned_wp_hours"])), Decimal("100"))
        self.assertEqual(Decimal(str(over["remaining_budget_hours"])), Decimal("-20"))
        self.assertEqual(over["diagnostic_state"], "OVERALLOCATED")
        self.assertEqual(over["associated_work_package_count"], 2)
        self.assertEqual(over["budget_included_work_package_count"], 1)
        cancelled = next(
            row for row in over["work_packages"] if row["id"] == "WP-217-CANCELLED"
        )
        self.assertFalse(cancelled["budget_included"])
        divergent = next(
            row for row in over["work_packages"] if row["id"] == "WP-217-A"
        )
        self.assertEqual(
            divergent["resource_class_code"],
            "INSTALLATEUR_AUTOMATISATION",
        )
        self.assertEqual(divergent["task_resource_class_code"], "PROGRAMMEUR")
        self.assertEqual(
            divergent["resource_class_diagnostic"],
            "WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE",
        )

        unavailable = self._task(payload, "218")
        self.assertIsNone(unavailable["budget_hours"])
        self.assertIsNone(unavailable["remaining_budget_hours"])
        self.assertEqual(unavailable["diagnostic_state"], "BUDGET_UNAVAILABLE")
        self.assertEqual(
            unavailable["budget_source_diagnostic"],
            "AVERAGE_HOURLY_COST_UNAVAILABLE",
        )

        no_work_package = self._task(payload, "219")
        self.assertEqual(no_work_package["diagnostic_state"], "NO_WORK_PACKAGES")
        self.assertEqual(no_work_package["associated_work_package_count"], 0)
        self.assertEqual(
            Decimal(str(no_work_package["planned_wp_hours"])),
            Decimal("0"),
        )

        exact = self._task(payload, "220")
        self.assertEqual(exact["diagnostic_state"], "FULLY_COVERED")
        self.assertEqual(Decimal(str(exact["remaining_budget_hours"])), Decimal("0"))

        unknown_load = self._task(payload, "221")
        self.assertIsNone(unknown_load["planned_wp_hours"])
        self.assertIsNone(unknown_load["remaining_budget_hours"])
        self.assertEqual(
            unknown_load["diagnostic_state"],
            "WORK_PACKAGE_LOAD_UNAVAILABLE",
        )

    def test_only_depmo_tasks_contribute_and_historical_unclassified_wp_is_explicit(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={"project_number": "P-1"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertNotIn("500", {task["task_code"] for task in payload["tasks"]})
        self.assertEqual(
            payload["diagnostics"],
            ["UNCLASSIFIED_WORK_PACKAGES", "UNCLASSIFIED_WORK_PACKAGE_LOAD"],
        )
        self.assertEqual(len(payload["unclassified_work_packages"]), 1)
        historical = payload["unclassified_work_packages"][0]
        self.assertEqual(historical["id"], "WP-HISTORICAL")
        self.assertEqual(historical["reference"], "EFF-HISTORICAL")
        self.assertEqual(Decimal(str(historical["planned_hours"])), Decimal("40"))

    def test_projection_is_isolated_between_projects(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={"project_number": "P-2"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["project_id"], "P2")
        self.assertEqual(len(payload["tasks"]), 1)
        task = payload["tasks"][0]
        self.assertEqual(task["task_catalog_item_id"], "TASK-P2-216")
        self.assertEqual(Decimal(str(task["planned_wp_hours"])), Decimal("40"))
        self.assertEqual(Decimal(str(task["remaining_budget_hours"])), Decimal("60"))
        self.assertEqual(task["diagnostic_state"], "PARTIALLY_COVERED")
        self.assertEqual(payload["unclassified_work_packages"], [])
        self.assertEqual(payload["diagnostics"], ["UNCLASSIFIED_WORK_PACKAGE_LOAD"])

    def test_unknown_project_uses_application_error_contract(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={"project_number": "P-404"},
                )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(
            response.json()["error"]["code"],
            "medium_term_project_not_found",
        )


if __name__ == "__main__":
    unittest.main()
