from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete

from app.application.errors import ApplicationConflictError
from app.application.work_package_weekly_load import WeeklyLoadValue
from app.infrastructure.sql import (
    AppUser,
    Project,
    TaskCatalogEntry,
    WorkPackage,
    WorkPackageAudit,
    WorkPackageWeeklyLoad,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.work_package_repository import SqlWorkPackageRepository


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(
    IS_MSSQL,
    "Validation multi-session réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class WorkPackageWeeklyLoadSqlServerConcurrencyTests(unittest.TestCase):
    def test_two_replacements_with_same_expected_version_are_serialized_by_work_package(self) -> None:
        marker = uuid4().hex[:12]
        project_id = f"502C-P-{marker}"
        project_number = f"502C-{marker}"
        task_id = f"502C-T-{marker}"
        work_package_id = f"502C-WP-{marker}"
        actor_id = f"502C-U-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        Project(
                            id=project_id,
                            number=project_number,
                            name="502C SQL Server CAS",
                            status="Actif",
                        ),
                        TaskCatalogEntry(
                            id=task_id,
                            project_number=project_number,
                            task_code=f"216-{marker}",
                            label="502C",
                            status="Actif",
                            active=True,
                            workforce_eligible=True,
                            account_group="DEPMO",
                        ),
                        AppUser(
                            id=actor_id,
                            issuer=f"urn:resourceplanner:502c:{marker}",
                            subject=marker,
                            display_name="502C SQL Server CAS",
                            email=None,
                            roles_json='["ADMIN"]',
                            active=True,
                        ),
                    ]
                )
                session.flush()
                session.add(
                    WorkPackage(
                        id=work_package_id,
                        project_id=project_id,
                        task_catalog_item_id=task_id,
                        version=1,
                        name="WP 502C",
                        start_date=date(2026, 9, 14),
                        end_date=date(2026, 9, 25),
                        planned_hours=Decimal("16.00"),
                        status="planned",
                    )
                )

            distributions = (
                (
                    WeeklyLoadValue(date(2026, 9, 14), Decimal("8.00")),
                    WeeklyLoadValue(date(2026, 9, 21), Decimal("8.00")),
                ),
                (
                    WeeklyLoadValue(date(2026, 9, 14), Decimal("10.00")),
                    WeeklyLoadValue(date(2026, 9, 21), Decimal("6.00")),
                ),
            )

            def replace(loads: tuple[WeeklyLoadValue, ...]) -> str:
                with factory.begin() as session:
                    try:
                        SqlWorkPackageRepository(
                            session,
                            actor_user_id=actor_id,
                        ).replace_weekly_loads(
                            work_package_id,
                            loads,
                            origin="MANUAL",
                            expected_version=1,
                        )
                    except ApplicationConflictError as exc:
                        if exc.code != "work_package_version_conflict":
                            raise
                        return "conflict"
                    return "committed"

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(replace, loads) for loads in distributions]
                outcomes = [future.result(timeout=30) for future in futures]

            self.assertEqual(sorted(outcomes), ["committed", "conflict"])
            with factory() as session:
                work_package = session.get(WorkPackage, work_package_id)
                rows = tuple(
                    session.query(WorkPackageWeeklyLoad)
                    .filter(WorkPackageWeeklyLoad.work_package_id == work_package_id)
                    .order_by(WorkPackageWeeklyLoad.week_start)
                    .all()
                )
                self.assertEqual(work_package.version, 2)
                self.assertEqual(work_package.weekly_load_origin, "MANUAL")
                self.assertEqual(
                    sum((Decimal(row.hours) for row in rows), Decimal("0.00")),
                    Decimal("16.00"),
                )
                self.assertEqual(len(rows), 2)
        finally:
            with factory.begin() as session:
                session.execute(
                    delete(WorkPackageWeeklyLoad).where(
                        WorkPackageWeeklyLoad.work_package_id == work_package_id
                    )
                )
                session.execute(
                    delete(WorkPackageAudit).where(
                        WorkPackageAudit.work_package_id == work_package_id
                    )
                )
                session.execute(
                    delete(WorkPackage).where(WorkPackage.id == work_package_id)
                )
                session.execute(
                    delete(TaskCatalogEntry).where(TaskCatalogEntry.id == task_id)
                )
                session.execute(delete(AppUser).where(AppUser.id == actor_id))
                session.execute(delete(Project).where(Project.id == project_id))
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
