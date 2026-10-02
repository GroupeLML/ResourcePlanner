from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from functools import partial
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application.security import AuthPrincipal, ROLE_PROJECT_MANAGER
from app.infrastructure.sql import (
    AppUser,
    BusinessContact,
    Project,
    ProjectCoManager,
    ResourceClassConfig,
    TaskCatalogEntry,
    TaskCatalogProjectSyncState,
    WorkPackage,
    WorkPackageWeeklyLoad,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from app.server.security import static_auth_resolver
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class MediumTermBudgetReadModelTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add_all(
            [
                BusinessContact(
                    id="BC-PM-1",
                    display_name="Benjamin Germain",
                    source="ERP",
                    external_system="ACUMATICA",
                    external_entity="EMPLOYEE",
                    external_id="EMP-PM-1",
                ),
                BusinessContact(
                    id="BC-LEGACY",
                    display_name="Ancienne FK ignorée",
                    source="LOCAL",
                ),
            ]
        )
        session.flush()
        session.add(
            AppUser(
                id="U-PM-1",
                issuer="urn:test",
                subject="pm-1",
                display_name="Benjamin Germain",
                email=None,
                employee_external_id="EMP-PM-1",
                business_contact_id="BC-PM-1",
                roles_json='["PROJECT_MANAGER"]',
                active=True,
            )
        )
        session.add_all(
            [
                Project(
                    id="P1",
                    number="P-1",
                    name="Projet 1",
                    status="Actif",
                    project_manager_external_id="EMP-PM-1",
                    project_manager_contact_id="BC-LEGACY",
                    project_manager_name="Libellé ERP non autoritaire",
                ),
                Project(
                    id="P2",
                    number="P-2",
                    name="Projet 2",
                    status="Actif",
                    project_manager_external_id="EMP-PM-2",
                    project_manager_name="Nom sans identité canonique",
                ),
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
        session.add(
            TaskCatalogProjectSyncState(
                project_number="P-1",
                last_attempt_at=datetime(2026, 10, 1, 13, 42, tzinfo=timezone.utc),
                last_success_at=datetime(2026, 10, 1, 13, 42, tzinfo=timezone.utc),
                source_rows=6,
                task_count=6,
                rejected_rows=0,
            )
        )
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
                    budget_amount_cad=Decimal("50000.00"),
                    budget_actual_cad=Decimal("31000.00"),
                    average_hourly_cost_cad=Decimal("100"),
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
                    budget_amount_cad=Decimal("50000.00"),
                    budget_actual_cad=Decimal("52000.00"),
                    average_hourly_cost_cad=Decimal("100"),
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
                    budget_amount_cad=Decimal("50000.00"),
                    budget_actual_cad=Decimal("31000.00"),
                    average_hourly_cost_cad=None,
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
                    budget_amount_cad=None,
                    budget_actual_cad=Decimal("10000.00"),
                    average_hourly_cost_cad=Decimal("100"),
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
                    planned_hours=Decimal("130"),
                    start_date=date(2026, 9, 21),
                    end_date=date(2026, 10, 5),
                    weekly_load_origin="MANUAL",
                    status="planned",
                ),
                WorkPackage(
                    id="WP-216-B",
                    project_id="P1",
                    task_catalog_item_id="TASK-216",
                    name="Lot B",
                    planned_hours=Decimal("50"),
                    start_date=date(2026, 9, 28),
                    end_date=date(2026, 10, 5),
                    weekly_load_origin="MANUAL",
                    status="planned",
                ),
                WorkPackage(
                    id="WP-216-CLOSED",
                    project_id="P1",
                    task_catalog_item_id="TASK-216",
                    name="Lot fermé historique",
                    planned_hours=Decimal("20"),
                    start_date=date(2026, 9, 28),
                    end_date=date(2026, 9, 28),
                    weekly_load_origin="MANUAL",
                    status="closed",
                ),
                WorkPackage(
                    id="WP-217-A",
                    project_id="P1",
                    task_catalog_item_id="TASK-217",
                    name="Lot suralloué",
                    resource_class_code="INSTALLATEUR_AUTOMATISATION",
                    planned_hours=Decimal("100"),
                    start_date=date(2026, 9, 28),
                    end_date=date(2026, 9, 28),
                    weekly_load_origin="MANUAL",
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
                    start_date=date(2026, 9, 28),
                    end_date=date(2026, 9, 28),
                    weekly_load_origin="MANUAL",
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
        session.flush()
        session.add_all(
            [
                WorkPackageWeeklyLoad(
                    work_package_id="WP-216-A",
                    week_start=date(2026, 9, 21),
                    hours=Decimal("30"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-216-A",
                    week_start=date(2026, 9, 28),
                    hours=Decimal("40"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-216-A",
                    week_start=date(2026, 10, 5),
                    hours=Decimal("60"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-216-B",
                    week_start=date(2026, 9, 28),
                    hours=Decimal("20"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-216-B",
                    week_start=date(2026, 10, 5),
                    hours=Decimal("30"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-216-CLOSED",
                    week_start=date(2026, 9, 28),
                    hours=Decimal("20"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-217-A",
                    week_start=date(2026, 9, 28),
                    hours=Decimal("100"),
                ),
                WorkPackageWeeklyLoad(
                    work_package_id="WP-220",
                    week_start=date(2026, 9, 28),
                    hours=Decimal("16"),
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

    def test_co_managers_expand_mine_scope_without_multiplying_medium_term_rows(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory.begin() as session:
                    session.add_all(
                        [
                            BusinessContact(
                                id="BC-CO-B",
                                display_name="Co chargé B",
                                source="LOCAL",
                            ),
                            BusinessContact(
                                id="BC-CO-C",
                                display_name="Co chargé C",
                                source="LOCAL",
                            ),
                            AppUser(
                                id="U-CO-B",
                                issuer=None,
                                subject=None,
                                display_name="Co chargé B",
                                email=None,
                                employee_external_id=None,
                                business_contact_id="BC-CO-B",
                                roles_json='["PROJECT_MANAGER"]',
                                active=True,
                            ),
                            AppUser(
                                id="U-CO-C",
                                issuer=None,
                                subject=None,
                                display_name="Co chargé C",
                                email=None,
                                employee_external_id=None,
                                business_contact_id="BC-CO-C",
                                roles_json='["PROJECT_MANAGER"]',
                                active=True,
                            ),
                        ]
                    )
                    project = session.get(Project, "P1")
                    assert project is not None
                    project.co_managers_version = 2
                    session.flush()
                    session.add_all(
                        [
                            ProjectCoManager(
                                project_id="P1",
                                business_contact_id="BC-CO-B",
                                created_by_user_id="U-CO-B",
                            ),
                            ProjectCoManager(
                                project_id="P1",
                                business_contact_id="BC-CO-C",
                                created_by_user_id="U-CO-C",
                            ),
                        ]
                    )
            finally:
                engine.dispose()

            principals = (
                AuthPrincipal.from_roles(
                    local_user_id="U-PM-1",
                    issuer="urn:test",
                    subject="pm",
                    display_name="Benjamin Germain",
                    email=None,
                    employee_external_id="EMP-PM-1",
                    roles=(ROLE_PROJECT_MANAGER,),
                    auth_mode="local",
                ),
                AuthPrincipal.from_roles(
                    local_user_id="U-CO-B",
                    issuer="urn:test",
                    subject="co-b",
                    display_name="Co chargé B",
                    email=None,
                    employee_external_id=None,
                    roles=(ROLE_PROJECT_MANAGER,),
                    auth_mode="local",
                ),
                AuthPrincipal.from_roles(
                    local_user_id="U-CO-C",
                    issuer="urn:test",
                    subject="co-c",
                    display_name="Co chargé C",
                    email=None,
                    employee_external_id=None,
                    roles=(ROLE_PROJECT_MANAGER,),
                    auth_mode="local",
                ),
            )

            payloads = []
            for principal in principals:
                app = create_api_app.func(
                    database_url,
                    auth_resolver=static_auth_resolver(principal),
                )
                with TestClient(app) as client:
                    response = client.get(
                        "/api/v1/medium-term/budget",
                        params={"scope": "mine"},
                    )
                self.assertEqual(response.status_code, 200, response.text)
                payloads.append(response.json())

        expected_ids = None
        for payload in payloads:
            task_ids = [task["task_catalog_item_id"] for task in payload["tasks"]]
            self.assertEqual(len(task_ids), len(set(task_ids)))
            self.assertTrue(task_ids)
            self.assertTrue(
                all(task["project_id"] == "P1" for task in payload["tasks"])
            )
            self.assertTrue(
                all(
                    task["manager_group_key"] == "erp:EMP-PM-1"
                    for task in payload["tasks"]
                )
            )
            if expected_ids is None:
                expected_ids = task_ids
            else:
                self.assertEqual(task_ids, expected_ids)

    def test_projection_aggregates_budget_with_stable_backend_diagnostics(self) -> None:
        with (
            patch(
                "app.infrastructure.sql.web_query_repository.current_business_date",
                return_value=date(2026, 9, 30),
            ),
            TemporaryDirectory() as directory,
        ):
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
        self.assertEqual(payload["reference_week_start"], "2026-09-28")
        self.assertEqual(payload["actual_through_date"], "2026-09-27")
        self.assertEqual(
            payload["reference_basis"],
            "ERP_BUDGET_ACTUAL_THROUGH_PREVIOUS_WEEK",
        )
        self.assertNotIn("last_approved_time_date", payload)
        self.assertNotIn("cutoff_status", payload)
        self.assertTrue(
            payload["erp_budget_last_success_at"].startswith("2026-10-01T13:42:00")
        )
        self.assertEqual(
            {task["task_code"] for task in payload["tasks"]},
            {"216", "217", "218", "219", "220", "221"},
        )

        programming = self._task(payload, "216")
        self.assertEqual(programming["project_manager_contact_id"], "BC-PM-1")
        self.assertEqual(
            programming["project_manager_display_name"],
            "Benjamin Germain",
        )
        self.assertEqual(programming["manager_group_key"], "erp:EMP-PM-1")
        self.assertEqual(programming["manager_display_name"], "Benjamin Germain")
        self.assertEqual(programming["manager_resolution_status"], "RESOLVED")
        self.assertTrue(
            programming["erp_budget_last_success_at"].startswith(
                "2026-10-01T13:42:00"
            )
        )
        self.assertEqual(
            Decimal(str(programming["budget_amount_cad"])),
            Decimal("50000"),
        )
        self.assertEqual(
            Decimal(str(programming["budget_actual_cad"])),
            Decimal("31000"),
        )
        self.assertEqual(
            Decimal(str(programming["remaining_budget_cad"])),
            Decimal("19000"),
        )
        self.assertIsNone(programming["financial_diagnostic"])
        self.assertEqual(
            Decimal(str(programming["average_hourly_cost_cad"])),
            Decimal("100"),
        )
        self.assertEqual(
            Decimal(str(programming["remaining_budget_hours_from_actual"])),
            Decimal("190"),
        )
        self.assertIsNone(programming["actual_hours_diagnostic"])
        self.assertEqual(
            Decimal(str(programming["future_work_package_hours"])),
            Decimal("150"),
        )
        self.assertIsNone(programming["future_work_package_diagnostic"])
        self.assertEqual(
            Decimal(str(programming["remaining_after_work_packages_hours"])),
            Decimal("40"),
        )
        self.assertEqual(Decimal(str(programming["budget_hours"])), Decimal("240"))
        self.assertEqual(Decimal(str(programming["planned_wp_hours"])), Decimal("200"))
        self.assertEqual(
            Decimal(str(programming["remaining_budget_hours"])),
            Decimal("40"),
        )
        self.assertNotEqual(
            Decimal(str(programming["remaining_budget_hours_from_actual"])),
            Decimal(str(programming["remaining_budget_hours"])),
        )
        self.assertEqual(programming["diagnostic_state"], "PARTIALLY_COVERED")
        self.assertEqual(programming["associated_work_package_count"], 3)
        self.assertEqual(programming["budget_included_work_package_count"], 3)
        classed = next(
            row for row in programming["work_packages"] if row["id"] == "WP-216-A"
        )
        self.assertEqual(classed["resource_class_code"], "PROGRAMMEUR")
        self.assertEqual(classed["resource_class_label"], "Programmeur")
        self.assertTrue(classed["resource_class_active"])
        self.assertEqual(classed["task_resource_class_code"], "PROGRAMMEUR")
        self.assertIsNone(classed["resource_class_diagnostic"])

        unclassed = next(
            row for row in programming["work_packages"] if row["id"] == "WP-216-B"
        )
        self.assertTrue(unclassed["budget_included"])
        self.assertTrue(unclassed["current_load_included"])
        self.assertIsNone(unclassed["resource_class_code"])
        self.assertEqual(unclassed["task_resource_class_code"], "PROGRAMMEUR")
        self.assertEqual(
            unclassed["resource_class_diagnostic"],
            "WORK_PACKAGE_TASK_RESOURCE_CLASS_DIVERGENCE",
        )
        closed = next(
            row for row in programming["work_packages"] if row["id"] == "WP-216-CLOSED"
        )
        self.assertTrue(closed["budget_included"])
        self.assertFalse(closed["current_load_included"])

        over = self._task(payload, "217")
        self.assertEqual(
            Decimal(str(over["remaining_budget_cad"])),
            Decimal("-2000"),
        )
        self.assertEqual(
            Decimal(str(over["remaining_budget_hours_from_actual"])),
            Decimal("-20"),
        )
        self.assertEqual(
            Decimal(str(over["future_work_package_hours"])),
            Decimal("100"),
        )
        self.assertEqual(
            Decimal(str(over["remaining_after_work_packages_hours"])),
            Decimal("-120"),
        )
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
        self.assertEqual(
            Decimal(str(unavailable["remaining_budget_cad"])),
            Decimal("19000"),
        )
        self.assertIsNone(unavailable["financial_diagnostic"])
        self.assertIsNone(unavailable["average_hourly_cost_cad"])
        self.assertIsNone(unavailable["remaining_budget_hours_from_actual"])
        self.assertEqual(
            unavailable["actual_hours_diagnostic"],
            "resource_class_cost_missing",
        )
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
        self.assertEqual(
            unknown_load["financial_diagnostic"],
            "ERP_FINANCIAL_BUDGET_INCOMPLETE",
        )
        self.assertIsNone(unknown_load["remaining_budget_hours_from_actual"])
        self.assertIsNone(unknown_load["future_work_package_hours"])
        self.assertEqual(
            unknown_load["future_work_package_diagnostic"],
            "WEEKLY_LOAD_INCOMPLETE",
        )
        self.assertIsNone(unknown_load["remaining_after_work_packages_hours"])
        self.assertIsNone(unknown_load["planned_wp_hours"])
        self.assertIsNone(unknown_load["remaining_budget_hours"])
        self.assertEqual(
            unknown_load["diagnostic_state"],
            "WORK_PACKAGE_LOAD_UNAVAILABLE",
        )

    def test_reference_week_keeps_current_monday_when_as_of_is_monday(self) -> None:
        with (
            patch(
                "app.infrastructure.sql.web_query_repository.current_business_date",
                return_value=date(2026, 10, 5),
            ),
            TemporaryDirectory() as directory,
        ):
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={"project_number": "P-1"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["reference_week_start"], "2026-10-05")
        self.assertEqual(payload["actual_through_date"], "2026-10-04")

    def test_future_load_uses_weekly_boundary_and_task_identity(self) -> None:
        with (
            patch(
                "app.infrastructure.sql.web_query_repository.current_business_date",
                return_value=date(2026, 9, 30),
            ),
            TemporaryDirectory() as directory,
        ):
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={"project_number": "P-1"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        programming = self._task(payload, "216")
        installation = self._task(payload, "217")
        self.assertEqual(
            Decimal(str(programming["future_work_package_hours"])),
            Decimal("150"),
        )
        self.assertEqual(
            Decimal(str(installation["future_work_package_hours"])),
            Decimal("100"),
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
        self.assertIsNone(payload["erp_budget_last_success_at"])
        self.assertIn("reference_week_start", payload)
        self.assertIn("actual_through_date", payload)
        self.assertNotIn("last_approved_time_date", payload)
        self.assertEqual(task["task_catalog_item_id"], "TASK-P2-216")
        self.assertIsNone(task["project_manager_contact_id"])
        self.assertEqual(
            task["project_manager_display_name"],
            "Nom sans identité canonique",
        )
        self.assertEqual(task["manager_group_key"], "erp:EMP-PM-2")
        self.assertEqual(
            task["manager_display_name"],
            "Nom sans identité canonique",
        )
        self.assertEqual(task["manager_resolution_status"], "UNRESOLVED_USER")
        self.assertIn(
            "ERP_PROJECT_MANAGER_APP_USER_NOT_LINKED",
            task["manager_diagnostics"],
        )
        self.assertIsNone(task["erp_budget_last_success_at"])
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
