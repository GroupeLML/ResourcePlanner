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
                        }.issubset(tables)
                    )
                    auth_columns = {
                        column["name"]
                        for column in inspect(connection).get_columns("auth_sessions")
                    }
                    self.assertIn("auth_mode", auth_columns)
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
