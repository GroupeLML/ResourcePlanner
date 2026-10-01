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
        "ix_asset_type_approval_scope_mappings_scope",
        "asset_type_approval_scope_mappings",
        ["approval_scope_id"],
        unique=False,
    )

    op.create_table(
        "asset_approval_approvers",
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("app_user_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name="fk_asset_approval_approvers_asset",
        ),
        sa.ForeignKeyConstraint(
            ["app_user_id"],
            ["app_users.id"],
            name="fk_asset_approval_approvers_user",
        ),
        sa.PrimaryKeyConstraint(
            "asset_id",
            "app_user_id",
            name="pk_asset_approval_approvers",
        ),
    )
    op.create_index(
        "ix_asset_approval_approvers_user",
        "asset_approval_approvers",
        ["app_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_asset_approval_approvers_user",
        table_name="asset_approval_approvers",
    )
    op.drop_table("asset_approval_approvers")
    op.drop_index(
        "ix_asset_type_approval_scope_mappings_scope",
        table_name="asset_type_approval_scope_mappings",
    )
    op.drop_table("asset_type_approval_scope_mappings")
