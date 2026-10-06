from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete

from app.domain.verification import VerificationScope
from app.infrastructure.sql import (
    Project,
    SqlVerificationRepository,
    VerificationScopeRow,
    WorkPackage,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.verification_repository import VerificationVersionConflict


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(
    IS_MSSQL,
    "Validation multi-session réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class VerificationSqlServerConcurrencyTests(unittest.TestCase):
    def test_two_mutations_with_same_expected_version_have_one_winner(self) -> None:
        marker = uuid4().hex[:12]
        project_id = f"363B-P-{marker}"
        work_package_id = f"363B-WP-{marker}"
        scope_id = f"363B-VS-{marker}"

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add(
                    Project(
                        id=project_id,
                        number=f"363B-{marker}",
                        name="363B SQL Server CAS",
                    )
                )
                session.add(
                    WorkPackage(
                        id=work_package_id,
                        project_id=project_id,
                        name="363B SQL Server CAS",
                    )
                )
            with factory.begin() as session:
                SqlVerificationRepository(session).add_scope(
                    VerificationScope(
                        id=scope_id,
                        work_package_id=work_package_id,
                    )
                )

            def mutate(_: int) -> str:
                with factory.begin() as session:
                    try:
                        SqlVerificationRepository(session).compare_and_increment_version(
                            scope_id,
                            expected_verification_version=1,
                        )
                    except VerificationVersionConflict:
                        return "conflict"
                    return "committed"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(mutate, (1, 2)))

            self.assertEqual(sorted(outcomes), ["committed", "conflict"])
            with factory() as session:
                scope = session.get(VerificationScopeRow, scope_id)
                assert scope is not None
                self.assertEqual(scope.verification_version, 2)
        finally:
            try:
                with factory.begin() as session:
                    session.execute(
                        delete(VerificationScopeRow).where(
                            VerificationScopeRow.id == scope_id
                        )
                    )
                    session.execute(
                        delete(WorkPackage).where(WorkPackage.id == work_package_id)
                    )
                    session.execute(delete(Project).where(Project.id == project_id))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
