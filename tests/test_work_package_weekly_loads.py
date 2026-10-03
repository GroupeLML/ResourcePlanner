from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import partial
from tempfile import TemporaryDirectory
import unittest
import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application.errors import ApplicationValidationError
from app.application.work_package_weekly_load import WeeklyLoadValue
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
    transactional_session,
)
from app.infrastructure.sql.work_package_repository import SqlWorkPackageRepository
from app.server import create_api_app
from tests.approval_test_support import TEST_ADMIN_USER_ID
from tests.http_test_auth import TEST_ADMIN_AUTH_RESOLVER
from tests.sqlite_test_template import SqliteDatabaseTemplate


create_api_app = partial(create_api_app, auth_resolver=TEST_ADMIN_AUTH_RESOLVER)


class WorkPackageWeeklyLoadTests(unittest.TestCase):
    @staticmethod
    def _seed_database(session) -> None:
        session.add_all(
            [
                Project(id="P1", number="P-1", name="Projet 1", status="Actif"),
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
                    id="TASK-P1-216",
                    project_number="P-1",
                    task_code="216",
                    label="Programmation",
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
                    id="WP-VALID",
                    project_id="P1",
                    task_catalog_item_id="TASK-P1-216",
                    version=1,
                    name="WP répartissable",
                    start_date=date(2026, 9, 14),
                    end_date=date(2026, 10, 2),
                    planned_hours=Decimal("10.01"),
                    status="planned",
                    legacy_effort_id="EFF-VALID",
                ),
                WorkPackage(
                    id="WP-NO-HOURS",
                    project_id="P1",
                    task_catalog_item_id="TASK-P1-216",
                    version=1,
                    name="WP sans charge",
                    start_date=date(2026, 9, 14),
                    end_date=date(2026, 9, 18),
                    planned_hours=None,
                    status="planned",
                ),
                WorkPackage(
                    id="WP-NO-DATES",
                    project_id="P1",
                    task_catalog_item_id="TASK-P1-216",
                    version=1,
                    name="WP sans dates",
                    planned_hours=Decimal("8.00"),
                    status="planned",
                ),
            ]
        )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._database_template = SqliteDatabaseTemplate(
            filename="work-package-weekly-loads.db",
            seed=cls._seed_database,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._database_template.cleanup()
        super().tearDownClass()

    def _database(self, directory: str) -> str:
        return self._database_template.copy_to(directory)

    @staticmethod
    def _loads() -> list[dict[str, object]]:
        return [
            {"week_start": "2026-09-14", "hours": "3.34"},
            {"week_start": "2026-09-21", "hours": "3.34"},
            {"week_start": "2026-09-28", "hours": "3.33"},
        ]

    def test_auto_proposal_is_deterministic_and_does_not_persist(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app) as client:
                first = client.post("/api/v1/work-packages/EFF-VALID/weekly-loads/proposal")
                second = client.post("/api/v1/work-packages/EFF-VALID/weekly-loads/proposal")

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json(), second.json())
            payload = first.json()
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["origin"], "AUTO")
            self.assertEqual(
                [(row["week_start"], Decimal(str(row["hours"]))) for row in payload["loads"]],
                [
                    ("2026-09-14", Decimal("3.34")),
                    ("2026-09-21", Decimal("3.34")),
                    ("2026-09-28", Decimal("3.33")),
                ],
            )

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    wp = session.get(WorkPackage, "WP-VALID")
                    count = session.scalar(
                        select(func.count())
                        .select_from(WorkPackageWeeklyLoad)
                        .where(WorkPackageWeeklyLoad.work_package_id == "WP-VALID")
                    )
                    self.assertIsNone(wp.weekly_load_origin)
                    self.assertEqual(int(count or 0), 0)
            finally:
                engine.dispose()

    def test_auto_acceptance_is_idempotent_and_manual_replacement_changes_origin(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app) as client:
                body = {
                    "expected_version": 1,
                    "origin": "AUTO",
                    "loads": self._loads(),
                }
                first = client.put(
                    "/api/v1/work-packages/EFF-VALID/weekly-loads",
                    json=body,
                    headers={"Idempotency-Key": "502c-auto-accept"},
                )
                replay = client.put(
                    "/api/v1/work-packages/EFF-VALID/weekly-loads",
                    json=body,
                    headers={"Idempotency-Key": "502c-auto-accept"},
                )
                manual = client.put(
                    "/api/v1/work-packages/EFF-VALID/weekly-loads",
                    json={
                        "expected_version": 2,
                        "origin": "MANUAL",
                        "loads": [
                            {"week_start": "2026-09-14", "hours": "4.00"},
                            {"week_start": "2026-09-21", "hours": "3.00"},
                            {"week_start": "2026-09-28", "hours": "3.01"},
                        ],
                    },
                )

            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["version"], 2)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json(), first.json())
            self.assertEqual(manual.status_code, 200, manual.text)
            self.assertEqual(manual.json()["version"], 3)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    wp = session.get(WorkPackage, "WP-VALID")
                    rows = tuple(
                        session.scalars(
                            select(WorkPackageWeeklyLoad)
                            .where(WorkPackageWeeklyLoad.work_package_id == "WP-VALID")
                            .order_by(WorkPackageWeeklyLoad.week_start)
                        ).all()
                    )
                    audits = tuple(
                        session.scalars(
                            select(WorkPackageAudit)
                            .where(WorkPackageAudit.work_package_id == "WP-VALID")
                            .order_by(WorkPackageAudit.resulting_version)
                        ).all()
                    )
                    self.assertEqual(wp.weekly_load_origin, "MANUAL")
                    self.assertEqual(wp.version, 3)
                    self.assertEqual([Decimal(row.hours) for row in rows], [
                        Decimal("4.00"), Decimal("3.00"), Decimal("3.01")
                    ])
                    self.assertEqual(
                        [row.action for row in audits],
                        ["REPLACE_WEEKLY_LOADS", "REPLACE_WEEKLY_LOADS"],
                    )
                    first_audit = json.loads(audits[0].new_values_json)
                    second_audit = json.loads(audits[1].new_values_json)
                    self.assertEqual(
                        first_audit["weekly_loads"],
                        [
                            {"week_start": "2026-09-14", "hours": "3.34"},
                            {"week_start": "2026-09-21", "hours": "3.34"},
                            {"week_start": "2026-09-28", "hours": "3.33"},
                        ],
                    )
                    self.assertEqual(
                        second_audit["weekly_loads"],
                        [
                            {"week_start": "2026-09-14", "hours": "4.00"},
                            {"week_start": "2026-09-21", "hours": "3.00"},
                            {"week_start": "2026-09-28", "hours": "3.01"},
                        ],
                    )
            finally:
                engine.dispose()

    def test_auto_origin_rejects_a_modified_distribution(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.put(
                    "/api/v1/work-packages/EFF-VALID/weekly-loads",
                    json={
                        "expected_version": 1,
                        "origin": "AUTO",
                        "loads": [
                            {"week_start": "2026-09-14", "hours": "4.00"},
                            {"week_start": "2026-09-21", "hours": "3.00"},
                            {"week_start": "2026-09-28", "hours": "3.01"},
                        ],
                    },
                )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["error"]["code"],
            "work_package_weekly_load_auto_proposal_mismatch",
        )

    def test_validation_rejects_duplicate_non_monday_outside_window_and_sum_mismatch(self) -> None:
        invalid_cases = (
            (
                "duplicate",
                [
                    {"week_start": "2026-09-14", "hours": "5.00"},
                    {"week_start": "2026-09-14", "hours": "5.01"},
                ],
                "work_package_weekly_load_duplicate_week",
            ),
            (
                "not-monday",
                [{"week_start": "2026-09-15", "hours": "10.01"}],
                "work_package_weekly_load_week_start_invalid",
            ),
            (
                "outside",
                [{"week_start": "2026-10-05", "hours": "10.01"}],
                "work_package_weekly_load_outside_window",
            ),
            (
                "under",
                [
                    {"week_start": "2026-09-14", "hours": "3.00"},
                    {"week_start": "2026-09-21", "hours": "3.00"},
                    {"week_start": "2026-09-28", "hours": "3.00"},
                ],
                "work_package_weekly_load_total_mismatch",
            ),
            (
                "over",
                [
                    {"week_start": "2026-09-14", "hours": "4.00"},
                    {"week_start": "2026-09-21", "hours": "4.00"},
                    {"week_start": "2026-09-28", "hours": "4.00"},
                ],
                "work_package_weekly_load_total_mismatch",
            ),
        )
        for label, loads, code in invalid_cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                app = create_api_app(self._database(directory))
                with TestClient(app, raise_server_exceptions=False) as client:
                    response = client.put(
                        "/api/v1/work-packages/EFF-VALID/weekly-loads",
                        json={
                            "expected_version": 1,
                            "origin": "MANUAL",
                            "loads": loads,
                        },
                    )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], code)

    def test_negative_hours_are_rejected_by_request_contract(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.put(
                    "/api/v1/work-packages/EFF-VALID/weekly-loads",
                    json={
                        "expected_version": 1,
                        "origin": "MANUAL",
                        "loads": [{"week_start": "2026-09-14", "hours": "-1"}],
                    },
                )
            self.assertEqual(response.status_code, 422, response.text)

    def test_proposal_requires_dates_and_planned_hours(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_api_app(self._database(directory))
            with TestClient(app, raise_server_exceptions=False) as client:
                no_hours = client.post("/api/v1/work-packages/WP-NO-HOURS/weekly-loads/proposal")
                no_dates = client.post("/api/v1/work-packages/WP-NO-DATES/weekly-loads/proposal")
            self.assertEqual(no_hours.status_code, 422, no_hours.text)
            self.assertEqual(
                no_hours.json()["error"]["code"],
                "work_package_weekly_load_planned_hours_required",
            )
            self.assertEqual(no_dates.status_code, 422, no_dates.text)
            self.assertEqual(
                no_dates.json()["error"]["code"],
                "work_package_weekly_load_dates_required",
            )

    def test_stale_cas_and_planned_hour_increase_preserves_explicit_intent(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            app = create_api_app(database_url)
            with TestClient(app, raise_server_exceptions=False) as client:
                accepted = client.put(
                    "/api/v1/work-packages/EFF-VALID/weekly-loads",
                    json={
                        "expected_version": 1,
                        "origin": "AUTO",
                        "loads": self._loads(),
                    },
                )
                stale = client.put(
                    "/api/v1/work-packages/EFF-VALID/weekly-loads",
                    json={
                        "expected_version": 1,
                        "origin": "MANUAL",
                        "loads": self._loads(),
                    },
                )
                increased = client.patch(
                    "/api/v1/work-packages/EFF-VALID",
                    json={"expected_version": 2, "planned_hours": 12.0},
                )

            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(
                stale.json()["error"]["code"],
                "work_package_version_conflict",
            )
            self.assertEqual(increased.status_code, 200, increased.text)
            self.assertEqual(increased.json()["version"], 3)

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with factory() as session:
                    wp = session.get(WorkPackage, "WP-VALID")
                    rows = tuple(
                        session.scalars(
                            select(WorkPackageWeeklyLoad)
                            .where(WorkPackageWeeklyLoad.work_package_id == "WP-VALID")
                            .order_by(WorkPackageWeeklyLoad.week_start)
                        ).all()
                    )
                    intervals = tuple(
                        session.scalars(
                            select(WorkPackageLoadInterval)
                            .where(WorkPackageLoadInterval.work_package_id == "WP-VALID")
                            .order_by(WorkPackageLoadInterval.start_date)
                        ).all()
                    )
                    self.assertEqual(wp.version, 3)
                    self.assertEqual(Decimal(wp.planned_hours), Decimal("12.00"))
                    self.assertEqual(
                        [Decimal(row.hours) for row in rows],
                        [Decimal("3.34"), Decimal("3.34"), Decimal("3.33")],
                    )
                    self.assertEqual(
                        sum((Decimal(row.hours) for row in intervals), Decimal("0.00")),
                        Decimal("10.01"),
                    )
            finally:
                engine.dispose()

    def test_failure_after_row_replacement_rolls_back_entire_transaction(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = self._database(directory)
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                with transactional_session(factory) as session:
                    SqlWorkPackageRepository(
                        session,
                        actor_user_id=TEST_ADMIN_USER_ID,
                    ).replace_weekly_loads(
                        "WP-VALID",
                        tuple(
                            WeeklyLoadValue(
                                week_start=date.fromisoformat(str(item["week_start"])),
                                hours=Decimal(str(item["hours"])),
                            )
                            for item in self._loads()
                        ),
                        origin="AUTO",
                        expected_version=1,
                    )

                with self.assertRaises(ApplicationValidationError):
                    with transactional_session(factory) as session:
                        SqlWorkPackageRepository(
                            session,
                            actor_user_id=None,
                        ).replace_weekly_loads(
                            "WP-VALID",
                            (
                                WeeklyLoadValue(date(2026, 9, 14), Decimal("4.00")),
                                WeeklyLoadValue(date(2026, 9, 21), Decimal("3.00")),
                                WeeklyLoadValue(date(2026, 9, 28), Decimal("3.01")),
                            ),
                            origin="MANUAL",
                            expected_version=2,
                        )

                with factory() as session:
                    wp = session.get(WorkPackage, "WP-VALID")
                    rows = tuple(
                        session.scalars(
                            select(WorkPackageWeeklyLoad)
                            .where(WorkPackageWeeklyLoad.work_package_id == "WP-VALID")
                            .order_by(WorkPackageWeeklyLoad.week_start)
                        ).all()
                    )
                    self.assertEqual(wp.version, 2)
                    self.assertEqual(wp.weekly_load_origin, "AUTO")
                    self.assertEqual(
                        [Decimal(row.hours) for row in rows],
                        [Decimal("3.34"), Decimal("3.34"), Decimal("3.33")],
                    )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
