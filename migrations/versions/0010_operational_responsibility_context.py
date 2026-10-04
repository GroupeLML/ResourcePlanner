"""Add operational responsibility overrides and captured-context provenance.

Revision ID: 0010_operational_responsibility_context
Revises: 0009_work_package_load_intervals
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa


revision: str = "0010_operational_responsibility_context"
down_revision: str | None = "0009_work_package_load_intervals"
branch_labels: str | None = None
depends_on: str | None = None


PROJECT_VERSION_CHECK = (
    "ck_projects_operational_responsible_override_version_positive"
)
PROVENANCE_CHECK = (
    "ck_resource_requirements_operational_responsibility_context_provenance"
)
CONTEXT_VERSION_CHECK = (
    "ck_resource_requirements_operational_responsibility_context_version_positive"
)

PROJECT_OVERRIDE_FK = (
    "fk_projects_operational_responsible_override_contact_id_business_contacts"
)
REQUIREMENT_OVERRIDE_FK = (
    "fk_resource_requirements_operational_responsible_override_contact_id_business_contacts"
)
CAPTURED_CONTACT_FK = (
    "fk_resource_requirements_captured_operational_responsible_contact_id_business_contacts"
)
SHIFT_OVERRIDE_FK = (
    "fk_shifts_operational_responsible_override_contact_id_business_contacts"
)

PROJECT_OVERRIDE_INDEX = "ix_projects_operational_responsible_override_contact_id"
REQUIREMENT_OVERRIDE_INDEX = (
    "ix_resource_requirements_operational_responsible_override_contact_id"
)
CAPTURED_CONTACT_INDEX = (
    "ix_resource_requirements_captured_operational_responsible_contact_id"
)
PROVENANCE_INDEX = (
    "ix_resource_requirements_operational_responsibility_context_provenance"
)
SHIFT_OVERRIDE_INDEX = "ix_shifts_operational_responsible_override_contact_id"

APPROVAL_CAPTURE = "APPROVAL_CAPTURE"
MIGRATION_OBSERVED = "MIGRATION_OBSERVED"
LEGACY_UNKNOWN = "LEGACY_UNKNOWN"

SOURCE_REQUEST_OVERRIDE = "REQUEST_OVERRIDE"
SOURCE_TASK_RESPONSIBLE = "TASK_RESPONSIBLE"
SOURCE_PROJECT_MANAGER = "PROJECT_MANAGER"


def _add_contact_column(
    table_name: str,
    column_name: str,
    fk_name: str,
) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} "
            "VARCHAR(36) REFERENCES business_contacts(id)"
        )
        return

    op.add_column(
        table_name,
        sa.Column(column_name, sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        fk_name,
        table_name,
        "business_contacts",
        [column_name],
        ["id"],
    )


def _add_project_version() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE projects ADD COLUMN operational_responsible_override_version "
            "INTEGER NOT NULL DEFAULT 1 "
            f"CONSTRAINT {PROJECT_VERSION_CHECK} "
            "CHECK (operational_responsible_override_version >= 1)"
        )
        return

    op.add_column(
        "projects",
        sa.Column(
            "operational_responsible_override_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        PROJECT_VERSION_CHECK,
        "projects",
        "operational_responsible_override_version >= 1",
    )


def _add_context_columns() -> None:
    bind = op.get_bind()
    op.add_column(
        "resource_requirements",
        sa.Column(
            "captured_operational_responsible_source_type",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "resource_requirements",
        sa.Column(
            "captured_operational_responsible_source_entity_id",
            sa.String(length=36),
            nullable=True,
        ),
    )
    op.add_column(
        "resource_requirements",
        sa.Column(
            "captured_operational_responsible_diagnostics",
            sa.Text(),
            nullable=True,
        ),
    )

    if bind.dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE resource_requirements ADD COLUMN "
            "operational_responsibility_context_provenance VARCHAR(32) "
            "NOT NULL DEFAULT 'LEGACY_UNKNOWN' "
            f"CONSTRAINT {PROVENANCE_CHECK} CHECK ("
            "operational_responsibility_context_provenance IN "
            "('APPROVAL_CAPTURE','OPERATIONAL_CAPTURE','MIGRATION_OBSERVED','LEGACY_UNKNOWN')"
            ")"
        )
        op.execute(
            "ALTER TABLE resource_requirements ADD COLUMN "
            "operational_responsibility_context_version INTEGER "
            f"CONSTRAINT {CONTEXT_VERSION_CHECK} CHECK ("
            "operational_responsibility_context_version IS NULL OR "
            "operational_responsibility_context_version >= 1"
            ")"
        )
        return

    op.add_column(
        "resource_requirements",
        sa.Column(
            "operational_responsibility_context_provenance",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'LEGACY_UNKNOWN'"),
        ),
    )
    op.add_column(
        "resource_requirements",
        sa.Column(
            "operational_responsibility_context_version",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        PROVENANCE_CHECK,
        "resource_requirements",
        "operational_responsibility_context_provenance IN "
        "('APPROVAL_CAPTURE','OPERATIONAL_CAPTURE','MIGRATION_OBSERVED','LEGACY_UNKNOWN')",
    )
    op.create_check_constraint(
        CONTEXT_VERSION_CHECK,
        "resource_requirements",
        "operational_responsibility_context_version IS NULL OR "
        "operational_responsibility_context_version >= 1",
    )


def _backfill_legacy_context() -> None:
    bind = op.get_bind()
    manager_rows = bind.execute(
        sa.text(
            """
            SELECT p.id AS project_id, au.business_contact_id
            FROM projects AS p
            JOIN app_users AS au
              ON au.employee_external_id = p.project_manager_external_id
            JOIN business_contacts AS bc
              ON bc.id = au.business_contact_id
            WHERE au.business_contact_id IS NOT NULL
            """
        )
    ).mappings()
    managers_by_project = {
        str(row["project_id"]): str(row["business_contact_id"])
        for row in manager_rows
        if row["business_contact_id"] is not None
    }

    rows = bind.execute(
        sa.text(
            """
            SELECT
                rr.id,
                rr.workforce_request_id,
                rr.project_id,
                rr.approved_contact_context_status,
                rr.approved_operational_responsible_override_contact_id,
                rr.approved_task_catalog_item_id,
                task.operational_responsible_contact_id AS task_contact_id
            FROM resource_requirements AS rr
            LEFT JOIN task_catalog_items AS task
              ON task.id = rr.approved_task_catalog_item_id
            """
        )
    ).mappings()

    update = sa.text(
        """
        UPDATE resource_requirements
        SET captured_operational_responsible_contact_id = :contact_id,
            captured_operational_responsible_source_type = :source_type,
            captured_operational_responsible_source_entity_id = :source_entity_id,
            operational_responsibility_context_provenance = :provenance,
            operational_responsibility_context_version = 1
        WHERE id = :requirement_id
        """
    )

    for row in rows:
        if str(row["approved_contact_context_status"] or "") != "CAPTURED":
            continue

        contact_id = row[
            "approved_operational_responsible_override_contact_id"
        ]
        if contact_id is not None:
            bind.execute(
                update,
                {
                    "requirement_id": str(row["id"]),
                    "contact_id": str(contact_id),
                    "source_type": SOURCE_REQUEST_OVERRIDE,
                    "source_entity_id": (
                        str(row["workforce_request_id"])
                        if row["workforce_request_id"] is not None
                        else None
                    ),
                    "provenance": APPROVAL_CAPTURE,
                },
            )
            continue

        task_contact_id = row["task_contact_id"]
        if task_contact_id is not None:
            bind.execute(
                update,
                {
                    "requirement_id": str(row["id"]),
                    "contact_id": str(task_contact_id),
                    "source_type": SOURCE_TASK_RESPONSIBLE,
                    "source_entity_id": (
                        str(row["approved_task_catalog_item_id"])
                        if row["approved_task_catalog_item_id"] is not None
                        else None
                    ),
                    "provenance": MIGRATION_OBSERVED,
                },
            )
            continue

        manager_contact_id = managers_by_project.get(str(row["project_id"]))
        if manager_contact_id is not None:
            bind.execute(
                update,
                {
                    "requirement_id": str(row["id"]),
                    "contact_id": manager_contact_id,
                    "source_type": SOURCE_PROJECT_MANAGER,
                    "source_entity_id": str(row["project_id"]),
                    "provenance": MIGRATION_OBSERVED,
                },
            )


def upgrade() -> None:
    _add_contact_column(
        "projects",
        "operational_responsible_override_contact_id",
        PROJECT_OVERRIDE_FK,
    )
    _add_project_version()

    _add_contact_column(
        "resource_requirements",
        "operational_responsible_override_contact_id",
        REQUIREMENT_OVERRIDE_FK,
    )
    _add_contact_column(
        "resource_requirements",
        "captured_operational_responsible_contact_id",
        CAPTURED_CONTACT_FK,
    )
    _add_context_columns()

    _add_contact_column(
        "shifts",
        "operational_responsible_override_contact_id",
        SHIFT_OVERRIDE_FK,
    )

    op.create_index(
        PROJECT_OVERRIDE_INDEX,
        "projects",
        ["operational_responsible_override_contact_id"],
        unique=False,
    )
    op.create_index(
        REQUIREMENT_OVERRIDE_INDEX,
        "resource_requirements",
        ["operational_responsible_override_contact_id"],
        unique=False,
    )
    op.create_index(
        CAPTURED_CONTACT_INDEX,
        "resource_requirements",
        ["captured_operational_responsible_contact_id"],
        unique=False,
    )
    op.create_index(
        PROVENANCE_INDEX,
        "resource_requirements",
        ["operational_responsibility_context_provenance"],
        unique=False,
    )
    op.create_index(
        SHIFT_OVERRIDE_INDEX,
        "shifts",
        ["operational_responsible_override_contact_id"],
        unique=False,
    )

    if not context.is_offline_mode():
        _backfill_legacy_context()


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index(SHIFT_OVERRIDE_INDEX, table_name="shifts")
    op.drop_index(PROVENANCE_INDEX, table_name="resource_requirements")
    op.drop_index(CAPTURED_CONTACT_INDEX, table_name="resource_requirements")
    op.drop_index(REQUIREMENT_OVERRIDE_INDEX, table_name="resource_requirements")
    op.drop_index(PROJECT_OVERRIDE_INDEX, table_name="projects")

    if bind.dialect.name != "sqlite":
        op.drop_constraint(SHIFT_OVERRIDE_FK, "shifts", type_="foreignkey")
        op.drop_constraint(
            CAPTURED_CONTACT_FK,
            "resource_requirements",
            type_="foreignkey",
        )
        op.drop_constraint(
            REQUIREMENT_OVERRIDE_FK,
            "resource_requirements",
            type_="foreignkey",
        )
        op.drop_constraint(PROJECT_OVERRIDE_FK, "projects", type_="foreignkey")
        op.drop_constraint(
            CONTEXT_VERSION_CHECK,
            "resource_requirements",
            type_="check",
        )
        op.drop_constraint(
            PROVENANCE_CHECK,
            "resource_requirements",
            type_="check",
        )
        op.drop_constraint(PROJECT_VERSION_CHECK, "projects", type_="check")

    op.drop_column(
        "shifts",
        "operational_responsible_override_contact_id",
    )
    op.drop_column(
        "resource_requirements",
        "operational_responsibility_context_version",
    )
    op.drop_column(
        "resource_requirements",
        "operational_responsibility_context_provenance",
        mssql_drop_default=True,
    )
    op.drop_column(
        "resource_requirements",
        "captured_operational_responsible_diagnostics",
    )
    op.drop_column(
        "resource_requirements",
        "captured_operational_responsible_source_entity_id",
    )
    op.drop_column(
        "resource_requirements",
        "captured_operational_responsible_source_type",
    )
    op.drop_column(
        "resource_requirements",
        "captured_operational_responsible_contact_id",
    )
    op.drop_column(
        "resource_requirements",
        "operational_responsible_override_contact_id",
    )
    op.drop_column(
        "projects",
        "operational_responsible_override_version",
        mssql_drop_default=True,
    )
    op.drop_column(
        "projects",
        "operational_responsible_override_contact_id",
    )
