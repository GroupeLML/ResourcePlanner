from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import inspect, select, text

from app.infrastructure.sql import (
    AppUser,
    AuthSession,
    Base,
    Project,
    WorkPackage,
    create_sql_engine,
)
from tools.bridge_sqlite_0048_runtime import (
    BridgeBlocked,
    SOURCE_REVISION,
    _code_head,
    bridge_sqlite_0048,
)


def _url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_current_database(path: Path) -> None:
    engine = create_sql_engine(_url(path))
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(128) NOT NULL)")
            )
            connection.execute(
                text("INSERT INTO alembic_version(version_num) VALUES (:revision)"),
                {"revision": _code_head()},
            )
            connection.execute(
                Project.__table__.insert(),
                {
                    "id": "PROJECT-BRIDGE",
                    "number": "P-BRIDGE",
                    "name": "Projet bridge",
                    "status": "active",
                },
            )
            connection.execute(
                AppUser.__table__.insert(),
                {
                    "id": "USER-BRIDGE",
                    "issuer": None,
                    "subject": None,
                    "display_name": "Utilisateur bridge",
                    "roles_json": '["ADMIN"]',
                    "active": True,
                },
            )
            connection.execute(
                WorkPackage.__table__.insert(),
                {
                    "id": "WP-BRIDGE",
                    "project_id": "PROJECT-BRIDGE",
                    "code": "WP-BRIDGE",
                    "name": "Lot bridge",
                },
            )
            connection.execute(
                AuthSession.__table__.insert(),
                {
                    "id": "SESSION-BRIDGE",
                    "token_hash": "b" * 64,
                    "csrf_token_hash": "c" * 64,
                    "user_id": "USER-BRIDGE",
                    "auth_mode": "local",
                    "expires_at": now + timedelta(hours=8),
                },
            )
    finally:
        engine.dispose()


