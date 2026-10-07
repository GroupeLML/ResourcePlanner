"""Persist Planning manual resource order per AppUser.

Revision ID: 0019_planning_resource_user_order
Revises: 0018_competency_resource_class
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0019_planning_resource_user_order"
down_revision: str | None = "0018_competency_resource_class"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "planning_resource_user_orders",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f(
                "ck_planning_resource_user_orders_"
                "planning_resource_user_order_position_non_negative"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_users.id"],
            name=op.f("fk_planning_resource_user_orders_user_id_app_users"),
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["resources.id"],
            name=op.f("fk_planning_resource_user_orders_resource_id_resources"),
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "resource_id",
            name=op.f("pk_planning_resource_user_orders"),
        ),
    )
    op.create_index(
        "ix_planning_resource_user_orders_user_position",
        "planning_resource_user_orders",
        ["user_id", "position"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_planning_resource_user_orders_user_position",
        table_name="planning_resource_user_orders",
    )
    op.drop_table("planning_resource_user_orders")
