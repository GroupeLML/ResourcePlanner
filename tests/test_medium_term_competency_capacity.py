from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from functools import partial
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.infrastructure.sql import (
    AvailabilityRuleResourceClass,
    Competency,
    Project,
    RequestLine,
    RequestLineCompetency,
    Resource,
    ResourceAvailabilityRule,
    ResourceClassConfig,
    ResourceCompetency,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestPeriod,
)
from app.server import create_api_app
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)

MONDAY = date(2026, 10, 5)
FRIDAY = date(2026, 10, 9)
NEXT_MONDAY = date(2026, 10, 12)
NEXT_FRIDAY = date(2026, 10, 16)


class MediumTermCompetencyCapacityTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add(
            Project(id="P1", number="P-1", name="Projet 619B", status="Actif")
        )
        session.add_all(
            [
                ResourceClassConfig(
                    code="AUTOMATION",
                    label="Automatisation",
                    average_hourly_cost_cad=Decimal("100"),
                    active=True,
                ),
                ResourceClassConfig(
                    code="FIELD",
                    label="Chantier",
                    average_hourly_cost_cad=Decimal("90"),
                    active=True,
                ),
            ]
        )
        session.add(
            TaskCatalogEntry(
                id="T1",
                project_number="P-1",
                task_code="216",
                label="Programmation",
                active=True,
                workforce_eligible=True,
                account_group="DEPMO",
                budget_hours=Decimal("200"),
                resource_class_code="FIELD",
            )
        )
        session.add_all(
            [
                Competency(
                    id="C-PLC",
                    name="PLC",
                    active=True,
                    sort_order=1,
                    resource_class_code="AUTOMATION",
                ),
                Competency(
                    id="C-SCADA",
                    name="SCADA",
                    active=True,
                    sort_order=2,
                    resource_class_code="AUTOMATION",
                ),
                Competency(
                    id="C-WELD",
                    name="Soudure",
                    active=True,
                    sort_order=3,
                    resource_class_code=None,
                ),
            ]
        )
        session.add_all(
            [
                Resource(
                    id="R-BOTH",
                    name="Alice",
                    resource_class="FIELD",
                    active=True,
                    erp_active=True,
                ),
                Resource(
                    id="R-PLC",
                    name="Bob",
                    resource_class="AUTOMATION",
                    active=True,
                    erp_active=True,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ResourceCompetency(resource_id="R-BOTH", competency_id="C-PLC"),
                ResourceCompetency(resource_id="R-BOTH", competency_id="C-SCADA"),
                ResourceCompetency(resource_id="R-PLC", competency_id="C-PLC"),
                ResourceAvailabilityRule(
                    id="STD-BOTH",
                    resource_id="R-BOTH",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="STD-PLC",
                    resource_id="R-PLC",
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(12, 0),
                    active=True,
                ),
                ResourceAvailabilityRule(
                    id="HOL-FIELD",
                    resource_id=None,
                    availability_type="Jour férié",
                    start_date=FRIDAY,
                    end_date=FRIDAY,
                    active=True,
                ),
            ]
        )
        session.flush()
        session.add(
            AvailabilityRuleResourceClass(
                availability_rule_id="HOL-FIELD",
                resource_class_code="FIELD",
            )
        )

        session.add_all(
            [
                WorkforceRequest(
                    id="D1",
                    legacy_demand_number="DMO-619B-1",
                    project_id="P1",
                    status="Brouillon",
                    confirmation="Confirmée",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("40"),
                    resource_count=7,
                    line_mode=True,
                ),
                WorkforceRequest(
                    id="D2",
                    legacy_demand_number="DMO-619B-2",
                    project_id="P1",
                    status="Soumise",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=NEXT_MONDAY,
                    estimated_hours=Decimal("99"),
                    resource_count=3,
                    line_mode=True,
                ),
                WorkforceRequest(
                    id="D3",
                    legacy_demand_number="DMO-619B-3",
                    project_id="P1",
                    status="Brouillon",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    resource_count=1,
                    line_mode=True,
                ),
                WorkforceRequest(
                    id="D4",
                    legacy_demand_number="DMO-619B-4",
                    project_id="P1",
                    status="Brouillon",
                    confirmation="Tentative",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("4"),
                    resource_count=1,
                    line_mode=True,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                RequestLine(
                    id="L1",
                    workforce_request_id="D1",
                    position=0,
                    kind="WORKFORCE",
                    slot_count=7,
                    required_resource_class="FIELD",
                    required_competencies_snapshot="PLC; SCADA",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("40"),
                    task_catalog_item_id="T1",
                    confirmation="Confirmée",
                    active=True,
                ),
                RequestLine(
                    id="L2",
                    workforce_request_id="D2",
                    position=0,
                    kind="WORKFORCE",
                    slot_count=3,
                    required_resource_class="FIELD",
                    required_competencies_snapshot="PLC",
                    desired_start=MONDAY,
                    desired_end=NEXT_MONDAY,
                    estimated_hours=Decimal("99"),
                    task_catalog_item_id="T1",
                    confirmation="Tentative",
                    active=True,
                ),
                RequestLine(
                    id="L3",
                    workforce_request_id="D3",
                    position=0,
                    kind="WORKFORCE",
                    slot_count=1,
                    required_resource_class="FIELD",
                    required_competencies_snapshot="Soudure",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=None,
                    task_catalog_item_id="T1",
                    confirmation="Tentative",
                    active=True,
                ),
                RequestLine(
                    id="L4",
                    workforce_request_id="D4",
                    position=0,
                    kind="WORKFORCE",
                    slot_count=1,
                    required_resource_class="FIELD",
                    required_competencies_snapshot="Compétence historique",
                    desired_start=MONDAY,
                    desired_end=FRIDAY,
                    estimated_hours=Decimal("4"),
                    task_catalog_item_id="T1",
                    confirmation="Tentative",
                    active=True,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                RequestLineCompetency(request_line_id="L1", competency_id="C-PLC"),
                RequestLineCompetency(request_line_id="L1", competency_id="C-SCADA"),
                RequestLineCompetency(request_line_id="L2", competency_id="C-PLC"),
                RequestLineCompetency(request_line_id="L3", competency_id="C-WELD"),
                WorkforceRequestPeriod(
                    id="P-MON",
                    period_key="P-MON",
                    workforce_request_id="D2",
                    request_line_id="L2",
                    sequence=0,
                    kind="ALTERNATIVE",
                    alternative_group="CHOICE",
                    start_date=MONDAY,
                    end_date=MONDAY,
                    hours=Decimal("8"),
                    confirmation="Tentative",
                    active=True,
                ),
                WorkforceRequestPeriod(
                    id="P-NEXT",
                    period_key="P-NEXT",
                    workforce_request_id="D2",
                    request_line_id="L2",
                    sequence=1,
                    kind="ALTERNATIVE",
                    alternative_group="CHOICE",
                    start_date=NEXT_MONDAY,
                    end_date=NEXT_MONDAY,
                    hours=Decimal("12"),
                    confirmation="Tentative",
                    active=True,
                ),
            ]
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="medium-term-competency-capacity.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _skill(week: dict, competency_id: str) -> dict:
        return next(
            row
            for row in week["competencies"]
            if row["competency_id"] == competency_id
        )

    def test_weekly_skill_load_capacity_and_common_qualification_are_non_additive(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "project_number": "P-1",
                        "start": MONDAY.isoformat(),
                        "end": NEXT_FRIDAY.isoformat(),
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        weeks = response.json()["weeks"]
        self.assertEqual([week["week_start"] for week in weeks], [
            MONDAY.isoformat(),
            NEXT_MONDAY.isoformat(),
        ])

        first_plc = self._skill(weeks[0], "C-PLC")
        first_scada = self._skill(weeks[0], "C-SCADA")
        first_weld = self._skill(weeks[0], "C-WELD")

        # D1 requires PLC AND SCADA: its full 40 h appears on each skill, but the
        # rows are explicitly non-additive. D2 contributes its unresolved 8 h
        # alternative in this week and resource_count is never multiplied again.
        self.assertEqual(Decimal(str(first_plc["requested_hours"])), Decimal("48.00"))
        self.assertEqual(Decimal(str(first_scada["requested_hours"])), Decimal("40.00"))
        self.assertTrue(first_plc["non_additive"])
        self.assertTrue(first_scada["non_additive"])

        # Friday's FIELD-only holiday removes eight hours from R-BOTH using its
        # real FIELD class, even though both skills are grouped under AUTOMATION.
        self.assertEqual(Decimal(str(first_plc["capacity_hours"])), Decimal("52.00"))
        self.assertEqual(Decimal(str(first_scada["capacity_hours"])), Decimal("32.00"))
        self.assertEqual(first_plc["qualifying_resource_count"], 2)
        self.assertEqual(first_scada["qualifying_resource_count"], 1)
        self.assertEqual(first_scada["state"], "overloaded")
        self.assertIn("COMPETENCY_CAPACITY_EXCEEDED", first_scada["diagnostics"])

        self.assertIsNone(first_weld["requested_hours"])
        self.assertIn(
            "COMPETENCY_REQUESTED_HOURS_UNAVAILABLE",
            first_weld["diagnostics"],
        )
        self.assertIn(
            "COMPETENCY_REFERENCE_UNRESOLVED",
            weeks[0]["competency_diagnostics"],
        )

        combination = weeks[0]["competency_combinations"][0]
        self.assertEqual(set(combination["competency_ids"]), {"C-PLC", "C-SCADA"})
        self.assertEqual(combination["required_resource_class_code"], "FIELD")
        self.assertEqual(Decimal(str(combination["requested_hours"])), Decimal("40.00"))
        self.assertEqual(Decimal(str(combination["common_capacity_hours"])), Decimal("32.00"))
        self.assertTrue(combination["advisory_only"])
        self.assertTrue(combination["non_additive"])
        self.assertIn(
            "COMPETENCY_COMMON_QUALIFICATION_EXCEEDED",
            combination["diagnostics"],
        )

        second_plc = self._skill(weeks[1], "C-PLC")
        self.assertEqual(Decimal(str(second_plc["requested_hours"])), Decimal("12.00"))
        self.assertIn(
            "COMPETENCY_ALTERNATIVE_UNRESOLVED",
            second_plc["diagnostics"],
        )

    def test_demand_only_projection_derives_week_window_without_work_package(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                response = client.get(
                    "/api/v1/medium-term/budget",
                    params={"project_number": "P-1"},
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["window_start"], MONDAY.isoformat())
        self.assertEqual(payload["window_end"], NEXT_MONDAY.isoformat())
        self.assertEqual(
            [week["week_start"] for week in payload["weeks"]],
            [MONDAY.isoformat(), NEXT_MONDAY.isoformat()],
        )
        self.assertEqual(
            Decimal(str(payload["weeks"][0]["capacity_hours"])),
            Decimal("52.00"),
        )
        self.assertEqual(
            Decimal(str(self._skill(payload["weeks"][0], "C-SCADA")["requested_hours"])),
            Decimal("40.00"),
        )

    def test_business_class_and_competency_group_filters_are_distinct(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app) as client:
                matching_business = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "project_number": "P-1",
                        "resource_class_code": "FIELD",
                        "competency_resource_class_code": "AUTOMATION",
                        "start": MONDAY.isoformat(),
                        "end": FRIDAY.isoformat(),
                    },
                )
                other_business = client.get(
                    "/api/v1/medium-term/budget",
                    params={
                        "project_number": "P-1",
                        "resource_class_code": "AUTOMATION",
                        "competency_resource_class_code": "AUTOMATION",
                        "start": MONDAY.isoformat(),
                        "end": FRIDAY.isoformat(),
                    },
                )

        self.assertEqual(matching_business.status_code, 200, matching_business.text)
        self.assertEqual(other_business.status_code, 200, other_business.text)

        matching_skills = matching_business.json()["weeks"][0]["competencies"]
        other_skills = other_business.json()["weeks"][0]["competencies"]
        self.assertEqual(
            {row["competency_id"] for row in matching_skills},
            {"C-PLC", "C-SCADA"},
        )
        self.assertNotIn("C-WELD", {row["competency_id"] for row in matching_skills})
        self.assertEqual(
            Decimal(str(self._skill(matching_business.json()["weeks"][0], "C-SCADA")["requested_hours"])),
            Decimal("40.00"),
        )
        self.assertEqual(
            Decimal(str(self._skill(other_business.json()["weeks"][0], "C-SCADA")["requested_hours"])),
            Decimal("0.00"),
        )


if __name__ == "__main__":
    unittest.main()
