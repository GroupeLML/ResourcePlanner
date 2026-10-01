from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def _config(database_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


class WorkPackageResourceClassMigrationTests(unittest.TestCase):
    def test_additive_backfill_is_prudent_and_preserves_existing_work_package_data(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "work-package-class.db"
            config = _config(database_path)
            command.upgrade(config, "0002_work_package_weekly_loads")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            INSERT INTO resource_class_configs
                                (code, label, active)
                            VALUES
                                ('ACTIVE', 'Active', 1),
                                ('INACTIVE', 'Inactive', 0)
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO projects (id, number, name)
                            VALUES
                                ('P1', 'P-1', 'Projet 1'),
                                ('P2', 'P-2', 'Projet 2')
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO task_catalog_items
                                (id, project_number, task_code, label, resource_class_code)
                            VALUES
                                ('T-VALID', 'P-1', '210', 'Valide', 'ACTIVE'),
                                ('T-INACTIVE', 'P-1', '211', 'Inactive', 'INACTIVE'),
                                ('T-NOCLASS', 'P-1', '212', 'Sans classe', NULL),
                                ('T-MISSINGCLASS', 'P-1', '213', 'Classe absente', 'MISSING'),
                                ('T-P2', 'P-2', '214', 'Autre projet', 'ACTIVE')
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO work_packages
                                (id, project_id, task_catalog_item_id, code, name,
                                 start_date, end_date, planned_hours, status,
                                 weekly_load_origin)
                            VALUES
                                ('WP-VALID', 'P1', 'T-VALID', 'ACTIVE', 'Valide',
                                 '2026-09-14', '2026-09-20', 8, 'active', 'MANUAL'),
                                ('WP-INACTIVE', 'P1', 'T-INACTIVE', 'I', 'Inactive',
                                 NULL, NULL, 12, 'planned', NULL),
                                ('WP-NOCLASS', 'P1', 'T-NOCLASS', 'N', 'Sans classe',
                                 NULL, NULL, 16, 'planned', NULL),
                                ('WP-MISSINGCLASS', 'P1', 'T-MISSINGCLASS', 'M', 'Classe absente',
                                 NULL, NULL, 20, 'planned', NULL),
                                ('WP-MISMATCH', 'P1', 'T-P2', 'X', 'Projet incohérent',
                                 NULL, NULL, 24, 'planned', NULL),
                                ('WP-MISSINGTASK', 'P1', 'T-ABSENT', 'Y', 'Tâche absente',
                                 NULL, NULL, 28, 'planned', NULL),
                                ('WP-NOTASK', 'P1', NULL, 'ACTIVE', 'Sans tâche',
                                 NULL, NULL, 32, 'planned', NULL)
                            """
                        )
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO work_package_weekly_loads
                                (work_package_id, week_start, hours)
                            VALUES ('WP-VALID', '2026-09-14', 8)
                            """
                        )
                    )
                    before_packages = connection.execute(
                        text(
                            """
                            SELECT id, task_catalog_item_id, code, name, start_date,
                                   end_date, planned_hours, status, weekly_load_origin
                            FROM work_packages
                            ORDER BY id
                            """
                        )
                    ).all()
                    before_loads = connection.execute(
                        text(
                            """
                            SELECT work_package_id, week_start, hours
                            FROM work_package_weekly_loads
                            ORDER BY work_package_id, week_start
                            """
                        )
                    ).all()
            finally:
                engine.dispose()

            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                inspector = inspect(engine)
                columns = {
                    column["name"]: column for column in inspector.get_columns("work_packages")
                }
                self.assertIn("resource_class_code", columns)
                self.assertTrue(columns["resource_class_code"]["nullable"])
                self.assertIn(
                    (
                        ("resource_class_code",),
                        "resource_class_configs",
                        ("code",),
                    ),
                    {
                        (
                            tuple(item["constrained_columns"]),
                            item["referred_table"],
                            tuple(item["referred_columns"]),
                        )
                        for item in inspector.get_foreign_keys("work_packages")
                    },
                )

                with engine.connect() as connection:
                    classes = dict(
                        connection.execute(
                            text(
                                """
                                SELECT id, resource_class_code
                                FROM work_packages
                                ORDER BY id
                                """
                            )
                        ).all()
                    )
                    self.assertEqual(classes["WP-VALID"], "ACTIVE")
                    self.assertEqual(classes["WP-INACTIVE"], "INACTIVE")
                    self.assertIsNone(classes["WP-NOCLASS"])
                    self.assertIsNone(classes["WP-MISSINGCLASS"])
                    self.assertIsNone(classes["WP-MISMATCH"])
                    self.assertIsNone(classes["WP-MISSINGTASK"])
                    self.assertIsNone(classes["WP-NOTASK"])

                    after_packages = connection.execute(
                        text(
                            """
                            SELECT id, task_catalog_item_id, code, name, start_date,
                                   end_date, planned_hours, status, weekly_load_origin
                            FROM work_packages
                            ORDER BY id
                            """
                        )
                    ).all()
                    after_loads = connection.execute(
                        text(
                            """
                            SELECT work_package_id, week_start, hours
                            FROM work_package_weekly_loads
                            ORDER BY work_package_id, week_start
                            """
                        )
                    ).all()
                    self.assertEqual(after_packages, before_packages)
                    self.assertEqual(after_loads, before_loads)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
