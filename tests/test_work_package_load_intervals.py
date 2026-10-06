from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import partial
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application.work_package_load import (
    WorkPackageLoadIntervalValue,
    WorkPackageLoadState,
    projected_daily_loads,
    projected_weekly_loads,
    work_package_status,
)
from app.infrastructure.sql import (
    AppUser,
    Project,
    TaskCatalogEntry,
    WorkPackage,
    WorkPackageAudit,
    WorkPackageLoadInterval,
    WorkPackageWeeklyLoad,
    create_session_factory,
    create_sql_engine,
)
from app.server import create_api_app
from tests.approval_test_support import TEST_ADMIN_USER_ID
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class WorkPackageLoadProjectionTests(unittest.TestCase):
    def test_empty_explicit_collection_projects_full_balance_on_calendar_days(self) -> None:
        state = WorkPackageLoadState(
            reference="WP",
            version=1,
            start_date=date(2026, 9, 28),
            end_date=date(2026, 10, 7),
            planned_hours=Decimal("10.01"),
            legacy_status="planned",
            terminal_status=None,
            intervals=(),
        )

        weeks = projected_weekly_loads(state)

        self.assertEqual(
            sum((row.hours for row in weeks), Decimal("0.00")),
            Decimal("10.01"),
        )
        self.assertEqual(
            sum((row.explicit_hours for row in weeks), Decimal("0.00")),
            Decimal("0.00"),
        )
        self.assertEqual(
            sum((row.automatic_hours for row in weeks), Decimal("0.00")),
            Decimal("10.01"),
        )
        self.assertEqual([row.week_start for row in weeks], [
            date(2026, 9, 28),
            date(2026, 10, 5),
        ])
        # 10.01 h -> 1001 cents over 10 calendar days: first day gets 1.01 h.
        self.assertEqual(weeks[0].automatic_hours, Decimal("7.01"))
        self.assertEqual(weeks[1].automatic_hours, Decimal("3.00"))

    def test_daily_projection_preserves_full_distribution_before_midweek_cutoff(self) -> None:
        state = WorkPackageLoadState(
            reference="WP-614F",
            version=1,
            start_date=date(2026, 9, 28),
            end_date=date(2026, 10, 4),
            planned_hours=Decimal("40.00"),
            legacy_status="planned",
            terminal_status=None,
            intervals=(
                WorkPackageLoadIntervalValue(
                    id="I-614F",
                    start_date=date(2026, 9, 28),
                    end_date=date(2026, 10, 4),
                    hours=Decimal("40.00"),
                ),
            ),
        )

        days = projected_daily_loads(state)
        after_cutoff = sum(
            (row.hours for row in days if row.day >= date(2026, 10, 1)),
            Decimal("0.00"),
        )

        self.assertEqual(sum((row.hours for row in days), Decimal("0.00")), Decimal("40.00"))
        self.assertEqual(after_cutoff, Decimal("22.84"))
        self.assertEqual(
            [row.hours for row in days],
            [
                Decimal("5.72"),
                Decimal("5.72"),
                Decimal("5.72"),
                Decimal("5.71"),
                Decimal("5.71"),
                Decimal("5.71"),
                Decimal("5.71"),
            ],
        )

    def test_partial_explicit_interval_is_additive_and_balance_is_not_double_counted(self) -> None:
        state = WorkPackageLoadState(
            reference="WP",
            version=1,
            start_date=date(2026, 9, 28),
            end_date=date(2026, 10, 7),
            planned_hours=Decimal("100.00"),
            legacy_status="planned",
            terminal_status=None,
            intervals=(
                WorkPackageLoadIntervalValue(
                    id="I-1",
                    start_date=date(2026, 9, 30),
                    end_date=date(2026, 10, 2),
                    hours=Decimal("40.00"),
                ),
            ),
        )

        weeks = projected_weekly_loads(state)

        self.assertEqual(
            sum((row.explicit_hours for row in weeks), Decimal("0.00")),
            Decimal("40.00"),
        )
        self.assertEqual(
            sum((row.automatic_hours for row in weeks), Decimal("0.00")),
            Decimal("60.00"),
        )
        self.assertEqual(
            sum((row.hours for row in weeks), Decimal("0.00")),
            Decimal("100.00"),
        )

    def test_status_is_derived_from_terminal_fact_then_start_date(self) -> None:
        today = date(2026, 10, 3)
        self.assertEqual(
            work_package_status(
                start_date=date(2026, 10, 4),
                terminal_status=None,
                legacy_status="active",
                today=today,
            ),
            ("planned", None),
        )
        self.assertEqual(
            work_package_status(
                start_date=date(2026, 10, 3),
                terminal_status=None,
                legacy_status="planned",
                today=today,
            ),
            ("active", None),
        )
        self.assertEqual(
            work_package_status(
                start_date=date(2027, 1, 1),
                terminal_status="closed",
                legacy_status="planned",
                today=today,
            ),
            ("closed", None),
        )
        self.assertEqual(
            work_package_status(
                start_date=None,
                terminal_status=None,
                legacy_status="mystere",
                today=today,
            ),
            ("planned", "WORK_PACKAGE_STATUS_UNKNOWN_LEGACY"),
        )


class WorkPackageLoadIntervalApiTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add_all(
            [
                Project(id="P591", number="P-591", name="Projet 591", status="Actif"),
                AppUser(
                    id=TEST_ADMIN_USER_ID,
                    issuer="urn:resourceplanner:test",
                    subject="explicit-test-admin",
                    display_name="Administrateur de test explicite",
                    email=None,
                    roles_json='["ADMIN"]',
                    active=True,
                ),
                TaskCatalogEntry(
                    id="TASK-591",
                    project_number="P-591",
                    task_code="591",
                    label="Charge moyen terme",
                    status="Actif",
                    active=True,
                    workforce_eligible=True,
                    account_group="DEPMO",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                WorkPackage(
                    id="WP-591",
                    project_id="P591",
                    task_catalog_item_id="TASK-591",
                    version=1,
                    name="WP intervalles",
                    start_date=date(2026, 10, 1),
                    end_date=date(2026, 10, 10),
                    planned_hours=Decimal("100.00"),
                    status="planned",
                    legacy_effort_id="EFF-591",
                ),
                WorkPackage(
                    id="WP-LEGACY-591",
                    project_id="P591",
                    task_catalog_item_id="TASK-591",
                    version=1,
                    name="WP historique hebdo",
                    start_date=date(2026, 9, 28),
                    end_date=date(2026, 10, 4),
                    planned_hours=Decimal("8.00"),
                    status="planned",
                    weekly_load_origin="MANUAL",
                    legacy_effort_id="EFF-LEGACY-591",
                ),
            ]
        )
        session.flush()
        session.add(
            WorkPackageWeeklyLoad(
                work_package_id="WP-LEGACY-591",
                week_start=date(2026, 9, 28),
                hours=Decimal("8.00"),
            )
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="work-package-load-intervals.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _projected(client: TestClient, reference: str) -> dict[str, object]:
        response = client.get(
            "/api/v1/medium-term/budget",
            params={"project_number": "P-591"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        rows = [
            row
            for task in payload["tasks"]
            for row in task["work_packages"]
            if row["reference"] == reference
        ]
        assert len(rows) == 1, payload
        return rows[0]

    def test_partial_intervals_are_saved_atomically_with_work_package_properties(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)

            with TestClient(app, raise_server_exceptions=False) as client:
                saved = client.patch(
                    "/api/v1/work-packages/EFF-591",
                    json={
                        "expected_version": 1,
                        "name": "WP intervalles révisé",
                        "planned_hours": 120,
                        "load_intervals": [
                            {
                                "start_date": "2026-10-02",
                                "end_date": "2026-10-05",
                                "hours": "40.00",
                            }
                        ],
                    },
                )
                projected = self._projected(client, "EFF-591")

            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["version"], 2)
            self.assertEqual(Decimal(str(projected["planned_hours"])), Decimal("120.00"))
            self.assertEqual(Decimal(str(projected["explicit_hours"])), Decimal("40.00"))
            self.assertEqual(Decimal(str(projected["automatic_hours"])), Decimal("80.00"))
            self.assertEqual(len(projected["load_intervals"]), 1)
            self.assertTrue(projected["load_intervals"][0]["id"])
            self.assertEqual(projected["load_intervals"][0]["origin"], "MANUAL")
            self.assertEqual(
                sum(
                    (Decimal(str(row["hours"])) for row in projected["weekly_loads"]),
                    Decimal("0.00"),
                ),
                Decimal("120.00"),
            )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    wp = session.get(WorkPackage, "WP-591")
                    intervals = tuple(
                        session.scalars(
                            select(WorkPackageLoadInterval)
                            .where(WorkPackageLoadInterval.work_package_id == "WP-591")
                        ).all()
                    )
                    audits = tuple(
                        session.scalars(
                            select(WorkPackageAudit)
                            .where(WorkPackageAudit.work_package_id == "WP-591")
                        ).all()
                    )
                    self.assertEqual(wp.name, "WP intervalles révisé")
                    self.assertEqual(Decimal(wp.planned_hours), Decimal("120.00"))
                    self.assertEqual(len(intervals), 1)
                    self.assertEqual(len(audits), 1)
                    self.assertEqual(audits[0].resulting_version, 2)
            finally:
                engine.dispose()

    def test_explicit_hours_cannot_exceed_planned_and_failure_is_atomic(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.patch(
                    "/api/v1/work-packages/EFF-591",
                    json={
                        "expected_version": 1,
                        "name": "Ne doit pas persister",
                        "planned_hours": 30,
                        "load_intervals": [
                            {
                                "start_date": "2026-10-01",
                                "end_date": "2026-10-03",
                                "hours": "40.00",
                            }
                        ],
                    },
                )

            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(
                response.json()["error"]["code"],
                "work_package_load_explicit_exceeds_planned",
            )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    wp = session.get(WorkPackage, "WP-591")
                    count = int(
                        session.scalar(
                            select(func.count())
                            .select_from(WorkPackageLoadInterval)
                            .where(WorkPackageLoadInterval.work_package_id == "WP-591")
                        )
                        or 0
                    )
                    self.assertEqual(wp.version, 1)
                    self.assertEqual(wp.name, "WP intervalles")
                    self.assertEqual(Decimal(wp.planned_hours), Decimal("100.00"))
                    self.assertEqual(count, 0)
            finally:
                engine.dispose()

    def test_period_change_can_adjust_intervals_in_same_command_but_not_silently(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)

            with TestClient(app, raise_server_exceptions=False) as client:
                first = client.patch(
                    "/api/v1/work-packages/EFF-591",
                    json={
                        "expected_version": 1,
                        "load_intervals": [
                            {
                                "start_date": "2026-10-01",
                                "end_date": "2026-10-04",
                                "hours": "40.00",
                            }
                        ],
                    },
                )
                rejected = client.patch(
                    "/api/v1/work-packages/EFF-591",
                    json={
                        "expected_version": 2,
                        "start_date": "2026-10-03",
                    },
                )
                adjusted = client.patch(
                    "/api/v1/work-packages/EFF-591",
                    json={
                        "expected_version": 2,
                        "start_date": "2026-10-03",
                        "load_intervals": [
                            {
                                "id": self._projected(client, "EFF-591")["load_intervals"][0]["id"],
                                "start_date": "2026-10-03",
                                "end_date": "2026-10-04",
                                "hours": "40.00",
                            }
                        ],
                    },
                )

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(rejected.status_code, 409, rejected.text)
            self.assertEqual(
                rejected.json()["error"]["code"],
                "work_package_load_intervals_replan_required",
            )
            self.assertEqual(adjusted.status_code, 200, adjusted.text)
            self.assertEqual(adjusted.json()["version"], 3)

    def test_empty_canonical_collection_removes_legacy_explicit_intent(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)

            with TestClient(app, raise_server_exceptions=False) as client:
                before = self._projected(client, "EFF-LEGACY-591")
                cleared = client.patch(
                    "/api/v1/work-packages/EFF-LEGACY-591",
                    json={
                        "expected_version": 1,
                        "load_intervals": [],
                    },
                )
                after = self._projected(client, "EFF-LEGACY-591")

            self.assertEqual(Decimal(str(before["explicit_hours"])), Decimal("8.00"))
            self.assertEqual(Decimal(str(before["automatic_hours"])), Decimal("0.00"))
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertEqual(after["load_intervals"], [])
            self.assertEqual(Decimal(str(after["explicit_hours"])), Decimal("0.00"))
            self.assertEqual(Decimal(str(after["automatic_hours"])), Decimal("8.00"))

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    legacy_rows = int(
                        session.scalar(
                            select(func.count())
                            .select_from(WorkPackageWeeklyLoad)
                            .where(WorkPackageWeeklyLoad.work_package_id == "WP-LEGACY-591")
                        )
                        or 0
                    )
                    self.assertEqual(legacy_rows, 0)
                    self.assertIsNone(session.get(WorkPackage, "WP-LEGACY-591").weekly_load_origin)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
