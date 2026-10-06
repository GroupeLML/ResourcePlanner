from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.application.errors import ApplicationConflictError
from app.infrastructure.sql import (
    AppUser,
    Resource,
    SqlTaskCatalogRepository,
    TaskCatalogEntry,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.models import TaskCatalogPreferredResourceAudit


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(
    IS_MSSQL,
    "Validation multi-session réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class TaskPreferredResourceSqlServerConcurrencyTests(unittest.TestCase):
    def test_two_nominations_with_same_expected_version_have_one_winner(self) -> None:
        marker = uuid4().hex[:12]
        task_id = f"617A-T-{marker}"
        actor_id = f"617A-U-{marker}"
        resource_ids = (f"617A-R1-{marker}", f"617A-R2-{marker}")

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        AppUser(
                            id=actor_id,
                            display_name="617A SQL Server CAS",
                            roles_json=json.dumps(["ADMIN"]),
                            active=True,
                        ),
                        Resource(
                            id=resource_ids[0],
                            name=f"617A Alice {marker}",
                            active=True,
                            erp_active=True,
                        ),
                        Resource(
                            id=resource_ids[1],
                            name=f"617A Bob {marker}",
                            active=True,
                            erp_active=True,
                        ),
                        TaskCatalogEntry(
                            id=task_id,
                            project_number=f"617A-{marker}",
                            task_code="210",
                            label="617A SQL Server CAS",
                            active=True,
                        ),
                    ]
                )

            def assign(resource_id: str) -> str:
                with factory.begin() as session:
                    try:
                        SqlTaskCatalogRepository(session).set_preferred_resource(
                            task_id,
                            resource_id,
                            actor_user_id=actor_id,
                            expected_version=1,
                        )
                    except ApplicationConflictError as exc:
                        if exc.code != "task_preferred_resource_version_conflict":
                            raise
                        return "conflict"
                    return "committed"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(assign, resource_ids))

            self.assertEqual(sorted(outcomes), ["committed", "conflict"])
            with factory() as session:
                task = session.get(TaskCatalogEntry, task_id)
                assert task is not None
                self.assertEqual(task.preferred_resource_version, 2)
                self.assertIn(task.preferred_resource_id, resource_ids)
                audits = list(
                    session.scalars(
                        select(TaskCatalogPreferredResourceAudit).where(
                            TaskCatalogPreferredResourceAudit.task_catalog_item_id
                            == task_id
                        )
                    ).all()
                )
                self.assertEqual(len(audits), 1)
                self.assertEqual(audits[0].resulting_version, 2)
                self.assertEqual(audits[0].new_resource_id, task.preferred_resource_id)
        finally:
            try:
                with factory.begin() as session:
                    session.execute(
                        delete(TaskCatalogPreferredResourceAudit).where(
                            TaskCatalogPreferredResourceAudit.task_catalog_item_id
                            == task_id
                        )
                    )
                    session.execute(
                        delete(TaskCatalogEntry).where(TaskCatalogEntry.id == task_id)
                    )
                    session.execute(
                        delete(Resource).where(Resource.id.in_(resource_ids))
                    )
                    session.execute(delete(AppUser).where(AppUser.id == actor_id))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
