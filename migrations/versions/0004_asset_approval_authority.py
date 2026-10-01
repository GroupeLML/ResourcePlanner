"""Add asset approval scope and unit approver mappings.

Revision ID: 0004_asset_approval_authority
Revises: 0003_work_package_resource_class
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0004_asset_approval_authority"
down_revision: str | None = "0003_work_package_resource_class"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "asset_type_approval_scope_mappings",
        sa.Column("asset_type_id", sa.String(length=36), nullable=False),
        sa.Column("approval_scope_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_type_id"],
            ["asset_types.id"],
            name="fk_asset_type_approval_scope_asset_type",
        ),
        sa.ForeignKeyConstraint(
            ["approval_scope_id"],
            ["approval_scopes.id"],
            name="fk_asset_type_approval_scope_scope",
        ),
        sa.PrimaryKeyConstraint(
            "asset_type_id",
            "approval_scope_id",
            name="pk_asset_type_approval_scope_mappings",
        ),
    )
    op.create_index(
        "ix_asset_type_approval_scope_mappings_approval_scope_id",
        "asset_type_approval_scope_mappings",
        ["approval_scope_id"],
        unique=False,
    )

    op.create_table(
        "asset_approvers",
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("app_user_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_asset_approvers_asset",
        ),
        sa.ForeignKeyConstraint(
            ["app_user_id"],
            ["app_users.id"],
            name="fk_asset_approvers_user",
        ),
        sa.PrimaryKeyConstraint(
            "asset_id",
            "app_user_id",
            name="pk_asset_approvers",
        ),
    )
    op.create_index(
        "ix_asset_approvers_app_user_id",
        "asset_approvers",
        ["app_user_id"],
        unique=False,
    )

    bind = op.get_bind()
    asset_type_column = sa.Column("asset_type_id", sa.String(length=36), nullable=True)
    proposed_asset_column = sa.Column("proposed_asset_id", sa.String(length=36), nullable=True)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("approval_requirements", recreate="always") as batch_op:
            batch_op.add_column(asset_type_column)
            batch_op.add_column(proposed_asset_column)
            batch_op.create_foreign_key(
                "fk_approval_requirements_asset_type",
                "asset_types",
                ["asset_type_id"],
                ["id"],
            )
            batch_op.create_foreign_key(
                "fk_approval_requirements_proposed_asset",
                "assets",
                ["proposed_asset_id"],
                ["id"],
            )
            batch_op.create_index(
                "ix_approval_requirements_asset_type_id",
                ["asset_type_id"],
                unique=False,
            )
            batch_op.create_index(
                "ix_approval_requirements_proposed_asset_id",
                ["proposed_asset_id"],
                unique=False,
            )
    else:
        op.add_column("approval_requirements", asset_type_column)
        op.add_column("approval_requirements", proposed_asset_column)
        op.create_foreign_key(
            "fk_approval_requirements_asset_type",
            "approval_requirements",
            "asset_types",
            ["asset_type_id"],
            ["id"],
        )
        op.create_foreign_key(
            "fk_approval_requirements_proposed_asset",
            "approval_requirements",
            "assets",
            ["proposed_asset_id"],
            ["id"],
        )
        op.create_index(
            "ix_approval_requirements_asset_type_id",
            "approval_requirements",
            ["asset_type_id"],
            unique=False,
        )
        op.create_index(
            "ix_approval_requirements_proposed_asset_id",
            "approval_requirements",
            ["proposed_asset_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("approval_requirements", recreate="always") as batch_op:
            batch_op.drop_index("ix_approval_requirements_proposed_asset_id")
            batch_op.drop_index("ix_approval_requirements_asset_type_id")
            batch_op.drop_constraint("fk_approval_requirements_proposed_asset", type_="foreignkey")
            batch_op.drop_constraint("fk_approval_requirements_asset_type", type_="foreignkey")
            batch_op.drop_column("proposed_asset_id")
            batch_op.drop_column("asset_type_id")
    else:
        op.drop_index(
            "ix_approval_requirements_proposed_asset_id",
            table_name="approval_requirements",
        )
        op.drop_index(
            "ix_approval_requirements_asset_type_id",
            table_name="approval_requirements",
        )
        op.drop_constraint(
            "fk_approval_requirements_proposed_asset",
            "approval_requirements",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_approval_requirements_asset_type",
            "approval_requirements",
            type_="foreignkey",
        )
        op.drop_column("approval_requirements", "proposed_asset_id")
        op.drop_column("approval_requirements", "asset_type_id")
    op.drop_index(
        "ix_asset_approvers_app_user_id",
        table_name="asset_approvers",
    )
    op.drop_table("asset_approvers")
    op.drop_index(
        "ix_asset_type_approval_scope_mappings_approval_scope_id",
        table_name="asset_type_approval_scope_mappings",
    )
    op.drop_table("asset_type_approval_scope_mappings")
