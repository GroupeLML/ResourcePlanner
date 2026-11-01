from __future__ import annotations

import os
from pathlib import Path
import unittest

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


def _alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
    return config


@unittest.skipUnless(
    IS_MSSQL,
    "Validation #742 réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class ResourceResidenceCitySqlServerMigrationTests(unittest.TestCase):
    def test_0027_to_0028_preserves_resource_and_round_trips_residence_city(self) -> None:
        config = _alembic_config()
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
