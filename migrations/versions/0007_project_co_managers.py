"""Add persistent project co-managers, collection CAS and audit.

Revision ID: 0007_project_co_managers
Revises: 0006_asset_requirement_origins
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0007_project_co_managers"
down_revision: str | None = "0006_asset_requirement_origins"
branch_labels: str | None = None
depends_on: str | None = None


PROJECT_VERSION_CHECK = "ck_projects_co_managers_version_positive"


def _add_project_version() -> None:
    bind = op.get_bind()
    column = sa.Column(
        "co_managers_version",
        sa.Integer(),
        nullable=False,
        server_default=sa.text("1"),
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projects", recreate="always") as batch_op:
            batch_op.add_column(column)
            batch_op.create_check_constraint(
                op.f(PROJECT_VERSION_CHECK),
                "co_managers_version >= 1",
            )
        return

    op.add_column("projects", column)
    op.create_check_constraint(
        op.f(PROJECT_VERSION_CHECK),
        "projects",
        "co_managers_version >= 1",
    )


def upgrade() -> None:
    _add_project_version()

    op.create_table(
        "project_co_managers",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("business_contact_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_co_managers_project_id_projects"),
        ),
        sa.ForeignKeyConstraint(
            ["business_contact_id"],
            ["business_contacts.id"],
            name=op.f(
                "fk_project_co_managers_business_contact_id_business_contacts"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_users.id"],
            name=op.f("fk_project_co_managers_created_by_user_id_app_users"),
        ),
        sa.PrimaryKeyConstraint(
            "project_id",
            "business_contact_id",
            name=op.f("pk_project_co_managers"),
        ),
    )
    op.create_index(
        "ix_project_co_managers_contact_project",
        "project_co_managers",
        ["business_contact_id", "project_id"],
        unique=False,
    )

    op.create_table(
        "project_manager_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("after_json", sa.Text(), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "resulting_version IS NULL OR resulting_version >= 1",
            name=op.f(
                "ck_project_manager_audit_resulting_version_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_manager_audit_project_id_projects"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_users.id"],
            name=op.f("fk_project_manager_audit_actor_user_id_app_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_manager_audit")),
    )
    op.create_index(
        "ix_project_manager_audit_project_created",
        "project_manager_audit",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_project_manager_audit_actor",
        "project_manager_audit",
        ["actor_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_manager_audit_actor",
        table_name="project_manager_audit",
    )
    op.drop_index(
        "ix_project_manager_audit_project_created",
        table_name="project_manager_audit",
    )
    op.drop_table("project_manager_audit")

    op.drop_index(
        "ix_project_co_managers_contact_project",
        table_name="project_co_managers",
    )
    op.drop_table("project_co_managers")

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projects", recreate="always") as batch_op:
            batch_op.drop_constraint(
                op.f(PROJECT_VERSION_CHECK),
                type_="check",
            )
            batch_op.drop_column("co_managers_version")
        return

    op.drop_constraint(
        op.f(PROJECT_VERSION_CHECK),
        "projects",
        type_="check",
    )
    op.drop_column("projects", "co_managers_version", mssql_drop_default=True)
