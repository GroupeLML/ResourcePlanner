from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.infrastructure.sql import Base


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
INITIAL_REVISION = MIGRATIONS / "versions" / "0001_initial_planning_schema.py"
EXPECTED_TABLES = {
    "approval_decisions",
    "approval_requirement_approvers",
    "approval_requirements",
    "approval_scopes",
    "approval_scope_approvers",
    "resource_class_approval_scope_mappings",
    "task_approval_scope_mappings",
    "business_contacts",
    "delivery_plans",
    "delivery_items",
    "delivery_change_history",
    "command_idempotency_receipts",
    "planning_mutation_state",
    "projects",
    "request_approval_cycles",
    "request_approval_references",
    "request_approval_revisions",
    "request_operational_states",
    "request_lines",
    "request_line_competencies",
    "resource_requirement_competencies",
    "resource_class_configs",
    "project_task_class_overrides",
    "resources",
    "work_packages",
    "workforce_requests",
    "workforce_request_history",
    "workforce_request_periods",
    "workforce_request_period_selections",
    "workforce_request_period_requirements",
    "resource_availability_rules",
    "resource_requirements",
    "shifts",
    "smtp_configuration",
    "smtp_configuration_audit",
    "communication_deliveries",
    "task_class_standards",
}


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


class SqlMigrationTests(unittest.TestCase):
    def test_initial_revision_is_self_contained(self) -> None:
        source = INITIAL_REVISION.read_text(encoding="utf-8")

        self.assertIn('revision: str = "0001_initial_planning_schema"', source)
        self.assertIn("down_revision", source)
        self.assertNotIn("app.infrastructure.sql", source)
        self.assertNotIn("from app", source)
        self.assertNotIn("import app", source)

    def test_upgrade_and_downgrade_on_sqlite(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "migration-test.db"
            config = alembic_config(database_path)

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            upgraded = set(inspect(engine).get_table_names())
            self.assertTrue(EXPECTED_TABLES.issubset(upgraded))
            self.assertIn("alembic_version", upgraded)
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        text("SELECT version FROM planning_mutation_state WHERE id = 'GLOBAL'")
                    ).scalar_one(),
                    1,
                )
            engine.dispose()

            command.downgrade(config, "base")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            downgraded = set(inspect(engine).get_table_names())
            self.assertFalse(EXPECTED_TABLES.intersection(downgraded))
            engine.dispose()

    def test_migration_matches_orm_columns_and_nullability(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "schema-parity.db"
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
                        f"Migration/ORM drift for {table_name}",
                    )
            finally:
                engine.dispose()

    def test_period_selection_primary_key_is_line_scoped(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "line-period-selection-pk.db"
            config = alembic_config(database_path)
            command.upgrade(config, "head")

            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            inspector = inspect(engine)
            try:
                primary_key = inspector.get_pk_constraint(
                    "workforce_request_period_selections"
                )
                self.assertEqual(
                    primary_key["constrained_columns"],
                    ["request_line_id", "alternative_group"],
                )
                columns = {
                    column["name"]: bool(column["nullable"])
                    for column in inspector.get_columns(
                        "workforce_request_period_selections"
                    )
                }
                self.assertFalse(columns["request_line_id"])
            finally:
                engine.dispose()

    def test_initial_migration_compiles_offline_for_postgresql_and_mssql(self) -> None:
        for url in ("postgresql://", "mssql+pyodbc://"):
            output = StringIO()
            command.upgrade(offline_config(url, output), "head", sql=True)
            ddl = output.getvalue().upper()

            self.assertIn("CREATE TABLE PROJECTS", ddl, url)
            self.assertIn("CREATE TABLE RESOURCE_REQUIREMENTS", ddl, url)
            self.assertIn("CREATE TABLE SHIFTS", ddl, url)
            self.assertIn("CREATE TABLE WORKFORCE_REQUEST_PERIODS", ddl, url)
            self.assertIn("CREATE TABLE WORKFORCE_REQUEST_PERIOD_SELECTIONS", ddl, url)
            self.assertIn("CREATE TABLE WORKFORCE_REQUEST_PERIOD_REQUIREMENTS", ddl, url)
            self.assertIn("CREATE TABLE COMMAND_IDEMPOTENCY_RECEIPTS", ddl, url)
            self.assertIn("UQ_COMMAND_IDEMPOTENCY_ACTOR_SCOPE_KEY", ddl, url)
            self.assertIn("WORKFORCE_REQUEST_ID", ddl, url)
            self.assertIn("CREATE TABLE REQUEST_LINES", ddl, url)
            self.assertIn("CREATE TABLE REQUEST_LINE_COMPETENCIES", ddl, url)
            self.assertIn("CREATE TABLE RESOURCE_REQUIREMENT_COMPETENCIES", ddl, url)
            self.assertIn("SOURCE_REQUEST_LINE_ID", ddl, url)
            self.assertIn("REQUEST_LINE_ID", ddl, url)
            self.assertIn("AGGREGATE_VERSION", ddl, url)
            self.assertIn("LINE_MODE", ddl, url)
            self.assertIn("ESTIMATED_HOURS_SOURCE", ddl, url)
            self.assertIn("DEFAULT_HOURS_PER_DAY", ddl, url)
            self.assertIn("REQUIRED_RESOURCE_CLASS", ddl, url)
            self.assertIn("CREATE TABLE BUSINESS_CONTACTS", ddl, url)
            self.assertIn("CREATE TABLE APPROVAL_SCOPES", ddl, url)
            self.assertIn("CREATE TABLE DELIVERY_PLANS", ddl, url)
            self.assertIn("CREATE TABLE DELIVERY_ITEMS", ddl, url)
            self.assertIn("CREATE TABLE DELIVERY_CHANGE_HISTORY", ddl, url)
            self.assertIn("DELIVERY_VERSION", ddl, url)
            self.assertIn("CREATE TABLE REQUEST_APPROVAL_CYCLES", ddl, url)
            self.assertIn("CREATE TABLE APPROVAL_REQUIREMENTS", ddl, url)
            self.assertIn("CREATE TABLE APPROVAL_REQUIREMENT_APPROVERS", ddl, url)
            self.assertIn("CREATE TABLE APPROVAL_DECISIONS", ddl, url)
            self.assertIn("CREATE TABLE RESOURCE_CLASS_CONFIGS", ddl, url)
            self.assertIn("CREATE TABLE TASK_CLASS_STANDARDS", ddl, url)
            self.assertIn("CREATE TABLE PROJECT_TASK_CLASS_OVERRIDES", ddl, url)
            self.assertIn("SUBJECT_FINGERPRINT", ddl, url)
            self.assertIn("SUBMITTED_REQUEST_VERSION", ddl, url)
            self.assertIn("ACTION_ID", ddl, url)
            self.assertIn("CREATE TABLE APPROVAL_SCOPE_APPROVERS", ddl, url)
            self.assertIn("CREATE TABLE TASK_APPROVAL_SCOPE_MAPPINGS", ddl, url)
            self.assertIn("FK_SCOPE_APPROVER_USER", ddl, url)
            self.assertIn("FK_TASK_SCOPE_MAPPING_TASK", ddl, url)
            self.assertIn("PROJECT_MANAGER_CONTACT_ID", ddl, url)
            self.assertIn("OPERATIONAL_RESPONSIBLE_CONTACT_ID", ddl, url)
            self.assertIn("COORDINATOR_CONTACT_ID", ddl, url)
            self.assertIn("OPERATIONAL_RESPONSIBLE_OVERRIDE_CONTACT_ID", ddl, url)
            self.assertIn("APPROVED_TASK_CATALOG_ITEM_ID", ddl, url)
            self.assertIn(
                "APPROVED_OPERATIONAL_RESPONSIBLE_OVERRIDE_CONTACT_ID",
                ddl,
                url,
            )
            self.assertIn("APPROVED_REQUEST_VERSION", ddl, url)
            self.assertIn("APPROVED_CONTACT_CONTEXT_STATUS", ddl, url)
            self.assertIn("BUSINESS_CONTACT_ID", ddl, url)
            self.assertIn("UX_APP_USERS_BUSINESS_CONTACT_ID_NOT_NULL", ddl, url)
            self.assertIn("ERP_USER_ID", ddl, url)
            self.assertIn("UX_APP_USERS_OIDC_IDENTITY_NOT_NULL", ddl, url)
            self.assertIn("UX_APP_USERS_ERP_USER_ID_NOT_NULL", ddl, url)
            self.assertIn("CK_APP_USERS_OIDC_IDENTITY_COMPLETE", ddl, url)
            self.assertIn("FK_APP_USERS_ERP_USER_ID_ERP_USER_DIRECTORY", ddl, url)
            self.assertIn("CREATE TABLE IDENTITY_ADMIN_AUDIT", ddl, url)
            self.assertIn("ACTOR_USER_ID", ddl, url)
            self.assertIn("TARGET_USER_ID", ddl, url)
            self.assertIn("OLD_STATE_JSON", ddl, url)
            self.assertIn("NEW_STATE_JSON", ddl, url)
            self.assertIn("FK_IDENTITY_ADMIN_AUDIT_ACTOR_USER_ID_APP_USERS", ddl, url)
            self.assertIn("FK_IDENTITY_ADMIN_AUDIT_TARGET_USER_ID_APP_USERS", ddl, url)
            self.assertIn("MODEL_VERSION", ddl, url)
            self.assertIn("PROJECT_SNAPSHOT_JSON", ddl, url)
            self.assertIn("MESSAGE_KEY", ddl, url)
            self.assertIn("CC_RECIPIENTS_JSON", ddl, url)
            self.assertIn("CONTENT_FINGERPRINT", ddl, url)
            self.assertIn("APPROVABLE", ddl, url)
            self.assertIn("SMTP_CONFIGURATION", ddl, url)
            self.assertIn("SMTP_CONFIGURATION_AUDIT", ddl, url)
            self.assertIn("CHANGED_FIELDS_JSON", ddl, url)
            self.assertIn("COMMUNICATION_DELIVERIES", ddl, url)
            self.assertIn("ENCRYPTED_PASSWORD", ddl, url)
            self.assertIn("PROVIDER_MESSAGE_ID", ddl, url)
            self.assertIn("UX_COMMUNICATION_DELIVERIES_MESSAGE_PROVIDER", ddl, url)

    def test_approval_revision_migration_preserves_existing_plan_as_legacy_unknown(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "approval-revision-backfill.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0030_smtp_delivery")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO projects (id, number, name) "
                    "VALUES ('P1', 'P-1', 'Projet existant')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO workforce_requests (id, project_id, status) "
                    "VALUES ('D1', 'P1', 'En planification')"
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO resource_requirements (
                        id, project_id, workforce_request_id, start_date, end_date,
                        planned_hours, status, origin
                    ) VALUES (
                        'REQ-REQUEST', 'P1', 'D1', '2026-09-21', '2026-09-21',
                        8, 'Planifié', 'REQUEST'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO resource_requirements (
                        id, project_id, start_date, end_date, planned_hours,
                        status, origin
                    ) VALUES (
                        'REQ-ADHOC', 'P1', '2026-09-22', '2026-09-22',
                        4, 'À assigner', 'AD_HOC'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO resources (id, name, active)
                    VALUES ('R1', 'Alice', 1)
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO shifts (
                        id, resource_requirement_id, resource_id, work_date, hours,
                        allocation_type, locked, outside_standard_hours
                    ) VALUES (
                        'S1', 'REQ-REQUEST', 'R1', '2026-09-21', 8,
                        'Planifié', 1, 0
                    )
                    """
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                with engine.connect() as connection:
                    requirement = connection.exec_driver_sql(
                        """
                        SELECT planned_hours, status, approval_revision_id,
                               approved_entry_key, approval_reference_status
                        FROM resource_requirements
                        WHERE id = 'REQ-REQUEST'
                        """
                    ).fetchone()
                    self.assertEqual(
                        requirement,
                        (8, "Planifié", None, None, "LEGACY_UNKNOWN"),
                    )
                    adhoc = connection.exec_driver_sql(
                        """
                        SELECT approval_reference_status
                        FROM resource_requirements
                        WHERE id = 'REQ-ADHOC'
                        """
                    ).fetchone()
                    self.assertEqual(adhoc, ("NOT_APPLICABLE",))
                    shift = connection.exec_driver_sql(
                        """
                        SELECT resource_requirement_id, resource_id, work_date,
                               hours, locked
                        FROM shifts WHERE id = 'S1'
                        """
                    ).fetchone()
                    self.assertEqual(
                        shift,
                        ("REQ-REQUEST", "R1", "2026-09-21", 8, 1),
                    )
                    reference = connection.exec_driver_sql(
                        """
                        SELECT active_revision_id, status
                        FROM request_approval_references
                        WHERE workforce_request_id = 'D1'
                        """
                    ).fetchone()
                    self.assertEqual(reference, (None, "LEGACY_UNKNOWN"))
                    revisions = connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM request_approval_revisions"
                    ).scalar_one()
                    self.assertEqual(revisions, 0)
            finally:
                engine.dispose()

    def test_smtp_migration_backfills_existing_project_messages(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "smtp-backfill.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0029_project_communications")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    INSERT INTO communication_batches (
                        id, week_start, kind, snapshot_fingerprint, status,
                        prepared_at, model_version
                    ) VALUES (
                        'B1', '2026-09-21', 'project_confirmation',
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                        'APPROVED', '2026-09-20 12:00:00', 'project_v2'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO communication_messages (
                        id, batch_id, audience, recipient_id, recipient_email,
                        message_key, project_id, subject, body, included,
                        approvable
                    ) VALUES (
                        'M1', 'B1', 'project', 'C-PM',
                        'pm' || char(64) || 'example.invalid',
                        'project:P1', 'P1', 'Sujet test', 'Corps test', 1, 1
                    )
                    """
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                with engine.connect() as connection:
                    rows = connection.exec_driver_sql(
                        """
                        SELECT message_id, provider, status, attempt_count
                        FROM communication_deliveries
                        ORDER BY message_id
                        """
                    ).fetchall()
                    self.assertEqual(rows, [("M1", "SMTP", "PENDING", 0)])
            finally:
                engine.dispose()

    def test_approved_contact_context_migration_preserves_history_as_unknown(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "approved-contact-context.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0026_business_contacts")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO projects (id, number, name) "
                    "VALUES ('P1', 'P-1', 'Projet historique')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO workforce_requests (id, project_id) "
                    "VALUES ('D1', 'P1')"
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO resource_requirements (
                        id, project_id, workforce_request_id,
                        start_date, end_date, planned_hours, origin
                    ) VALUES (
                        'REQ-HIST', 'P1', 'D1',
                        '2026-09-21', '2026-09-21', 8, 'REQUEST'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO resource_requirements (
                        id, project_id, workforce_request_id,
                        start_date, end_date, planned_hours, origin
                    ) VALUES (
                        'REQ-ADHOC', 'P1', NULL,
                        '2026-09-21', '2026-09-21', 4, 'AD_HOC'
                    )
                    """
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.connect() as connection:
                rows = connection.exec_driver_sql(
                    """
                    SELECT id, approved_task_catalog_item_id,
                           approved_operational_responsible_override_contact_id,
                           approved_request_version,
                           approved_contact_context_status
                    FROM resource_requirements
                    ORDER BY id
                    """
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        ("REQ-ADHOC", None, None, None, "NOT_APPLICABLE"),
                        ("REQ-HIST", None, None, None, "LEGACY_UNKNOWN"),
                    ],
                )
            engine.dispose()

            command.downgrade(config, "0026_business_contacts")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            inspector = inspect(engine)
            columns = {
                column["name"]
                for column in inspector.get_columns("resource_requirements")
            }
            self.assertNotIn("approved_task_catalog_item_id", columns)
            self.assertNotIn(
                "approved_operational_responsible_override_contact_id",
                columns,
            )
            self.assertNotIn("approved_request_version", columns)
            self.assertNotIn("approved_contact_context_status", columns)
            with engine.connect() as connection:
                self.assertEqual(
                    connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM resource_requirements"
                    ).scalar_one(),
                    2,
                )
            engine.dispose()

    def test_business_contact_migration_backfills_only_stable_project_manager_identity(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "business-contact-backfill.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0025_requirement_resource_class")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    INSERT INTO projects (
                        id, number, name, project_manager_external_id,
                        project_manager_name
                    ) VALUES (
                        'P1', 'P-1', 'Projet 1', 'EMP-PM', 'Chargé Stable'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO projects (
                        id, number, name, project_manager_external_id,
                        project_manager_name
                    ) VALUES (
                        'P2', 'P-2', 'Projet 2', 'EMP-PM', 'Chargé Stable'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO projects (
                        id, number, name, project_manager_external_id,
                        project_manager_name
                    ) VALUES (
                        'P3', 'P-3', 'Projet 3', NULL, 'Nom seulement'
                    )
                    """
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.connect() as connection:
                contacts = connection.exec_driver_sql(
                    """
                    SELECT display_name, source, external_system,
                           external_entity, external_id
                    FROM business_contacts
                    ORDER BY external_id
                    """
                ).fetchall()
                self.assertEqual(
                    contacts,
                    [
                        (
                            "Chargé Stable",
                            "MIGRATION",
                            "RESOURCEPLANNER",
                            "EMPLOYEE",
                            "EMP-PM",
                        )
                    ],
                )
                project_links = connection.exec_driver_sql(
                    """
                    SELECT id, project_manager_contact_id
                    FROM projects
                    ORDER BY id
                    """
                ).fetchall()
                self.assertIsNotNone(project_links[0][1])
                self.assertEqual(project_links[0][1], project_links[1][1])
                self.assertIsNone(project_links[2][1])

                self.assertEqual(
                    connection.exec_driver_sql(
                        "SELECT project_manager_name FROM projects WHERE id = 'P3'"
                    ).scalar_one(),
                    "Nom seulement",
                )
            engine.dispose()

            command.downgrade(config, "0025_requirement_resource_class")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            inspector = inspect(engine)
            self.assertNotIn("business_contacts", inspector.get_table_names())
            columns = {
                column["name"] for column in inspector.get_columns("projects")
            }
            self.assertNotIn("project_manager_contact_id", columns)
            with engine.connect() as connection:
                self.assertEqual(
                    connection.exec_driver_sql(
                        "SELECT project_manager_name FROM projects WHERE id = 'P3'"
                    ).scalar_one(),
                    "Nom seulement",
                )
            engine.dispose()

    def test_user_business_contact_migration_reuses_employee_contact_and_creates_missing_profiles(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "user-business-contact.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0027_approved_contact_context")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    INSERT INTO business_contacts (
                        id, display_name, active, source,
                        external_system, external_entity, external_id, version
                    ) VALUES (
                        'C-PM', 'Chargé projet A', 1, 'MIGRATION',
                        'RESOURCEPLANNER', 'EMPLOYEE', 'EMP-PM', 1
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO app_users (
                        id, issuer, subject, display_name, email,
                        employee_external_id, roles_json, active
                    ) VALUES (
                        'U-PM', 'urn:test', 'pm', 'Chargé de projet Démo',
                        'pm' || char(64) || 'example.invalid', 'EMP-PM', '["PROJECT_MANAGER"]', 1
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO app_users (
                        id, issuer, subject, display_name, email,
                        employee_external_id, roles_json, active
                    ) VALUES (
                        'U-COORD', 'urn:test', 'coord', 'Coordonnateur Démo',
                        NULL, NULL, '["COORDINATOR"]', 1
                    )
                    """
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.connect() as connection:
                users = connection.exec_driver_sql(
                    "SELECT id, business_contact_id FROM app_users ORDER BY id"
                ).fetchall()
                self.assertEqual(len(users), 2)
                self.assertTrue(all(row[1] for row in users))
                links = dict(users)
                self.assertEqual(links["U-PM"], "C-PM")

                pm_contact = connection.exec_driver_sql(
                    """
                    SELECT display_name, email, source
                    FROM business_contacts WHERE id = 'C-PM'
                    """
                ).one()
                self.assertEqual(
                    pm_contact,
                    ("Chargé de projet Démo", "pm" + chr(64) + "example.invalid", "APP_USER"),
                )

                coordinator_contact = connection.exec_driver_sql(
                    """
                    SELECT display_name, source
                    FROM business_contacts WHERE id = ?
                    """,
                    (links["U-COORD"],),
                ).one()
                self.assertEqual(
                    coordinator_contact,
                    ("Coordonnateur Démo", "APP_USER"),
                )
            engine.dispose()

            command.downgrade(config, "0027_approved_contact_context")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            columns = {
                column["name"] for column in inspect(engine).get_columns("app_users")
            }
            self.assertNotIn("business_contact_id", columns)
            engine.dispose()

    def test_request_line_migration_backfills_without_rebuilding_plan(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "request-line-backfill.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0021_auth_security")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO projects (id, number, name) VALUES ('P1', 'P-1', 'Projet')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO resources (id, name, resource_class) "
                    "VALUES ('R1', 'Alice', 'AUT')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO work_packages (id, project_id, code, name) "
                    "VALUES ('WP1', 'P1', 'WP-1', 'Lot 1')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO task_catalog_items "
                    "(id, project_number, task_code, label) "
                    "VALUES ('TASK1', 'P-1', 'T-1', 'Tâche ERP')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO competencies (id, name, active, sort_order) "
                    "VALUES ('C1', 'PLC', 1, 0)"
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO workforce_requests (
                        id, legacy_demand_number, project_id, work_package_id,
                        erp_task_code, erp_task_label, confirmation,
                        desired_start, desired_end, description, resource_count,
                        required_competencies, estimated_hours, estimated_days,
                        proposed_resource_id, status
                    ) VALUES (
                        'D1', 'DEM-1', 'P1', 'WP1',
                        'T-1', 'Tâche ERP', 'Confirmée',
                        '2026-09-21', '2026-09-25', 'Besoin historique', 2,
                        'PLC', 48, 3, 'R1', 'En planification'
                    )
                    """
                )
                connection.exec_driver_sql(
                    "INSERT INTO workforce_request_competencies "
                    "(workforce_request_id, competency_id) VALUES ('D1', 'C1')"
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO workforce_request_periods (
                        id, period_key, workforce_request_id, sequence, kind,
                        alternative_group, start_date, end_date, hours,
                        confirmation, proposed_resource_id, resource_count,
                        desired_active_days, active, created_at, updated_at
                    ) VALUES (
                        'PER1', 'PER-1', 'D1', 0, 'CUMULATIVE',
                        NULL, '2026-09-21', '2026-09-23', 32,
                        'Confirmée', 'R1', 2, 2, 1,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO workforce_request_periods (
                        id, period_key, workforce_request_id, sequence, kind,
                        alternative_group, start_date, end_date, hours,
                        confirmation, resource_count, active, created_at, updated_at
                    ) VALUES (
                        'ALT1', 'ALT-1', 'D1', 1, 'ALTERNATIVE',
                        'VISITE', '2026-09-24', '2026-09-24', 8,
                        'Tentative', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO workforce_request_period_selections (
                        workforce_request_id, alternative_group, period_id,
                        selected_at, selected_by_name
                    ) VALUES (
                        'D1', 'VISITE', 'ALT1', CURRENT_TIMESTAMP, 'Coord'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO resource_requirements (
                        id, legacy_segment_id, project_id, workforce_request_id,
                        assigned_resource_id, start_date, end_date, planned_hours,
                        status, required_competency_id, planning_type, priority,
                        origin
                    ) VALUES (
                        'REQ1', 'SEG-1', 'P1', 'D1',
                        'R1', '2026-09-21', '2026-09-25', 24,
                        'Planifié', 'C1', 'Flexible', 'Normale', 'REQUEST'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO resource_requirements (
                        id, legacy_segment_id, project_id, workforce_request_id,
                        start_date, end_date, planned_hours, status,
                        planning_type, priority, origin
                    ) VALUES (
                        'REQ2', 'SEG-2', 'P1', 'D1',
                        '2026-09-21', '2026-09-25', 24, 'À assigner',
                        'Flexible', 'Normale', 'REQUEST'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO resource_requirements (
                        id, legacy_segment_id, project_id, workforce_request_id,
                        start_date, end_date, planned_hours, status,
                        required_competency_id, planning_type, priority, origin
                    ) VALUES (
                        'AD1', 'SEG-AD', 'P1', NULL,
                        '2026-09-21', '2026-09-21', 4, 'À assigner',
                        'C1', 'Flexible', 'Normale', 'AD_HOC'
                    )
                    """
                )
                connection.exec_driver_sql(
                    "INSERT INTO workforce_request_period_requirements "
                    "(resource_requirement_id, period_id) VALUES ('REQ1', 'PER1')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO shifts "
                    "(id, legacy_allocation_id, resource_requirement_id, resource_id, "
                    "work_date, hours, allocation_type, source, locked) "
                    "VALUES ('SHIFT1', 'AUTO-1', 'REQ1', 'R1', "
                    "'2026-09-21', 8, 'Flexible', 'AUTO', 1)"
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.connect() as connection:
                line = connection.exec_driver_sql(
                    """
                    SELECT id, workforce_request_id, position, kind, slot_count,
                           required_resource_class, required_competencies_snapshot,
                           desired_start, desired_end, desired_active_days,
                           estimated_hours, estimated_hours_source,
                           default_hours_per_day, confirmation, work_package_id,
                           task_catalog_item_id, erp_task_code, erp_task_label,
                           proposed_resource_id, description, active
                    FROM request_lines
                    WHERE id = 'D1'
                    """
                ).mappings().one()
                self.assertEqual(line["workforce_request_id"], "D1")
                self.assertEqual(line["position"], 0)
                self.assertEqual(line["kind"], "WORKFORCE")
                self.assertEqual(line["slot_count"], 2)
                self.assertIsNone(line["required_resource_class"])
                self.assertEqual(line["required_competencies_snapshot"], "PLC")
                self.assertEqual(float(line["desired_active_days"]), 3.0)
                self.assertEqual(float(line["estimated_hours"]), 48.0)
                self.assertEqual(line["estimated_hours_source"], "LEGACY")
                self.assertIsNone(line["default_hours_per_day"])
                self.assertEqual(line["work_package_id"], "WP1")
                self.assertEqual(line["task_catalog_item_id"], "TASK1")
                self.assertEqual(line["erp_task_code"], "T-1")
                self.assertEqual(line["erp_task_label"], "Tâche ERP")
                self.assertEqual(line["proposed_resource_id"], "R1")
                self.assertEqual(line["description"], "Besoin historique")
                self.assertEqual(line["active"], 1)

                line_competencies = connection.exec_driver_sql(
                    "SELECT request_line_id, competency_id "
                    "FROM request_line_competencies"
                ).fetchall()
                self.assertEqual(line_competencies, [("D1", "C1")])

                period_lines = connection.exec_driver_sql(
                    "SELECT id, request_line_id FROM workforce_request_periods "
                    "ORDER BY id"
                ).fetchall()
                self.assertEqual(period_lines, [("ALT1", "D1"), ("PER1", "D1")])
                selection_line = connection.exec_driver_sql(
                    "SELECT request_line_id FROM workforce_request_period_selections "
                    "WHERE workforce_request_id = 'D1' AND alternative_group = 'VISITE'"
                ).scalar_one()
                self.assertEqual(selection_line, "D1")

                requirement_lines = connection.exec_driver_sql(
                    "SELECT id, source_request_line_id FROM resource_requirements "
                    "ORDER BY id"
                ).fetchall()
                self.assertEqual(
                    requirement_lines,
                    [("AD1", None), ("REQ1", "D1"), ("REQ2", "D1")],
                )
                requirement_competencies = connection.exec_driver_sql(
                    "SELECT resource_requirement_id, competency_id "
                    "FROM resource_requirement_competencies "
                    "ORDER BY resource_requirement_id, competency_id"
                ).fetchall()
                self.assertEqual(
                    requirement_competencies,
                    [("AD1", "C1"), ("REQ1", "C1"), ("REQ2", "C1")],
                )

                version_row = connection.exec_driver_sql(
                    "SELECT aggregate_version, line_mode "
                    "FROM workforce_requests WHERE id = 'D1'"
                ).one()
                self.assertEqual(version_row[0], 1)
                self.assertEqual(version_row[1], 0)
                self.assertEqual(
                    connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM resource_requirements"
                    ).scalar_one(),
                    3,
                )
                shift = connection.exec_driver_sql(
                    "SELECT id, resource_requirement_id, hours, locked "
                    "FROM shifts WHERE id = 'SHIFT1'"
                ).one()
                self.assertEqual(shift[0], "SHIFT1")
                self.assertEqual(shift[1], "REQ1")
                self.assertEqual(float(shift[2]), 8.0)
                self.assertEqual(shift[3], 1)
            engine.dispose()

            command.downgrade(config, "0021_auth_security")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            inspector = inspect(engine)
            self.assertNotIn("request_lines", inspector.get_table_names())
            with engine.connect() as connection:
                self.assertEqual(
                    connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM workforce_requests"
                    ).scalar_one(),
                    1,
                )
                self.assertEqual(
                    connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM resource_requirements"
                    ).scalar_one(),
                    3,
                )
                self.assertEqual(
                    connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM shifts"
                    ).scalar_one(),
                    1,
                )
            engine.dispose()


    def test_canonical_requester_migration_does_not_guess_identity_from_name(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "canonical-requester.db"
            config = alembic_config(database_path)
            command.upgrade(config, "0033_request_operational_budgets")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO projects (id, number, name) "
                    "VALUES ('P1', 'P-1', 'Projet existant')"
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO app_users (
                        id, issuer, subject, display_name, roles_json, active
                    ) VALUES (
                        'U1', 'urn:test', 'subject-1', 'Nom identique',
                        '["PROJECT_MANAGER"]', 1
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO workforce_requests (
                        id, project_id, requester_name, status
                    ) VALUES (
                        'D1', 'P1', 'Nom identique', 'Brouillon'
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO workforce_request_history (
                        id, workforce_request_id, action, actor_name, occurred_at
                    ) VALUES (
                        'H1', 'D1', 'Création', 'Nom identique', CURRENT_TIMESTAMP
                    )
                    """
                )
            engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                with engine.connect() as connection:
                    request = connection.exec_driver_sql(
                        "SELECT requester_user_id, requester_name "
                        "FROM workforce_requests WHERE id = 'D1'"
                    ).one()
                    self.assertEqual(request, (None, "Nom identique"))
                    history = connection.exec_driver_sql(
                        "SELECT actor_user_id, actor_name "
                        "FROM workforce_request_history WHERE id = 'H1'"
                    ).one()
                    self.assertEqual(history, (None, "Nom identique"))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
