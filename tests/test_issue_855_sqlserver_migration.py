from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.infrastructure.sql import create_session_factory, create_sql_engine
from app.infrastructure.sql.communication_models import CommunicationMessageRow


class Issue855SqlServerMigrationTests(unittest.TestCase):
    def test_pre_0036_message_survives_and_html_round_trips(self) -> None:
        database_url = os.environ.get("RESOURCEPLANNER_SQLSERVER_TEST_URL")
        if not database_url or not database_url.startswith("mssql+"):
            self.skipTest("requires disposable live SQL Server")

        with patch.dict(os.environ, {"RESOURCEPLANNER_DATABASE_URL": database_url}):
            config = Config("alembic.ini")
            config.set_main_option("sqlalchemy.url", database_url)

            command.downgrade(config, "0035_workforce_request_site_contact")

            engine = create_sql_engine(database_url)
            try:
                columns_before = {
                    column["name"]
                    for column in inspect(engine).get_columns("communication_messages")
                }
                self.assertNotIn("body_html", columns_before)

                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO communication_batches "
                            "(id, week_start, kind, snapshot_fingerprint, status, "
                            "prepared_at, drafts_created_count, model_version) "
                            "VALUES (:id, :week_start, :kind, :fingerprint, :status, "
                            "CURRENT_TIMESTAMP, 0, :model_version)"
                        ),
                        {
                            "id": "ISSUE855-SQL-BATCH",
                            "week_start": "2026-09-21",
                            "kind": "project_confirmation",
                            "fingerprint": "f" * 64,
                            "status": "PREPARED",
                            "model_version": "project_v2",
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO communication_messages "
                            "(id, batch_id, audience, recipient_id, subject, body, included, approvable) "
                            "VALUES (:id, :batch_id, :audience, :recipient_id, :subject, :body, 1, 1)"
                        ),
                        {
                            "id": "ISSUE855-SQL-MESSAGE",
                            "batch_id": "ISSUE855-SQL-BATCH",
                            "audience": "project",
                            "recipient_id": "P1",
                            "subject": "Sujet historique",
                            "body": "Corps historique",
                        },
                    )
            finally:
                engine.dispose()

            command.upgrade(config, "head")

            engine = create_sql_engine(database_url)
            factory = create_session_factory(engine)
            try:
                columns_after = {
                    column["name"]
                    for column in inspect(engine).get_columns("communication_messages")
                }
                self.assertIn("body_html", columns_after)

                with factory.begin() as session:
                    message = session.get(
                        CommunicationMessageRow,
                        "ISSUE855-SQL-MESSAGE",
                    )
                    self.assertIsNotNone(message)
                    assert message is not None
                    self.assertEqual(message.body, "Corps historique")
                    self.assertIsNone(message.body_html)
                    message.body_html = "<div>Corps HTML durable</div>"

                with factory() as session:
                    message = session.get(
                        CommunicationMessageRow,
                        "ISSUE855-SQL-MESSAGE",
                    )
                    self.assertIsNotNone(message)
                    assert message is not None
                    self.assertEqual(message.body, "Corps historique")
                    self.assertEqual(
                        message.body_html,
                        "<div>Corps HTML durable</div>",
                    )
            finally:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "DELETE FROM communication_messages "
                            "WHERE id = 'ISSUE855-SQL-MESSAGE'"
                        )
                    )
                    connection.execute(
                        text(
                            "DELETE FROM communication_batches "
                            "WHERE id = 'ISSUE855-SQL-BATCH'"
                        )
                    )
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
