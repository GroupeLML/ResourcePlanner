from __future__ import annotations

from io import StringIO
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect, text

from app.infrastructure.sql import Base


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
VERSIONS = MIGRATIONS / "versions"
BASELINE_FILE = VERSIONS / "0001_v2_production_baseline.py"
BASELINE_REVISION = "v2_production_baseline"
HEAD_REVISION = "0010_operational_responsibility_context"


def alembic_config(database_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def offline_config(url: str, output: StringIO) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def _model_foreign_keys(table) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    result = set()
    for constraint in table.foreign_key_constraints:
        elements = tuple(constraint.elements)
        result.add(
            (
                tuple(element.parent.name for element in elements),
                elements[0].column.table.name,
                tuple(element.column.name for element in elements),
            )
        )
    return result


def _database_foreign_keys(inspector, table_name: str) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    return {
        (
            tuple(item["constrained_columns"]),
            item["referred_table"],
            tuple(item["referred_columns"]),
        )
        for item in inspector.get_foreign_keys(table_name)
    }


class SqlMigrationTests(unittest.TestCase):
    def test_production_baseline_is_self_contained_and_additive_head_is_linear(self) -> None:
        versions = sorted(path.name for path in VERSIONS.glob("*.py"))
        self.assertEqual(
            versions,
            [
                BASELINE_FILE.name,
                "0002_work_package_weekly_loads.py",
                "0003_work_package_resource_class.py",
                "0004_asset_approval_authority.py",
                "0005_acumatica_project_task_sync_runs.py",
                "0006_asset_requirement_origins.py",
                "0007_project_co_managers.py",
                "0008_asset_requirement_contexts.py",
                "0009_work_package_load_intervals.py",
                "0010_operational_responsibility_context.py",
            ],
        )

        source = BASELINE_FILE.read_text(encoding="utf-8")
        self.assertIn(f"revision: str = '{BASELINE_REVISION}'", source)
        self.assertIn("down_revision: Union[str, Sequence[str], None] = None", source)
        self.assertNotIn("app.infrastructure.sql", source)
        self.assertNotIn("from app", source)
        self.assertNotIn("import app", source)

        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(MIGRATIONS))
        script = ScriptDirectory.from_config(config)
        self.assertEqual(script.get_heads(), [HEAD_REVISION])
        self.assertEqual(
            [revision.revision for revision in script.walk_revisions()],
            [
                HEAD_REVISION,
                "0009_work_package_load_intervals",
                "0008_asset_requirement_contexts",
                "0007_project_co_managers",
                "0006_asset_requirement_origins",
                "0005_task_sync_runs",
                "0004_asset_approval_authority",
                "0003_work_package_resource_class",
                "0002_work_package_weekly_loads",
                BASELINE_REVISION,
            ],
        )

    def test_work_package_load_interval_migration_preserves_weekly_intent(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "work-package-load-intervals.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0008_asset_requirement_contexts")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO projects (id, number, name) "
                        "VALUES ('P-591', 'P-591', 'Projet 591')"
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO work_packages (
                            id, project_id, version, name, start_date, end_date,
                            planned_hours, status, weekly_load_origin
                        ) VALUES (
                            'WP-591-MIG', 'P-591', 1, 'Historique',
                            '2026-09-30', '2026-10-06', 10.00, 'closed', 'AUTO'
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO work_package_weekly_loads (
                            work_package_id, week_start, hours
                        ) VALUES
                            ('WP-591-MIG', '2026-09-28', 5.00),
                            ('WP-591-MIG', '2026-10-05', 5.00)
                        """
                    )
                )
            engine.dispose()

            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                with engine.connect() as connection:
                    terminal = connection.execute(
                        text(
                            "SELECT terminal_status FROM work_packages "
                            "WHERE id = 'WP-591-MIG'"
                        )
                    ).scalar_one()
                    intervals = connection.execute(
                        text(
                            """
                            SELECT start_date, end_date, hours, origin
                            FROM work_package_load_intervals
                            WHERE work_package_id = 'WP-591-MIG'
                            ORDER BY start_date
                            """
                        )
                    ).all()

                self.assertEqual(terminal, "closed")
                self.assertEqual(
                    [
                        (str(row.start_date), str(row.end_date), Decimal(str(row.hours)), row.origin)
                        for row in intervals
                    ],
                    [
                        ("2026-09-30", "2026-10-04", Decimal("5.00"), "LEGACY_AUTO"),
                        ("2026-10-05", "2026-10-06", Decimal("5.00"), "LEGACY_AUTO"),
                    ],
                )
            finally:
                engine.dispose()

    def test_work_package_load_interval_migration_preflight_rejects_origin_without_rows(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "work-package-load-preflight.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0008_asset_requirement_contexts")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO projects (id, number, name) "
                        "VALUES ('P-591-PREFLIGHT', 'P-591-PREFLIGHT', 'Projet')"
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO work_packages (
                            id, project_id, version, name, start_date, end_date,
                            planned_hours, status, weekly_load_origin
                        ) VALUES (
                            'WP-591-PREFLIGHT', 'P-591-PREFLIGHT', 1, 'Anomalie',
                            '2026-10-01', '2026-10-02', 8.00, 'planned', 'MANUAL'
                        )
                        """
                    )
                )
            engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "without persisted rows"):
                command.upgrade(config, "head")

    def test_project_co_manager_migration_is_additive_and_starts_empty(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "project-co-managers.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0006_asset_requirement_origins")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO projects (id, number, name) "
                        "VALUES ('P-573-A', 'P-573-A', 'Projet historique')"
                    )
                )
            engine.dispose()

            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                inspector = inspect(engine)
                self.assertIn("project_co_managers", inspector.get_table_names())
                self.assertIn("project_manager_audit", inspector.get_table_names())
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(
                            text(
                                "SELECT co_managers_version FROM projects "
                                "WHERE id = 'P-573-A'"
                            )
                        ).scalar_one(),
                        1,
                    )
                    self.assertEqual(
                        connection.execute(
                            text("SELECT COUNT(*) FROM project_co_managers")
                        ).scalar_one(),
                        0,
                    )
                    self.assertEqual(
                        connection.execute(
                            text("SELECT COUNT(*) FROM project_manager_audit")
                        ).scalar_one(),
                        0,
                    )
            finally:
                engine.dispose()

    def test_asset_requirement_origin_migration_preserves_historical_rows(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "asset-requirement-origin.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0005_task_sync_runs")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO asset_requirements (
                            id,
                            project_id,
                            workforce_request_id,
                            source_request_line_id,
                            approved_entry_key,
                            slot_index,
                            asset_type_id,
                            start_date,
                            end_date,
                            status
                        ) VALUES (
                            'AR-HIST',
                            'P-HIST',
                            'WR-HIST',
                            'RL-HIST',
                            'ENTRY-HIST',
                            0,
                            'AT-HIST',
                            '2026-09-01',
                            '2026-09-01',
                            'À affecter'
                        )
                        """
                    )
                )
            engine.dispose()

            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                with engine.connect() as connection:
                    row = connection.execute(
                        text(
                            "SELECT id, origin, shift_id, resource_requirement_id, "
                            "context_resource_id, project_id, start_date, end_date "
                            "FROM asset_requirements WHERE id = 'AR-HIST'"
                        )
                    ).one()
                    self.assertEqual(row.id, "AR-HIST")
                    self.assertEqual(row.origin, "REQUEST")
                    self.assertIsNone(row.shift_id)
                    self.assertIsNone(row.resource_requirement_id)
                    self.assertIsNone(row.context_resource_id)
                    self.assertEqual(row.project_id, "P-HIST")
                    self.assertEqual(str(row.start_date), "2026-09-01")
                    self.assertEqual(str(row.end_date), "2026-09-01")
            finally:
                engine.dispose()

    def test_asset_requirement_origin_downgrade_refuses_ad_hoc_data(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "asset-requirement-origin-downgrade.db"
            config = alembic_config(database_path)
            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO asset_requirements (
                            id,
                            project_id,
                            origin,
                            shift_id,
                            asset_type_id,
                            start_date,
                            end_date,
                            status
                        ) VALUES (
                            'AR-AD-HOC',
                            'P-AD-HOC',
                            'SHIFT_AD_HOC',
                            'SHIFT-AD-HOC',
                            'AT-AD-HOC',
                            '2026-09-01',
                            '2026-09-01',
                            'Planifié'
                        )
                        """
                    )
                )
            engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "SHIFT_AD_HOC"):
                command.downgrade(config, "0005_task_sync_runs")

    def test_operational_responsibility_context_migration_preserves_historical_provenance(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "operational-responsibility-context.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0009_work_package_load_intervals")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO business_contacts (id, display_name)
                        VALUES
                            ('C-REQ-594', 'Responsable demande'),
                            ('C-TASK-594', 'Responsable tâche'),
                            ('C-PM-594', 'Chargé ERP')
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO projects (
                            id, number, name, project_manager_external_id
                        ) VALUES
                            ('P-594', 'P-594', 'Projet 594', 'EMP-PM-594'),
                            ('P-OTHER-594', 'P-OTHER-594', 'Autre projet', NULL)
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO app_users (
                            id, display_name, employee_external_id,
                            business_contact_id, roles_json
                        ) VALUES (
                            'U-PM-594', 'Chargé ERP', 'EMP-PM-594',
                            'C-PM-594', '[]'
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO task_catalog_items (
                            id, project_number, task_code, label,
                            operational_responsible_contact_id
                        ) VALUES
                            (
                                'T-594', 'P-594', '100', 'Tâche 100',
                                'C-TASK-594'
                            ),
                            (
                                'T-WRONG-594', 'P-OTHER-594', '101', 'Tâche autre projet',
                                'C-TASK-594'
                            )
                        """
                    )
                )
                for request_id in (
                    "W-REQ-594",
                    "W-TASK-594",
                    "W-PM-594",
                    "W-BROKEN-594",
                    "W-UNKNOWN-594",
                ):
                    connection.execute(
                        text(
                            """
                            INSERT INTO workforce_requests (id, project_id)
                            VALUES (:request_id, 'P-594')
                            """
                        ),
                        {"request_id": request_id},
                    )
                connection.execute(
                    text(
                        """
                        INSERT INTO resource_requirements (
                            id, project_id, workforce_request_id,
                            approved_operational_responsible_override_contact_id,
                            approved_contact_context_status,
                            start_date, end_date, planned_hours, origin
                        ) VALUES (
                            'R-CAPTURED-594', 'P-594', 'W-REQ-594',
                            'C-REQ-594', 'CAPTURED',
                            '2026-10-01', '2026-10-01', 8.00, 'REQUEST'
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO resource_requirements (
                            id, project_id, workforce_request_id,
                            approved_task_catalog_item_id,
                            approved_contact_context_status,
                            start_date, end_date, planned_hours, origin
                        ) VALUES (
                            'R-TASK-594', 'P-594', 'W-TASK-594',
                            'T-594', 'CAPTURED',
                            '2026-10-01', '2026-10-01', 8.00, 'REQUEST'
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO resource_requirements (
                            id, project_id, workforce_request_id,
                            approved_contact_context_status,
                            start_date, end_date, planned_hours, origin
                        ) VALUES
                            (
                                'R-PM-594', 'P-594', 'W-PM-594',
                                'CAPTURED',
                                '2026-10-01', '2026-10-01', 8.00, 'REQUEST'
                            ),
                            (
                                'R-UNKNOWN-594', 'P-594', 'W-UNKNOWN-594',
                                'LEGACY_UNKNOWN',
                                '2026-10-01', '2026-10-01', 8.00, 'REQUEST'
                            )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO resource_requirements (
                            id, project_id, workforce_request_id,
                            approved_task_catalog_item_id,
                            approved_contact_context_status,
                            start_date, end_date, planned_hours, origin
                        ) VALUES (
                            'R-BROKEN-594', 'P-594', 'W-BROKEN-594',
                            'T-WRONG-594', 'CAPTURED',
                            '2026-10-01', '2026-10-01', 8.00, 'REQUEST'
                        )
                        """
                    )
                )
            engine.dispose()

            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                with engine.connect() as connection:
                    rows = {
                        row.id: row
                        for row in connection.execute(
                            text(
                                """
                                SELECT
                                    id,
                                    operational_responsible_override_contact_id,
                                    captured_operational_responsible_contact_id,
                                    captured_operational_responsible_source_type,
                                    captured_operational_responsible_source_entity_id,
                                    captured_operational_responsible_status,
                                    operational_responsibility_context_provenance,
                                    operational_responsibility_context_version
                                FROM resource_requirements
                                WHERE id LIKE 'R-%-594'
                                """
                            )
                        )
                    }
                    project = connection.execute(
                        text(
                            """
                            SELECT
                                operational_responsible_override_contact_id,
                                operational_responsible_override_version
                            FROM projects
                            WHERE id = 'P-594'
                            """
                        )
                    ).one()

                captured = rows["R-CAPTURED-594"]
                self.assertIsNone(
                    captured.operational_responsible_override_contact_id
                )
                self.assertEqual(
                    captured.captured_operational_responsible_contact_id,
                    "C-REQ-594",
                )
                self.assertEqual(
                    captured.captured_operational_responsible_source_type,
                    "REQUEST_OVERRIDE",
                )
                self.assertEqual(
                    captured.captured_operational_responsible_source_entity_id,
                    "W-REQ-594",
                )
                self.assertIsNone(
                    captured.captured_operational_responsible_status
                )
                self.assertEqual(
                    captured.operational_responsibility_context_provenance,
                    "APPROVAL_CAPTURE",
                )
                self.assertEqual(
                    captured.operational_responsibility_context_version,
                    1,
                )

                observed_task = rows["R-TASK-594"]
                self.assertEqual(
                    observed_task.captured_operational_responsible_contact_id,
                    "C-TASK-594",
                )
                self.assertEqual(
                    observed_task.captured_operational_responsible_source_type,
                    "TASK_RESPONSIBLE",
                )
                self.assertEqual(
                    observed_task.captured_operational_responsible_source_entity_id,
                    "T-594",
                )
                self.assertEqual(
                    observed_task.operational_responsibility_context_provenance,
                    "MIGRATION_OBSERVED",
                )
                self.assertEqual(
                    observed_task.operational_responsibility_context_version,
                    1,
                )

                observed_manager = rows["R-PM-594"]
                self.assertEqual(
                    observed_manager.captured_operational_responsible_contact_id,
                    "C-PM-594",
                )
                self.assertEqual(
                    observed_manager.captured_operational_responsible_source_type,
                    "PROJECT_MANAGER",
                )
                self.assertEqual(
                    observed_manager.captured_operational_responsible_source_entity_id,
                    "P-594",
                )
                self.assertEqual(
                    observed_manager.operational_responsibility_context_provenance,
                    "MIGRATION_OBSERVED",
                )
                self.assertEqual(
                    observed_manager.operational_responsibility_context_version,
                    1,
                )

                broken = rows["R-BROKEN-594"]
                self.assertIsNone(
                    broken.captured_operational_responsible_contact_id
                )
                self.assertIsNone(
                    broken.captured_operational_responsible_source_type
                )
                self.assertEqual(
                    broken.operational_responsibility_context_provenance,
                    "LEGACY_UNKNOWN",
                )
                self.assertIsNone(
                    broken.operational_responsibility_context_version
                )

                unknown = rows["R-UNKNOWN-594"]
                self.assertIsNone(
                    unknown.captured_operational_responsible_contact_id
                )
                self.assertIsNone(
                    unknown.captured_operational_responsible_source_type
                )
                self.assertIsNone(
                    unknown.captured_operational_responsible_source_entity_id
                )
                self.assertEqual(
                    unknown.operational_responsibility_context_provenance,
                    "LEGACY_UNKNOWN",
                )
                self.assertIsNone(
                    unknown.operational_responsibility_context_version
                )

                self.assertIsNone(
                    project.operational_responsible_override_contact_id
                )
                self.assertEqual(
                    project.operational_responsible_override_version,
                    1,
                )
            finally:
                engine.dispose()

    def test_fresh_sqlite_upgrade_reaches_baseline_with_only_technical_seed(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "fresh-baseline.db"
            config = alembic_config(database_path)
            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                inspector = inspect(engine)
                self.assertEqual(
                    set(inspector.get_table_names()),
                    set(Base.metadata.tables) | {"alembic_version"},
                )
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalar_one(),
                        HEAD_REVISION,
                    )
                    self.assertEqual(
                        connection.execute(
                            text(
                                "SELECT version FROM planning_mutation_state "
                                "WHERE id = 'GLOBAL'"
                            )
                        ).scalar_one(),
                        1,
                    )
                    for table_name in (
                        "app_users",
                        "break_glass_credentials",
                        "projects",
                        "resources",
                        "workforce_requests",
                    ):
                        count = connection.execute(
                            text(f"SELECT COUNT(*) FROM {table_name}")
                        ).scalar_one()
                        self.assertEqual(count, 0, table_name)
            finally:
                engine.dispose()

    def test_head_matches_metadata_columns_constraints_and_indexes(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "baseline-parity.db"
            config = alembic_config(database_path)
            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            inspector = inspect(engine)
            try:
                for table_name, table in Base.metadata.tables.items():
                    migrated_columns = {
                        column["name"]: bool(column["nullable"])
                        for column in inspector.get_columns(table_name)
                    }
                    model_columns = {
                        column.name: bool(column.nullable)
                        for column in table.columns
                    }
                    self.assertEqual(
                        migrated_columns,
                        model_columns,
                        f"column/nullability drift for {table_name}",
                    )

                    self.assertEqual(
                        tuple(inspector.get_pk_constraint(table_name)["constrained_columns"]),
                        tuple(column.name for column in table.primary_key.columns),
                        f"primary-key drift for {table_name}",
                    )
                    self.assertEqual(
                        _database_foreign_keys(inspector, table_name),
                        _model_foreign_keys(table),
                        f"foreign-key drift for {table_name}",
                    )

                    migrated_indexes = {
                        item["name"]
                        for item in inspector.get_indexes(table_name)
                        if item.get("name")
                    }
                    model_indexes = {
                        index.name
                        for index in table.indexes
                        if index.name
                    }
                    self.assertEqual(
                        migrated_indexes,
                        model_indexes,
                        f"index drift for {table_name}",
                    )

                    migrated_unique_columns = {
                        tuple(item["column_names"])
                        for item in inspector.get_unique_constraints(table_name)
                        if item.get("column_names")
                    }
                    model_unique_columns = {
                        tuple(column.name for column in constraint.columns)
                        for constraint in table.constraints
                        if isinstance(constraint, UniqueConstraint)
                    }
                    self.assertEqual(
                        migrated_unique_columns,
                        model_unique_columns,
                        f"unique-constraint drift for {table_name}",
                    )

                    migrated_checks = {
                        item.get("name")
                        for item in inspector.get_check_constraints(table_name)
                        if item.get("name")
                    }
                    model_checks = {
                        constraint.name
                        for constraint in table.constraints
                        if isinstance(constraint, CheckConstraint) and constraint.name
                    }
                    self.assertEqual(
                        migrated_checks,
                        model_checks,
                        f"check-constraint drift for {table_name}",
                    )
            finally:
                engine.dispose()

    def test_head_downgrade_removes_application_schema(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "baseline-downgrade.db"
            config = alembic_config(database_path)
            command.upgrade(config, "head")
            command.downgrade(config, "base")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                remaining = set(inspect(engine).get_table_names())
                self.assertFalse(set(Base.metadata.tables).intersection(remaining))
            finally:
                engine.dispose()

    def test_head_compiles_offline_for_mssql(self) -> None:
        output = StringIO()
        command.upgrade(
            offline_config("mssql+pyodbc://", output),
            "head",
            sql=True,
        )
        ddl = output.getvalue().upper()
        for token in (
            "CREATE TABLE PROJECTS",
            "CREATE TABLE WORK_PACKAGES",
            "CREATE TABLE WORK_PACKAGE_AUDIT",
            "CREATE TABLE WORK_PACKAGE_WEEKLY_LOADS",
            "CREATE TABLE WORK_PACKAGE_LOAD_INTERVALS",
            "TERMINAL_STATUS",
            "CREATE TABLE PROJECT_CO_MANAGERS",
            "CREATE TABLE PROJECT_MANAGER_AUDIT",
            "CO_MANAGERS_VERSION",
            "OPERATIONAL_RESPONSIBLE_OVERRIDE_VERSION",
            "OPERATIONAL_RESPONSIBILITY_CONTEXT_PROVENANCE",
            "CAPTURED_OPERATIONAL_RESPONSIBLE_CONTACT_ID",
            "RESOURCE_CLASS_CODE",
            "CREATE TABLE AUTH_SESSIONS",
            "CREATE TABLE ASSET_TYPE_APPROVAL_SCOPE_MAPPINGS",
            "CREATE TABLE ASSET_APPROVERS",
            "CREATE TABLE ACUMATICA_PROJECT_TASK_SYNC_RUNS",
            "CREATE TABLE ACUMATICA_PROJECT_TASK_SYNC_PROJECT_RESULTS",
            "ASSET_TYPE_ID",
            "PROPOSED_ASSET_ID",
            "CREATE TABLE BREAK_GLASS_CREDENTIALS",
            "CREATE TABLE AUTH_SECURITY_AUDIT",
            "CREATE TABLE PLANNING_MUTATION_STATE",
            "SHIFT_AD_HOC",
            "UX_ASSET_REQUIREMENTS_SHIFT_AD_HOC",
        ):
            self.assertIn(token, ddl)
        self.assertIn(BASELINE_REVISION.upper(), ddl)
        self.assertIn(HEAD_REVISION.upper(), ddl)


if __name__ == "__main__":
    unittest.main()