def _reshape_as_0048(path: Path) -> None:
    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.executescript(
            """
            DROP TABLE IF EXISTS auth_security_audit;
            DROP TABLE IF EXISTS break_glass_credentials;
            DROP TABLE IF EXISTS work_package_audit;
            DROP TABLE IF EXISTS work_package_weekly_loads;
            DROP TABLE IF EXISTS work_package_load_intervals;
            DROP TABLE IF EXISTS availability_rule_resource_classes;
            DROP TABLE IF EXISTS project_manager_audit;
            DROP TABLE IF EXISTS project_co_managers;

            ALTER TABLE communication_messages
                DROP COLUMN to_recipients_json;

            DROP TABLE IF EXISTS acumatica_project_task_sync_project_results;
            DROP TABLE IF EXISTS acumatica_project_task_sync_runs;
            DROP TABLE IF EXISTS asset_approvers;
            DROP TABLE IF EXISTS asset_type_approval_scope_mappings;

            CREATE TABLE projects_0048 (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                erp_external_id VARCHAR(128),
                number VARCHAR(64) NOT NULL UNIQUE,
                name VARCHAR(255) DEFAULT '' NOT NULL,
                client VARCHAR(255),
                project_manager_external_id VARCHAR(128),
                project_manager_name VARCHAR(255),
                project_manager_contact_id VARCHAR(36),
                status VARCHAR(32) DEFAULT 'active' NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                FOREIGN KEY(project_manager_contact_id) REFERENCES business_contacts (id)
            );
            INSERT INTO projects_0048 (
                id,
                erp_external_id,
                number,
                name,
                client,
                project_manager_external_id,
                project_manager_name,
                project_manager_contact_id,
                status,
                created_at,
                updated_at
            )
            SELECT
                id,
                erp_external_id,
                number,
                name,
                client,
                project_manager_external_id,
                project_manager_name,
                project_manager_contact_id,
                status,
                created_at,
                updated_at
            FROM projects;
            DROP TABLE projects;
            ALTER TABLE projects_0048 RENAME TO projects;
            CREATE INDEX ix_projects_erp_external_id
                ON projects (erp_external_id);
            CREATE INDEX ix_projects_project_manager_contact_id
                ON projects (project_manager_contact_id);
            CREATE INDEX ix_projects_project_manager_external_id
                ON projects (project_manager_external_id);
            CREATE INDEX ix_projects_status
                ON projects (status);


            CREATE TABLE resource_requirements_0048 AS
            SELECT
                id,
                legacy_segment_id,
                project_id,
                workforce_request_id,
                source_request_line_id,
                approved_task_catalog_item_id,
                approved_operational_responsible_override_contact_id,
                approved_request_version,
                approved_contact_context_status,
                approval_revision_id,
                approved_entry_key,
                approval_reference_status,
                assigned_resource_id,
                start_date,
                end_date,
                planned_hours,
                desired_active_days,
                load_profile,
                status,
                description,
                source_effort_id,
                required_resource_class,
                required_competency,
                required_competency_id,
                planning_type,
                priority,
                outside_standard_hours_allowed,
                confirmation,
                confirmation_overridden,
                origin,
                created_by_external_id,
                created_by_name,
                created_at,
                updated_at
            FROM resource_requirements;
            DROP TABLE resource_requirements;
            ALTER TABLE resource_requirements_0048 RENAME TO resource_requirements;

            CREATE TABLE shifts_0048 AS
            SELECT
                id,
                legacy_allocation_id,
                resource_requirement_id,
                resource_id,
                work_date,
                hours,
                allocation_type,
                source,
                locked,
                outside_standard_hours,
                confirmation,
                note,
                created_at,
                updated_at
            FROM shifts;
            DROP TABLE shifts;
            ALTER TABLE shifts_0048 RENAME TO shifts;

            CREATE TABLE approval_requirements_0048 (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                approval_cycle_id VARCHAR(36) NOT NULL,
                request_line_id VARCHAR(36) NOT NULL,
                task_catalog_item_id VARCHAR(36),
                approval_scope_id VARCHAR(36),
                proposed_resource_id VARCHAR(36),
                routing_sources_text TEXT DEFAULT '[]' NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                CONSTRAINT uq_approval_requirement_cycle_line
                    UNIQUE (approval_cycle_id, request_line_id),
                FOREIGN KEY(approval_cycle_id) REFERENCES request_approval_cycles (id),
                FOREIGN KEY(request_line_id) REFERENCES request_lines (id),
                FOREIGN KEY(task_catalog_item_id) REFERENCES task_catalog_items (id),
                FOREIGN KEY(approval_scope_id) REFERENCES approval_scopes (id),
                FOREIGN KEY(proposed_resource_id) REFERENCES resources (id)
            );
            INSERT INTO approval_requirements_0048 (
                id,
                approval_cycle_id,
                request_line_id,
                task_catalog_item_id,
                approval_scope_id,
                proposed_resource_id,
                routing_sources_text,
                created_at,
                updated_at
            )
            SELECT
                id,
                approval_cycle_id,
                request_line_id,
                task_catalog_item_id,
                approval_scope_id,
                proposed_resource_id,
                routing_sources_text,
                created_at,
                updated_at
            FROM approval_requirements;
            DROP TABLE approval_requirements;
            ALTER TABLE approval_requirements_0048 RENAME TO approval_requirements;
            CREATE INDEX ix_approval_requirements_approval_scope_id
                ON approval_requirements (approval_scope_id);
            CREATE INDEX ix_approval_requirements_cycle
                ON approval_requirements (approval_cycle_id);
            CREATE INDEX ix_approval_requirements_proposed_resource_id
                ON approval_requirements (proposed_resource_id);
            CREATE INDEX ix_approval_requirements_request_line_id
                ON approval_requirements (request_line_id);
            CREATE INDEX ix_approval_requirements_task_catalog_item_id
                ON approval_requirements (task_catalog_item_id);

            CREATE TABLE auth_sessions_0048 AS
            SELECT
                id,
                token_hash,
                csrf_token_hash,
                user_id,
                expires_at,
                revoked_at,
                created_at,
                updated_at
            FROM auth_sessions;
            DROP TABLE auth_sessions;
            ALTER TABLE auth_sessions_0048 RENAME TO auth_sessions;

            CREATE TABLE work_packages_0048 AS
            SELECT
                id,
                project_id,
                code,
                name,
                description,
                start_date,
                end_date,
                planned_hours,
                status,
                legacy_effort_id,
                created_at,
                updated_at
            FROM work_packages;
            DROP TABLE work_packages;
            ALTER TABLE work_packages_0048 RENAME TO work_packages;

            CREATE TABLE asset_requirements_0048 (
                id VARCHAR(36) NOT NULL,
                project_id VARCHAR(36) NOT NULL,
                workforce_request_id VARCHAR(36) NOT NULL,
                source_request_line_id VARCHAR(36) NOT NULL,
                source_period_id VARCHAR(36),
                approval_revision_id VARCHAR(36),
                approved_entry_key VARCHAR(512) NOT NULL,
                slot_index INTEGER DEFAULT 0 NOT NULL,
                asset_type_id VARCHAR(36) NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                usage_hours NUMERIC(12, 2),
                status VARCHAR(32) DEFAULT 'À affecter' NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                CONSTRAINT ck_asset_requirements_asset_requirement_window
                    CHECK (end_date >= start_date),
                CONSTRAINT ck_asset_requirements_asset_requirement_hours_positive
                    CHECK (usage_hours IS NULL OR usage_hours > 0),
                CONSTRAINT fk_asset_requirements_approval_revision_id_request_approval_revisions
                    FOREIGN KEY(approval_revision_id) REFERENCES request_approval_revisions (id),
                CONSTRAINT fk_asset_requirements_asset_type_id_asset_types
                    FOREIGN KEY(asset_type_id) REFERENCES asset_types (id),
                CONSTRAINT fk_asset_requirements_project_id_projects
                    FOREIGN KEY(project_id) REFERENCES projects (id),
                CONSTRAINT fk_asset_requirements_source_period_id_workforce_request_periods
                    FOREIGN KEY(source_period_id) REFERENCES workforce_request_periods (id),
                CONSTRAINT fk_asset_requirements_source_request_line_id_request_lines
                    FOREIGN KEY(source_request_line_id) REFERENCES request_lines (id),
                CONSTRAINT fk_asset_requirements_workforce_request_id_workforce_requests
                    FOREIGN KEY(workforce_request_id) REFERENCES workforce_requests (id),
                CONSTRAINT pk_asset_requirements PRIMARY KEY (id),
                CONSTRAINT uq_asset_requirement_entry_slot
                    UNIQUE (workforce_request_id, approved_entry_key, slot_index)
            );
            INSERT INTO asset_requirements_0048 (
                id,
                project_id,
                workforce_request_id,
                source_request_line_id,
                source_period_id,
                approval_revision_id,
                approved_entry_key,
                slot_index,
                asset_type_id,
                start_date,
                end_date,
                usage_hours,
                status,
                created_at,
                updated_at
            )
            SELECT
                id,
                project_id,
                workforce_request_id,
                source_request_line_id,
                source_period_id,
                approval_revision_id,
                approved_entry_key,
                slot_index,
                asset_type_id,
                start_date,
                end_date,
                usage_hours,
                status,
                created_at,
                updated_at
            FROM asset_requirements;
            DROP TABLE asset_requirements;
            ALTER TABLE asset_requirements_0048 RENAME TO asset_requirements;
            CREATE INDEX ix_asset_requirements_request
                ON asset_requirements (workforce_request_id, source_request_line_id);

            UPDATE alembic_version
            SET version_num = '0048_identity_admin_audit';
            """
        )
        raw.commit()
    finally:
        raw.close()


