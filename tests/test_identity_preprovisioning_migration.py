from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def alembic_config(database_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


class IdentityPreprovisioningMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_existing_identity_session_and_backfills_unique_erp_user(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "identity-preprovision.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0046_delivery_persistence")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    INSERT INTO erp_user_directory (
                        user_id, employee_external_id, display_name,
                        erp_user_active, employee_status, local_active,
                        roles_json, created_at, updated_at
                    ) VALUES (
                        'ERP-1', 'EMP-1', 'Utilisateur ERP',
                        1, 'Actif', 1, '["PROJECT_MANAGER"]',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO business_contacts (
                        id, display_name, active, source,
                        external_system, external_entity, external_id, version,
                        created_at, updated_at
                    ) VALUES (
                        'BC-1', 'Utilisateur existant', 1, 'APP_USER',
                        'RESOURCEPLANNER', 'EMPLOYEE', 'EMP-1', 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO app_users (
                        id, issuer, subject, display_name, email,
                        employee_external_id, business_contact_id,
                        roles_json, active
                    ) VALUES (
                        'U-1', 'issuer-existing', 'subject-existing',
                        'Utilisateur existant', NULL, 'EMP-1', 'BC-1',
                        '["PROJECT_MANAGER"]', 1
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO auth_sessions (
                        id, token_hash, csrf_token_hash, user_id,
                        expires_at
                    ) VALUES (
                        'S-1',
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                        NULL,
                        'U-1',
                        '2030-01-01 00:00:00'
                    )
                    """
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                inspector = inspect(engine)
                columns = {
                    column["name"]: bool(column["nullable"])
                    for column in inspector.get_columns("app_users")
                }
                self.assertTrue(columns["issuer"])
                self.assertTrue(columns["subject"])
                self.assertTrue(columns["erp_user_id"])
                indexes = {
                    item["name"]: item
                    for item in inspector.get_indexes("app_users")
                }
                self.assertIn("ux_app_users_oidc_identity_not_null", indexes)
                self.assertIn("ux_app_users_erp_user_id_not_null", indexes)

                with engine.connect() as connection:
                    user = connection.exec_driver_sql(
                        """
                        SELECT id, issuer, subject, roles_json, active,
                               employee_external_id, business_contact_id,
                               erp_user_id
                        FROM app_users
                        WHERE id = 'U-1'
                        """
                    ).one()
                    self.assertEqual(
                        user,
                        (
                            "U-1",
                            "issuer-existing",
                            "subject-existing",
                            '["PROJECT_MANAGER"]',
                            1,
                            "EMP-1",
                            "BC-1",
                            "ERP-1",
                        ),
                    )
                    session = connection.exec_driver_sql(
                        "SELECT id, user_id FROM auth_sessions WHERE id = 'S-1'"
                    ).one()
                    self.assertEqual(session, ("S-1", "U-1"))
                    self.assertEqual(
                        connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall(),
                        [],
                    )
            finally:
                engine.dispose()

    def test_ambiguous_employee_is_not_backfilled_and_new_constraints_are_enforced(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "identity-ambiguous.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0046_delivery_persistence")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                for user_id in ("ERP-A", "ERP-B"):
                    connection.exec_driver_sql(
                        """
                        INSERT INTO erp_user_directory (
                            user_id, employee_external_id, display_name,
                            erp_user_active, employee_status, local_active,
                            roles_json, created_at, updated_at
                        ) VALUES (?, 'EMP-X', ?, 1, 'Actif', 1, '["TECHNICIAN"]',
                                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (user_id, user_id),
                    )
                connection.exec_driver_sql(
                    """
                    INSERT INTO app_users (
                        id, issuer, subject, display_name,
                        employee_external_id, roles_json, active
                    ) VALUES (
                        'U-X', 'issuer-x', 'subject-x', 'Utilisateur X',
                        'EMP-X', '["TECHNICIAN"]', 0
                    )
                    """
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                with engine.begin() as connection:
                    erp_user_id = connection.exec_driver_sql(
                        "SELECT erp_user_id FROM app_users WHERE id = 'U-X'"
                    ).scalar_one()
                    self.assertIsNone(erp_user_id)
                    for suffix in ("A", "B", "C"):
                        connection.exec_driver_sql(
                            """
                            INSERT INTO app_users (
                                id, issuer, subject, display_name,
                                roles_json, active
                            ) VALUES (?, NULL, NULL, ?, '["TECHNICIAN"]', 1)
                            """,
                            (f"U-NULL-{suffix}", f"Utilisateur {suffix}"),
                        )

                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.exec_driver_sql(
                            """
                            INSERT INTO app_users (
                                id, issuer, subject, display_name,
                                roles_json, active
                            ) VALUES (
                                'U-PARTIAL', 'issuer-partial', NULL, 'Partiel',
                                '["TECHNICIAN"]', 1
                            )
                            """
                        )

                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.exec_driver_sql(
                            """
                            INSERT INTO app_users (
                                id, issuer, subject, display_name,
                                roles_json, active
                            ) VALUES (
                                'U-DUP-OIDC', 'issuer-x', 'subject-x', 'Collision OIDC',
                                '["TECHNICIAN"]', 1
                            )
                            """
                        )

                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        "UPDATE app_users SET erp_user_id = 'ERP-A' WHERE id = 'U-NULL-A'"
                    )
                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.exec_driver_sql(
                            "UPDATE app_users SET erp_user_id = 'ERP-A' WHERE id = 'U-NULL-B'"
                        )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
