"""Add project-task preferred resource preference, CAS and audit.

Revision ID: 0017_task_preferred_resource
Revises: 0016_task_erp_budget_freshness
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0017_task_preferred_resource"
down_revision: str | None = "0016_task_erp_budget_freshness"
branch_labels: str | None = None
depends_on: str | None = None


def _upgrade_task_catalog() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # Add the columns before the batch rebuild. Adding freshly introduced
        # columns inside recreate="always" can make Alembic infer a cyclic
        # partial column ordering when the source database has accumulated
        # additive columns from prior revisions (notably the 0048 bridge path).
        op.add_column(
            "task_catalog_items",
            sa.Column("preferred_resource_id", sa.String(length=36), nullable=True),
        )
        op.add_column(
            "task_catalog_items",
            sa.Column(
                "preferred_resource_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        with op.batch_alter_table("task_catalog_items", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                op.f("fk_task_catalog_items_preferred_resource_id_resources"),
                "resources",
                ["preferred_resource_id"],
                ["id"],
            )
            batch_op.create_check_constraint(
                op.f("ck_task_catalog_items_preferred_resource_version_positive"),
                "preferred_resource_version >= 1",
            )
        op.create_index(
            op.f("ix_task_catalog_items_preferred_resource_id"),
            "task_catalog_items",
            ["preferred_resource_id"],
            unique=False,
        )
        return

    op.add_column(
        "task_catalog_items",
        sa.Column("preferred_resource_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "task_catalog_items",
        sa.Column(
            "preferred_resource_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_foreign_key(
        op.f("fk_task_catalog_items_preferred_resource_id_resources"),
        "task_catalog_items",
        "resources",
        ["preferred_resource_id"],
        ["id"],
    )
    op.create_check_constraint(
        op.f("ck_task_catalog_items_preferred_resource_version_positive"),
        "task_catalog_items",
        "preferred_resource_version >= 1",
    )
    op.create_index(
        op.f("ix_task_catalog_items_preferred_resource_id"),
        "task_catalog_items",
        ["preferred_resource_id"],
        unique=False,
    )


def upgrade() -> None:
    _upgrade_task_catalog()
    op.create_table(
        "task_catalog_preferred_resource_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_catalog_item_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("old_resource_id", sa.String(length=36), nullable=True),
        sa.Column("new_resource_id", sa.String(length=36), nullable=True),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
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
            name=op.f(
                "ck_task_catalog_preferred_resource_audit_resulting_version_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["task_catalog_item_id"],
            ["task_catalog_items.id"],
            name=op.f(
                "fk_task_catalog_preferred_resource_audit_task_catalog_item_id_task_catalog_items"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_task_catalog_preferred_resource_audit"),
        ),
    )
    op.create_index(
        "ix_task_catalog_preferred_resource_audit_task_created",
        "task_catalog_preferred_resource_audit",
        ["task_catalog_item_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_catalog_preferred_resource_audit_actor",
        "task_catalog_preferred_resource_audit",
        ["actor_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_catalog_preferred_resource_audit_actor",
        table_name="task_catalog_preferred_resource_audit",
    )
    op.drop_index(
        "ix_task_catalog_preferred_resource_audit_task_created",
        table_name="task_catalog_preferred_resource_audit",
    )
    op.drop_table("task_catalog_preferred_resource_audit")

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("task_catalog_items", recreate="always") as batch_op:
            batch_op.drop_index(op.f("ix_task_catalog_items_preferred_resource_id"))
            batch_op.drop_constraint(
                op.f("ck_task_catalog_items_preferred_resource_version_positive"),
                type_="check",
            )
            batch_op.drop_constraint(
                op.f("fk_task_catalog_items_preferred_resource_id_resources"),
                type_="foreignkey",
            )
            batch_op.drop_column("preferred_resource_version")
            batch_op.drop_column("preferred_resource_id")
        return

    op.drop_index(
        op.f("ix_task_catalog_items_preferred_resource_id"),
        table_name="task_catalog_items",
    )
    op.drop_constraint(
        op.f("ck_task_catalog_items_preferred_resource_version_positive"),
        "task_catalog_items",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_task_catalog_items_preferred_resource_id_resources"),
        "task_catalog_items",
        type_="foreignkey",
    )
    op.drop_column(
        "task_catalog_items",
        "preferred_resource_version",
        mssql_drop_default=True,
    )
    op.drop_column("task_catalog_items", "preferred_resource_id")
