from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import text

from app.infrastructure.sql import Base, create_sql_engine
from tools.check_server_runtime import (
    DriverReadinessError,
    MigrationReadinessError,
    _expected_alembic_head,
    check_database_preflight,
)
from tools.check_sqlserver_readiness import (
    check_mssql_boolean_predicates,
    check_mssql_cascade_paths,
    check_mssql_query_compilation,
    check_mssql_schema_compilation,
)


ROOT = Path(__file__).resolve().parents[1]


class SqlServerReadinessTests(unittest.TestCase):
    def test_schema_and_critical_queries_compile_with_mssql_dialect(self) -> None:
        cascades = check_mssql_cascade_paths()
        booleans = check_mssql_boolean_predicates()
        schema = check_mssql_schema_compilation()
        queries = check_mssql_query_compilation()

        self.assertEqual(cascades.status, "ok")
        self.assertIn("sans cycle ni chemin multiple", cascades.details)
        self.assertEqual(booleans.status, "ok")
        self.assertIn("IS 1 / IS 0", booleans.details)
        self.assertEqual(schema.status, "ok")
        self.assertEqual(queries.status, "ok")
        self.assertIn("requêtes compilées", queries.details)
        self.assertIn("medium_term_budget_tasks", queries.details)
        self.assertIn("medium_term_budget_work_packages", queries.details)
        self.assertIn("medium_term_load_intervals", queries.details)
        self.assertIn("medium_term_load_interval_delete", queries.details)
        self.assertIn("medium_term_weekly_loads", queries.details)
        self.assertIn("medium_term_weekly_load_delete", queries.details)
        self.assertIn("work_package_weekly_load_cas", queries.details)
        self.assertIn("asset_planning_scoped_requirements", queries.details)
        self.assertIn("asset_global_occupancy", queries.details)

    def test_database_preflight_requires_alembic_version_table(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "missing-migration.db"
            url = f"sqlite:///{database.as_posix()}"
            engine = create_sql_engine(url)
            try:
                Base.metadata.create_all(engine)
            finally:
                engine.dispose()

            with self.assertRaises(MigrationReadinessError):
                check_database_preflight(url)

    def test_database_preflight_accepts_exact_repository_head(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "ready.db"
            url = f"sqlite:///{database.as_posix()}"
            engine = create_sql_engine(url)
            try:
                Base.metadata.create_all(engine)
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE alembic_version "
                            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                        )
                    )
                    connection.execute(
                        text("INSERT INTO alembic_version(version_num) VALUES (:head)"),
                        {"head": _expected_alembic_head()},
                    )
            finally:
                engine.dispose()

            summary = check_database_preflight(url)

        self.assertEqual(summary["database"], "sqlite")
        self.assertEqual(summary["connectivity"], "ok")
        self.assertEqual(summary["alembic_revision"], _expected_alembic_head())

    def test_missing_sqlalchemy_dialect_is_reported_as_driver_error(self) -> None:
        with self.assertRaises(DriverReadinessError):
            check_database_preflight("resourceplanner_missing_dialect://localhost/db")

    def test_server_requirements_pin_validated_pyodbc(self) -> None:
        requirements = (ROOT / "requirements-server.txt").read_text(encoding="utf-8").casefold()
        self.assertIn("pyodbc==5.3.0", requirements)

    def test_backend_image_installs_microsoft_odbc_driver_18(self) -> None:
        dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8").casefold()
        self.assertIn("msodbcsql18", dockerfile)
        self.assertIn("accept_eula=y", dockerfile)


if __name__ == "__main__":
    unittest.main()
