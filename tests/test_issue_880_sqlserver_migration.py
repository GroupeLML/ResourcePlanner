from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.infrastructure.sql import (
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)


def synthetic_phone(last_four: str = "0100") -> str:
    return "514" + "-" + "555" + "-" + last_four


@unittest.skipUnless(
    str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").startswith("mssql+"),
    "requires disposable live SQL Server",
)
class Issue880SqlServerMigrationTests(unittest.TestCase):
    def test_pre_0037_request_survives_with_opt_out_defaults_and_round_trips(self) -> None:
        database_url = str(os.environ["RESOURCEPLANNER_SQLSERVER_TEST_URL"])
        with patch.dict(os.environ, {"RESOURCEPLANNER_DATABASE_URL": database_url}):
            config = Config("alembic.ini")
            config.set_main_option("sqlalchemy.url", database_url)

            command.downgrade(config, "0036_communication_message_body_html")

            engine = create_sql_engine(database_url)
            try:
                before = {
                    column["name"]
                    for column in inspect(engine).get_columns("workforce_requests")
                }
                self.assertNotIn("include_project_manager_contact_phone", before)
                self.assertNotIn("project_manager_contact_phone", before)
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO projects (id, number, name, status) "
                            "VALUES (:id, :number, :name, :status)"
                        ),
                        {
                            "id": "ISSUE880-SQL-PROJECT",
                            "number": "ISSUE880-SQL",
                            "name": "Issue 880 SQL Server migration",
                            "status": "Actif",
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO workforce_requests "
                            "(id, legacy_demand_number, project_id, description) "
                            "VALUES (:id, :number, :project_id, :description)"
                        ),
                        {
                            "id": "ISSUE880-SQL-REQUEST",
                            "number": "DMO-880-SQL",
                            "project_id": "ISSUE880-SQL-PROJECT",
                            "description": "Existing pre-0037 request",
                        },
                    )
            finally:
                engine.dispose()

            command.upgrade(config, "head")

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                after = {
                    column["name"]
                    for column in inspect(engine).get_columns("workforce_requests")
                }
                self.assertIn("include_project_manager_contact_phone", after)
                self.assertIn("project_manager_contact_phone", after)

                with factory.begin() as session:
                    request = session.get(
                        WorkforceRequest,
                        "ISSUE880-SQL-REQUEST",
                    )
                    assert request is not None
                    self.assertFalse(
                        request.include_project_manager_contact_phone
                    )
                    self.assertIsNone(request.project_manager_contact_phone)
                    request.include_project_manager_contact_phone = True
                    request.project_manager_contact_phone = synthetic_phone("0880")

                with factory() as session:
                    request = session.get(
                        WorkforceRequest,
                        "ISSUE880-SQL-REQUEST",
                    )
                    assert request is not None
                    self.assertTrue(
                        request.include_project_manager_contact_phone
                    )
                    self.assertEqual(
                        request.project_manager_contact_phone,
                        synthetic_phone("0880"),
                    )
            finally:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "DELETE FROM workforce_requests "
                            "WHERE id = 'ISSUE880-SQL-REQUEST'"
                        )
                    )
                    connection.execute(
                        text(
                            "DELETE FROM projects "
                            "WHERE id = 'ISSUE880-SQL-PROJECT'"
                        )
                    )
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
