"""Add WorkPackage ERP task identity, local CAS and durable audit.

Revision ID: 0049_work_package_task_identity
Revises: 0048_identity_admin_audit
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0049_work_package_task_identity"
down_revision: str | None = "0048_identity_admin_audit"
branch_labels: str | None = None
depends_on: str | None = None


TASK_FK = "fk_work_packages_task_catalog_item_id_task_catalog_items"
VERSION_CHECK = "ck_work_packages_work_package_version_positive"
TASK_INDEX = "ix_work_packages_task_catalog_item"
AUDIT_VERSION_CHECK = "ck_work_package_audit_work_package_audit_version_positive"


def _extend_work_packages() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("work_packages", recreate="always") as batch_op:
            batch_op.add_column(
                sa.Column("task_catalog_item_id", sa.String(length=36), nullable=True)
            )
            batch_op.add_column(
                sa.Column(
                    "version",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("1"),
                )
            )
            batch_op.create_foreign_key(
                TASK_FK,
                "task_catalog_items",
                ["task_catalog_item_id"],
                ["id"],
            )
            batch_op.create_check_constraint(VERSION_CHECK, "version >= 1")
        op.create_index(
            TASK_INDEX,
            "work_packages",
            ["task_catalog_item_id"],
            unique=False,
        )
        return

    op.add_column(
        "work_packages",
        sa.Column("task_catalog_item_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "work_packages",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_foreign_key(
        TASK_FK,
        "work_packages",
        "task_catalog_items",
        ["task_catalog_item_id"],
        ["id"],
    )
    op.create_check_constraint(
        VERSION_CHECK,
        "work_packages",
        "version >= 1",
    )
    op.create_index(
        TASK_INDEX,
        "work_packages",
        ["task_catalog_item_id"],
        unique=False,
    )


def upgrade() -> None:
    _extend_work_packages()
    op.create_table(
        "work_package_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_package_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        sa.Column("old_values_json", sa.Text(), nullable=False),
        sa.Column("new_values_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "resulting_version >= 1",
            name=AUDIT_VERSION_CHECK,
        ),
        sa.ForeignKeyConstraint(
            ["work_package_id"],
            ["work_packages.id"],
            name="fk_work_package_audit_work_package_id_work_packages",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_users.id"],
            name="fk_work_package_audit_actor_user_id_app_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_work_package_audit"),
    )
    op.create_index(
        "ix_work_package_audit_package_created",
        "work_package_audit",
        ["work_package_id", "created_at"],
    )
    op.create_index(
        "ix_work_package_audit_actor",
        "work_package_audit",
        ["actor_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_work_package_audit_actor", table_name="work_package_audit")
    op.drop_index(
        "ix_work_package_audit_package_created",
        table_name="work_package_audit",
    )
    op.drop_table("work_package_audit")

    op.drop_index(TASK_INDEX, table_name="work_packages")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("work_packages", recreate="always") as batch_op:
            batch_op.drop_constraint(VERSION_CHECK, type_="check")
            batch_op.drop_constraint(TASK_FK, type_="foreignkey")
            batch_op.drop_column("version")
            batch_op.drop_column("task_catalog_item_id")
        return

    op.drop_constraint(VERSION_CHECK, "work_packages", type_="check")
    op.drop_constraint(TASK_FK, "work_packages", type_="foreignkey")
    op.drop_column("work_packages", "version")
    op.drop_column("work_packages", "task_catalog_item_id")