def _revision(path: Path) -> str:
    raw = sqlite3.connect(path)
    try:
        return str(raw.execute("SELECT version_num FROM alembic_version").fetchone()[0])
    finally:
        raw.close()


class Sqlite0048RuntimeBridgeTests(unittest.TestCase):
    def test_tool_is_directly_executable_from_repository_root(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/bridge_sqlite_0048_runtime.py", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--confirm-live-runtime", result.stdout)

    def test_dry_run_is_read_only_and_recognizes_exact_0048_shape(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "resourceplanner.db"
            _create_current_database(source)
            _reshape_as_0048(source)
            before = _hash(source)

            report = bridge_sqlite_0048(source)

            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["revision"], SOURCE_REVISION)
            self.assertEqual(report["work_packages"], 1)
            self.assertEqual(report["auth_sessions"], 1)
            self.assertEqual(_hash(source), before)
            self.assertEqual(_revision(source), SOURCE_REVISION)

    def test_apply_migrates_copy_to_head_then_atomically_replaces_live_database(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "resourceplanner.db"
            backup = root / "resourceplanner-before-bridge.db"
            _create_current_database(source)
            _reshape_as_0048(source)
            before = _hash(source)

            report = bridge_sqlite_0048(
                source,
                backup_path=backup,
                apply=True,
                confirm_live_runtime=True,
            )

            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["revision_after"], _code_head())
            self.assertEqual(_hash(backup), before)
            self.assertEqual(_revision(backup), SOURCE_REVISION)
            self.assertEqual(_revision(source), _code_head())

            engine = create_sql_engine(_url(source))
            try:
                with engine.connect() as connection:
                    tables = set(inspect(connection).get_table_names())
                    self.assertTrue(
                        {
                            "auth_security_audit",
                            "break_glass_credentials",
                            "work_package_audit",
                            "work_package_weekly_loads",
                            "availability_rule_resource_classes",
                        }.issubset(tables)
                    )
                    auth_columns = {
                        column["name"]
                        for column in inspect(connection).get_columns("auth_sessions")
                    }
                    self.assertIn("auth_mode", auth_columns)
                    asset_requirement_columns = {
                        column["name"]: column
                        for column in inspect(connection).get_columns(
                            "asset_requirements"
                        )
                    }
                    self.assertIn(
                        "resource_requirement_id",
                        asset_requirement_columns,
                    )
                    self.assertIn(
                        "context_resource_id",
                        asset_requirement_columns,
                    )
                    self.assertTrue(
                        asset_requirement_columns["project_id"]["nullable"]
                    )
                    session = connection.execute(
                        select(AuthSession).where(AuthSession.id == "SESSION-BRIDGE")
                    ).scalar_one()
                    self.assertIsNotNone(session)

                    wp = connection.execute(
                        select(WorkPackage.__table__).where(
                            WorkPackage.__table__.c.id == "WP-BRIDGE"
                        )
                    ).mappings().one()
                    self.assertEqual(wp["version"], 1)
                    self.assertIsNone(wp["task_catalog_item_id"])
                    self.assertIsNone(wp["weekly_load_origin"])
                    self.assertIsNone(wp["resource_class_code"])

                    auth_mode = connection.scalar(
                        text(
                            "SELECT auth_mode FROM auth_sessions "
                            "WHERE id = 'SESSION-BRIDGE'"
                        )
                    )
                    self.assertEqual(auth_mode, "oidc")
            finally:
                engine.dispose()

    def test_failure_before_atomic_replace_leaves_live_0048_unchanged(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "resourceplanner.db"
            backup = root / "resourceplanner-before-bridge.db"
            _create_current_database(source)
            _reshape_as_0048(source)
            before = _hash(source)

            with self.assertRaises(RuntimeError):
                bridge_sqlite_0048(
                    source,
                    backup_path=backup,
                    apply=True,
                    confirm_live_runtime=True,
                    fail_before_replace=True,
                )

            self.assertEqual(_hash(source), before)
            self.assertEqual(_revision(source), SOURCE_REVISION)
            self.assertEqual(_hash(backup), before)

    def test_apply_requires_explicit_live_runtime_confirmation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "resourceplanner.db"
            _create_current_database(source)
            _reshape_as_0048(source)

            with self.assertRaises(BridgeBlocked):
                bridge_sqlite_0048(
                    source,
                    backup_path=root / "backup.db",
                    apply=True,
                )

    def test_wrong_revision_is_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "resourceplanner.db"
            _create_current_database(source)

            with self.assertRaises(BridgeBlocked):
                bridge_sqlite_0048(source)


if __name__ == "__main__":
    unittest.main()
