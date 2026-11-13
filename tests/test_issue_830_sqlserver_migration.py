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


class Issue830SqlServerMigrationTests(unittest.TestCase):
    def test_conflicting_ambient_database_url_is_never_used_for_downgrade(self) -> None:
        # No database is contacted in this guard regression. The synthetic
        # alternate URL is deliberately invalid and must never be opened.
        disposable = "mssql+pyodbc://disposable.invalid/ResourcePlanner830"
        alternate = "mssql+pyodbc://unintended.invalid/NEVER_TOUCH"
        original = os.environ.get("RESOURCEPLANNER_DATABASE_URL")
        with patch.dict(
            os.environ,
            {
                "RESOURCEPLANNER_SQLSERVER_TEST_URL": disposable,
                "RESOURCEPLANNER_DATABASE_URL": alternate,
            },
        ):
            def inspect_binding(config, revision):
                self.assertEqual(revision, "0034_request_shift_survivor_protections")
                self.assertEqual(os.environ["RESOURCEPLANNER_DATABASE_URL"], disposable)
                self.assertEqual(config.get_main_option("sqlalchemy.url"), disposable)
                raise RuntimeError("synthetic stop before database access")

            with patch("tests.test_issue_830_sqlserver_migration.command.downgrade",
                       side_effect=inspect_binding) as downgrade:
                with patch("tests.test_issue_830_sqlserver_migration.create_sql_engine") as engine:
                    with self.assertRaisesRegex(RuntimeError, "synthetic stop"):
                        self.test_pre_0035_existing_request_survives_and_contact_round_trips()
                    downgrade.assert_called_once()
                    engine.assert_not_called()
            self.assertEqual(os.environ["RESOURCEPLANNER_DATABASE_URL"], alternate)
        self.assertEqual(os.environ.get("RESOURCEPLANNER_DATABASE_URL"), original)

    def test_binding_restores_absent_ambient_value_after_exception(self) -> None:
        disposable = "mssql+pyodbc://disposable.invalid/ResourcePlanner830"
        with patch.dict(os.environ, {"RESOURCEPLANNER_SQLSERVER_TEST_URL": disposable}):
            with patch.dict(os.environ, {}, clear=True):
                os.environ["RESOURCEPLANNER_SQLSERVER_TEST_URL"] = disposable
                with patch("tests.test_issue_830_sqlserver_migration.command.downgrade",
                           side_effect=RuntimeError("synthetic stop")):
                    with self.assertRaisesRegex(RuntimeError, "synthetic stop"):
                        self.test_pre_0035_existing_request_survives_and_contact_round_trips()
                self.assertNotIn("RESOURCEPLANNER_DATABASE_URL", os.environ)

    def test_pre_0035_existing_request_survives_and_contact_round_trips(self) -> None:
        database_url = os.environ.get("RESOURCEPLANNER_SQLSERVER_TEST_URL")
        if not database_url or not database_url.startswith("mssql+"):
            self.skipTest("requires disposable live SQL Server")

        # migrations/env.py intentionally gives this environment variable
        # precedence. Bind it for the ENTIRE destructive test scope, not just
        # Alembic configuration, and restore even on exceptions.
        with patch.dict(os.environ, {"RESOURCEPLANNER_DATABASE_URL": database_url}):
            config = Config("alembic.ini")
            config.set_main_option("sqlalchemy.url", database_url)
    
            # The live CI database is disposable. Rewind only the additive #830
            # migration so this test exercises an actual pre-0035 schema and row.
            command.downgrade(config, "0034_request_shift_survivor_protections")
    
            engine = create_sql_engine(database_url)
            try:
                columns_before = {
                    column["name"] for column in inspect(engine).get_columns("workforce_requests")
                }
                self.assertNotIn("site_contact_name", columns_before)
                self.assertNotIn("site_contact_phone", columns_before)
    
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO projects (id, number, name, status) "
                            "VALUES (:id, :number, :name, :status)"
                        ),
                        {
                            "id": "ISSUE830-SQL-PROJECT",
                            "number": "ISSUE830-SQL",
                            "name": "Issue 830 SQL Server migration",
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
                            "id": "ISSUE830-SQL-REQUEST",
                            "number": "DMO-830-SQL",
                            "project_id": "ISSUE830-SQL-PROJECT",
                            "description": "Existing pre-0035 request",
                        },
                    )
            finally:
                engine.dispose()
    
            command.upgrade(config, "head")
    
            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                columns_after = {
                    column["name"] for column in inspect(engine).get_columns("workforce_requests")
                }
                self.assertIn("site_contact_name", columns_after)
                self.assertIn("site_contact_phone", columns_after)
    
                with factory.begin() as session:
                    request = session.get(WorkforceRequest, "ISSUE830-SQL-REQUEST")
                    self.assertIsNotNone(request)
                    assert request is not None
                    self.assertEqual(request.description, "Existing pre-0035 request")
                    self.assertIsNone(request.site_contact_name)
                    self.assertIsNone(request.site_contact_phone)
                    request.site_contact_name = "Jean Tremblay"
                    request.site_contact_phone = "555-0830"
    
                with factory() as session:
                    request = session.get(WorkforceRequest, "ISSUE830-SQL-REQUEST")
                    self.assertIsNotNone(request)
                    assert request is not None
                    self.assertEqual(request.site_contact_name, "Jean Tremblay")
                    self.assertEqual(request.site_contact_phone, "555-0830")
            finally:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "DELETE FROM workforce_requests "
                            "WHERE id = 'ISSUE830-SQL-REQUEST'"
                        )
                    )
                    connection.execute(
                        text("DELETE FROM projects WHERE id = 'ISSUE830-SQL-PROJECT'")
                    )
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
