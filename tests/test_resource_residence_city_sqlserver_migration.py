from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.infrastructure.sql import (
    SqlResourceAdminRepository,
    create_session_factory,
    create_sql_engine,
)


ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")
PREVIOUS_REVISION = "0027_placement_not_before_date"
HEAD_REVISION = "0028_resource_residence_city"
RESOURCE_ID = "R-742-SQLSERVER-MIGRATION"


def _alembic_config(database_url: str = DATABASE_URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


@contextmanager
def _migration_database_scope(database_url: str):
    with patch.dict(
        os.environ,
        {"RESOURCEPLANNER_DATABASE_URL": database_url},
        clear=False,
    ):
        yield


class ResourceResidenceCityMigrationConfigurationTests(unittest.TestCase):
    def test_conflicting_ambient_database_url_is_overridden_through_cleanup(self) -> None:
        test_url = (
            "mssql+pyodbc://test-user:test-password@test-host/TestDatabase"
            "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
        )
        ambient_url = (
            "mssql+pyodbc://runtime-user:runtime-password@runtime-host/RuntimeDatabase"
            "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
        )
        observed_cleanup_urls: list[str | None] = []

        with patch.dict(
            os.environ,
            {"RESOURCEPLANNER_DATABASE_URL": ambient_url},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                with _migration_database_scope(test_url):
                    self.assertEqual(
                        os.environ.get("RESOURCEPLANNER_DATABASE_URL"),
                        test_url,
                    )
                    config = _alembic_config(test_url)
                    self.assertEqual(
                        config.get_main_option("sqlalchemy.url"),
                        test_url.replace("%", "%%"),
                    )
                    try:
                        raise RuntimeError("injected failure")
                    finally:
                        observed_cleanup_urls.append(
                            os.environ.get("RESOURCEPLANNER_DATABASE_URL")
                        )

            self.assertEqual(observed_cleanup_urls, [test_url])
            self.assertEqual(
                os.environ.get("RESOURCEPLANNER_DATABASE_URL"),
                ambient_url,
            )


@unittest.skipUnless(
    IS_MSSQL,
    "Validation #742 réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class ResourceResidenceCitySqlServerMigrationTests(unittest.TestCase):
    def test_0027_to_0028_preserves_resource_and_round_trips_residence_city(self) -> None:
        with _migration_database_scope(DATABASE_URL):
            config = _alembic_config(DATABASE_URL)
            engine = create_sql_engine(DATABASE_URL)
            factory = create_session_factory(engine)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text("DELETE FROM resources WHERE id = :resource_id"),
                        {"resource_id": RESOURCE_ID},
                    )

                command.downgrade(config, PREVIOUS_REVISION)

                columns_before = {
                    column["name"]
                    for column in inspect(engine).get_columns("resources")
                }
                self.assertNotIn("residence_city", columns_before)

                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO resources "
                            "(id, name, active, erp_active, sort_order) "
                            "VALUES (:id, :name, 1, 1, 0)"
                        ),
                        {
                            "id": RESOURCE_ID,
                            "name": "Ressource migration SQL Server 742",
                        },
                    )

                command.upgrade(config, HEAD_REVISION)

                columns_after = {
                    column["name"]
                    for column in inspect(engine).get_columns("resources")
                }
                self.assertIn("residence_city", columns_after)

                with engine.connect() as connection:
                    row = connection.execute(
                        text(
                            "SELECT id, residence_city FROM resources "
                            "WHERE id = :resource_id"
                        ),
                        {"resource_id": RESOURCE_ID},
                    ).one()
                    self.assertEqual(row.id, RESOURCE_ID)
                    self.assertIsNone(row.residence_city)

                with factory.begin() as session:
                    SqlResourceAdminRepository(session).update_resource(
                        RESOURCE_ID,
                        {"residence_city": "Chambly"},
                    )
                with factory() as session:
                    configured = SqlResourceAdminRepository(session).get_resource(
                        RESOURCE_ID
                    )
                    self.assertIsNotNone(configured)
                    self.assertEqual(configured.residence_city, "Chambly")

                with factory.begin() as session:
                    SqlResourceAdminRepository(session).update_resource(
                        RESOURCE_ID,
                        {"residence_city": "   "},
                    )
                with factory() as session:
                    cleared = SqlResourceAdminRepository(session).get_resource(
                        RESOURCE_ID
                    )
                    self.assertIsNotNone(cleared)
                    self.assertIsNone(cleared.residence_city)
            finally:
                try:
                    command.upgrade(config, "head")
                finally:
                    try:
                        with engine.begin() as connection:
                            connection.execute(
                                text("DELETE FROM resources WHERE id = :resource_id"),
                                {"resource_id": RESOURCE_ID},
                            )
                    finally:
                        engine.dispose()


if __name__ == "__main__":
    unittest.main()
