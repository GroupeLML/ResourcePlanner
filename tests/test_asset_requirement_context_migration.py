from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def alembic_config(database_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


class AssetRequirementContextMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_request_and_shift_ad_hoc_rows(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "asset-context-upgrade.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0007_project_co_managers")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO asset_requirements (
                        id, project_id, origin, shift_id, workforce_request_id,
                        source_request_line_id, source_period_id, approval_revision_id,
                        approved_entry_key, slot_index, asset_type_id,
                        start_date, end_date, usage_hours, status
                    ) VALUES (
                        'AR-REQUEST-575', 'P-575', 'REQUEST', NULL, 'WR-575',
                        'RL-575', 'PERIOD-575', 'REV-575',
                        'ENTRY-575', 2, 'AT-575',
                        '2026-10-01', '2026-10-05', 8, 'Planifié'
                    )
                """))
                connection.execute(text("""
                    INSERT INTO asset_requirements (
                        id, project_id, origin, shift_id, workforce_request_id,
                        source_request_line_id, source_period_id, approval_revision_id,
                        approved_entry_key, slot_index, asset_type_id,
                        start_date, end_date, usage_hours, status
                    ) VALUES (
                        'AR-SHIFT-575', 'P-575', 'SHIFT_AD_HOC', 'SHIFT-575', NULL,
                        NULL, NULL, NULL, NULL, 0, 'AT-575',
                        '2026-10-02', '2026-10-02', NULL, 'Planifié'
                    )
                """))
                connection.execute(text("""
                    INSERT INTO asset_allocations (
                        id, asset_requirement_id, asset_id, operator_resource_id,
                        start_date, end_date, locked, source
                    ) VALUES (
                        'ALLOC-REQUEST-575', 'AR-REQUEST-575', 'ASSET-575', 'R-575',
                        '2026-10-03', '2026-10-03', 1, 'MANUAL'
                    )
                """))
                connection.execute(text("""
                    INSERT INTO asset_allocations (
                        id, asset_requirement_id, asset_id, operator_resource_id,
                        start_date, end_date, locked, source
                    ) VALUES (
                        'ALLOC-SHIFT-575', 'AR-SHIFT-575', 'ASSET-576', 'R-576',
                        '2026-10-02', '2026-10-02', 1, 'MANUAL'
                    )
                """))
            engine.dispose()

            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                inspector = inspect(engine)
                columns = {
                    row["name"]: row for row in inspector.get_columns("asset_requirements")
                }
                self.assertTrue(columns["project_id"]["nullable"])
                self.assertIn("resource_requirement_id", columns)
                self.assertIn("context_resource_id", columns)

                foreign_keys = {
                    tuple(row["constrained_columns"]): row["referred_table"]
                    for row in inspector.get_foreign_keys("asset_requirements")
                }
                self.assertEqual(
                    foreign_keys[("resource_requirement_id",)],
                    "resource_requirements",
                )
                self.assertEqual(
                    foreign_keys[("context_resource_id",)],
                    "resources",
                )
                indexes = {
                    row["name"] for row in inspector.get_indexes("asset_requirements")
                }
                self.assertIn("ix_asset_requirements_resource_requirement_id", indexes)
                self.assertIn("ix_asset_requirements_context_resource_id", indexes)
                self.assertIn("ux_asset_requirements_request_entry_slot", indexes)
                self.assertIn("ux_asset_requirements_shift_ad_hoc", indexes)

                with engine.connect() as connection:
                    request = connection.execute(text("""
                        SELECT id, project_id, origin, shift_id, workforce_request_id,
                               source_request_line_id, source_period_id,
                               approval_revision_id, approved_entry_key, slot_index,
                               start_date, end_date, usage_hours,
                               resource_requirement_id, context_resource_id
                        FROM asset_requirements WHERE id = 'AR-REQUEST-575'
                    """)).mappings().one()
                    self.assertEqual(request["id"], "AR-REQUEST-575")
                    self.assertEqual(request["origin"], "REQUEST")
                    self.assertEqual(request["project_id"], "P-575")
                    self.assertEqual(request["workforce_request_id"], "WR-575")
                    self.assertEqual(request["source_request_line_id"], "RL-575")
                    self.assertEqual(request["source_period_id"], "PERIOD-575")
                    self.assertEqual(request["approval_revision_id"], "REV-575")
                    self.assertEqual(request["approved_entry_key"], "ENTRY-575")
                    self.assertEqual(request["slot_index"], 2)
                    self.assertEqual(str(request["start_date"]), "2026-10-01")
                    self.assertEqual(str(request["end_date"]), "2026-10-05")
                    self.assertIsNone(request["resource_requirement_id"])
                    self.assertIsNone(request["context_resource_id"])

                    shift = connection.execute(text("""
                        SELECT id, project_id, origin, shift_id,
                               resource_requirement_id, context_resource_id
                        FROM asset_requirements WHERE id = 'AR-SHIFT-575'
                    """)).mappings().one()
                    self.assertEqual(shift["id"], "AR-SHIFT-575")
                    self.assertEqual(shift["origin"], "SHIFT_AD_HOC")
                    self.assertEqual(shift["shift_id"], "SHIFT-575")
                    self.assertEqual(shift["project_id"], "P-575")
                    self.assertIsNone(shift["resource_requirement_id"])
                    self.assertIsNone(shift["context_resource_id"])

                    allocations = connection.execute(text("""
                        SELECT id, asset_requirement_id, operator_resource_id,
                               start_date, end_date
                        FROM asset_allocations
                        ORDER BY id
                    """)).mappings().all()
                    self.assertEqual(
                        [row["id"] for row in allocations],
                        ["ALLOC-REQUEST-575", "ALLOC-SHIFT-575"],
                    )
                    self.assertEqual(
                        allocations[0]["operator_resource_id"],
                        "R-575",
                    )
                    self.assertEqual(
                        str(allocations[0]["start_date"]),
                        "2026-10-03",
                    )
                    self.assertEqual(
                        str(allocations[0]["end_date"]),
                        "2026-10-03",
                    )
            finally:
                engine.dispose()

    def test_downgrade_to_0007_preserves_historical_origins(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "asset-context-downgrade.db"
            config = alembic_config(database_path)
            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO asset_requirements (
                        id, project_id, origin, shift_id, workforce_request_id,
                        source_request_line_id, approved_entry_key,
                        asset_type_id, start_date, end_date, status
                    ) VALUES
                    ('AR-REQ-DOWN', 'P-1', 'REQUEST', NULL, 'WR-1', 'RL-1',
                     'ENTRY-1', 'AT-1', '2026-10-01', '2026-10-01', 'À affecter'),
                    ('AR-SHIFT-DOWN', 'P-1', 'SHIFT_AD_HOC', 'SHIFT-1', NULL, NULL,
                     NULL, 'AT-1', '2026-10-01', '2026-10-01', 'Planifié')
                """))
            engine.dispose()

            command.downgrade(config, "0007_project_co_managers")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                inspector = inspect(engine)
                columns = {
                    row["name"]: row for row in inspector.get_columns("asset_requirements")
                }
                self.assertFalse(columns["project_id"]["nullable"])
                self.assertNotIn("resource_requirement_id", columns)
                self.assertNotIn("context_resource_id", columns)
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(
                            text("SELECT COUNT(*) FROM asset_requirements")
                        ).scalar_one(),
                        2,
                    )
            finally:
                engine.dispose()

    def test_downgrade_refuses_each_new_origin(self) -> None:
        shapes = {
            "PROJECT_DIRECT": {
                "project_id": "P-1",
                "resource_requirement_id": None,
                "context_resource_id": None,
            },
            "SEGMENT": {
                "project_id": "P-1",
                "resource_requirement_id": "RR-1",
                "context_resource_id": None,
            },
            "RESOURCE_PERIOD": {
                "project_id": None,
                "resource_requirement_id": None,
                "context_resource_id": "R-1",
            },
        }
        for origin, shape in shapes.items():
            with self.subTest(origin=origin), TemporaryDirectory() as directory:
                database_path = Path(directory) / f"{origin.lower()}.db"
                config = alembic_config(database_path)
                command.upgrade(config, "head")
                engine = create_engine(f"sqlite:///{database_path.as_posix()}")
                with engine.begin() as connection:
                    connection.execute(
                        text("""
                            INSERT INTO asset_requirements (
                                id, project_id, origin, shift_id,
                                resource_requirement_id, context_resource_id,
                                workforce_request_id, source_request_line_id,
                                source_period_id, approval_revision_id,
                                approved_entry_key, asset_type_id,
                                start_date, end_date, status
                            ) VALUES (
                                :id, :project_id, :origin, NULL,
                                :resource_requirement_id, :context_resource_id,
                                NULL, NULL, NULL, NULL, NULL, 'AT-1',
                                '2026-10-01', '2026-10-01', 'Planifié'
                            )
                        """),
                        {
                            "id": f"AR-{origin}",
                            "origin": origin,
                            **shape,
                        },
                    )
                engine.dispose()

                with self.assertRaisesRegex(RuntimeError, origin):
                    command.downgrade(config, "0007_project_co_managers")


if __name__ == "__main__":
    unittest.main()
