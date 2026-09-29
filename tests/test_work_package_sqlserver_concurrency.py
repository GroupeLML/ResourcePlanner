from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.infrastructure.sql import create_session_factory, create_sql_engine
from app.infrastructure.sql.demand_repository import SqlDemandRepository
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.models import (
    Project,
    RequestLine,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestHistory,
    WorkPackage,
    WorkPackageAudit,
)
from app.infrastructure.sql.work_package_repository import SqlWorkPackageRepository
from app.application.errors import ApplicationConflictError


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(
    IS_MSSQL,
    "Validation multi-session réservée à une base SQL Server de test via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class WorkPackageSqlServerConcurrencyTests(unittest.TestCase):
    """ADR-011 proof for the 502A dependency-vs-task race.

    The two operations deliberately start together. Whichever transaction acquires
    the WorkPackage row first, the final committed state must be coherent:
    either the task change wins before the dependency and the new line derives T2,
    or the dependency wins and the task change is rejected because the package is
    now in use.
    """

    def test_task_change_vs_new_request_dependency_is_serialized(self) -> None:
        marker = uuid4().hex[:12]
        project_id = f"WP-GUARD-P-{marker}"
        project_number = f"WPG-{marker}"
        task_1_id = f"WP-GUARD-T1-{marker}"
        task_2_id = f"WP-GUARD-T2-{marker}"
        work_package_id = f"WP-GUARD-WP-{marker}"
        actor_id = f"WP-GUARD-U-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        Project(
                            id=project_id,
                            number=project_number,
                            name="502A SQL Server guard",
                            status="Actif",
                        ),
                        TaskCatalogEntry(
                            id=task_1_id,
                            project_number=project_number,
                            task_code=f"210-{marker}",
                            label="T1",
                            status="Actif",
                            active=True,
                            workforce_eligible=True,
                        ),
                        TaskCatalogEntry(
                            id=task_2_id,
                            project_number=project_number,
                            task_code=f"211-{marker}",
                            label="T2",
                            status="Actif",
                            active=True,
                            workforce_eligible=True,
                        ),
                        AppUser(
                            id=actor_id,
                            issuer=f"urn:resourceplanner:502a:{marker}",
                            subject=marker,
                            display_name="502A SQL Server guard",
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
                        task_catalog_item_id=task_1_id,
                        version=1,
                        name="WP guard",
                        status="planned",
                    )
                )

            def change_task() -> str:
                with factory.begin() as session:
                    try:
                        SqlWorkPackageRepository(
                            session,
                            actor_user_id=actor_id,
                        ).update(
                            work_package_id,
                            {"task_catalog_item_id": task_2_id},
                            expected_version=1,
                        )
                    except ApplicationConflictError as exc:
                        if exc.code != "work_package_task_change_in_use":
                            raise
                        return "dependency_won"
                    return "task_change_won"

            def attach_request() -> str:
                with factory.begin() as session:
                    return SqlDemandRepository(
                        session,
                        actor_name="502A SQL Server guard",
                        actor_user_id=actor_id,
                    ).create(
                        {
                            "NumeroProjet": project_number,
                            "SourceEffortID": work_package_id,
                            "DateDebutSouhaitee": date(2026, 10, 5),
                            "TempsEstimeHeures": 8,
                        }
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                task_future = executor.submit(change_task)
                demand_future = executor.submit(attach_request)
                outcome = task_future.result(timeout=30)
                demand_number = demand_future.result(timeout=30)

            with factory() as session:
                work_package = session.get(WorkPackage, work_package_id)
                request = session.scalar(
                    select(WorkforceRequest).where(
                        WorkforceRequest.legacy_demand_number == demand_number
                    )
                )
                self.assertIsNotNone(work_package)
                self.assertIsNotNone(request)
                line = session.get(RequestLine, request.id)
                self.assertIsNotNone(line)
                self.assertEqual(line.work_package_id, work_package_id)

                if outcome == "task_change_won":
                    self.assertEqual(work_package.task_catalog_item_id, task_2_id)
                    self.assertEqual(line.task_catalog_item_id, task_2_id)
                    self.assertEqual(work_package.version, 2)
                else:
                    self.assertEqual(work_package.task_catalog_item_id, task_1_id)
                    self.assertEqual(line.task_catalog_item_id, task_1_id)
                    self.assertEqual(work_package.version, 1)
        finally:
            with factory.begin() as session:
                request_ids = tuple(
                    session.scalars(
                        select(WorkforceRequest.id).where(
                            WorkforceRequest.project_id == project_id
                        )
                    ).all()
                )
                if request_ids:
                    session.execute(
                        delete(WorkforceRequestHistory).where(
                            WorkforceRequestHistory.workforce_request_id.in_(request_ids)
                        )
                    )
                    session.execute(
                        delete(RequestLine).where(
                            RequestLine.workforce_request_id.in_(request_ids)
                        )
                    )
                    session.execute(
                        delete(WorkforceRequest).where(
                            WorkforceRequest.id.in_(request_ids)
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
                    delete(TaskCatalogEntry).where(
                        TaskCatalogEntry.id.in_((task_1_id, task_2_id))
                    )
                )
                session.execute(delete(AppUser).where(AppUser.id == actor_id))
                session.execute(delete(Project).where(Project.id == project_id))
            engine.dispose()


    def test_project_change_vs_new_request_dependency_is_serialized(self) -> None:
        marker = uuid4().hex[:12]
        project_1_id = f"WP-GUARD-P1-{marker}"
        project_2_id = f"WP-GUARD-P2-{marker}"
        project_1_number = f"WPG1-{marker}"
        project_2_number = f"WPG2-{marker}"
        work_package_id = f"WP-GUARD-WP2-{marker}"
        actor_id = f"WP-GUARD-U2-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        Project(
                            id=project_1_id,
                            number=project_1_number,
                            name="502A guard source",
                            status="Actif",
                        ),
                        Project(
                            id=project_2_id,
                            number=project_2_number,
                            name="502A guard target",
                            status="Actif",
                        ),
                        AppUser(
                            id=actor_id,
                            issuer=f"urn:resourceplanner:502a:project:{marker}",
                            subject=marker,
                            display_name="502A SQL Server project guard",
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
                        project_id=project_1_id,
                        task_catalog_item_id=None,
                        version=1,
                        name="WP project guard",
                        status="planned",
                    )
                )

            def change_project() -> str:
                with factory.begin() as session:
                    try:
                        SqlWorkPackageRepository(
                            session,
                            actor_user_id=actor_id,
                        ).update(
                            work_package_id,
                            {"project_number": project_2_number},
                            expected_version=1,
                        )
                    except ApplicationConflictError as exc:
                        if exc.code != "work_package_project_change_in_use":
                            raise
                        return "dependency_won"
                    return "project_change_won"

            def attach_request() -> tuple[str, str]:
                with factory.begin() as session:
                    try:
                        number = SqlDemandRepository(
                            session,
                            actor_name="502A SQL Server project guard",
                            actor_user_id=actor_id,
                        ).create(
                            {
                                "NumeroProjet": project_1_number,
                                "SourceEffortID": work_package_id,
                                "DateDebutSouhaitee": date(2026, 10, 5),
                                "TempsEstimeHeures": 8,
                            }
                        )
                    except ApplicationConflictError as exc:
                        if exc.code != "work_package_project_changed_during_link":
                            raise
                        return "project_change_won", ""
                    return "dependency_won", number

            with ThreadPoolExecutor(max_workers=2) as executor:
                project_future = executor.submit(change_project)
                demand_future = executor.submit(attach_request)
                project_outcome = project_future.result(timeout=30)
                demand_outcome, demand_number = demand_future.result(timeout=30)

            self.assertEqual(project_outcome, demand_outcome)

            with factory() as session:
                work_package = session.get(WorkPackage, work_package_id)
                self.assertIsNotNone(work_package)
                requests = list(
                    session.scalars(
                        select(WorkforceRequest).where(
                            WorkforceRequest.project_id == project_1_id
                        )
                    ).all()
                )
                if project_outcome == "project_change_won":
                    self.assertEqual(work_package.project_id, project_2_id)
                    self.assertEqual(requests, [])
                    self.assertEqual(work_package.version, 2)
                else:
                    self.assertEqual(work_package.project_id, project_1_id)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].legacy_demand_number, demand_number)
                    line = session.get(RequestLine, requests[0].id)
                    self.assertIsNotNone(line)
                    self.assertEqual(line.work_package_id, work_package_id)
                    self.assertEqual(work_package.version, 1)
        finally:
            with factory.begin() as session:
                request_ids = tuple(
                    session.scalars(
                        select(WorkforceRequest.id).where(
                            WorkforceRequest.project_id.in_(
                                (project_1_id, project_2_id)
                            )
                        )
                    ).all()
                )
                if request_ids:
                    session.execute(
                        delete(WorkforceRequestHistory).where(
                            WorkforceRequestHistory.workforce_request_id.in_(request_ids)
                        )
                    )
                    session.execute(
                        delete(RequestLine).where(
                            RequestLine.workforce_request_id.in_(request_ids)
                        )
                    )
                    session.execute(
                        delete(WorkforceRequest).where(
                            WorkforceRequest.id.in_(request_ids)
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
                session.execute(delete(AppUser).where(AppUser.id == actor_id))
                session.execute(
                    delete(Project).where(
                        Project.id.in_((project_1_id, project_2_id))
                    )
                )
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
