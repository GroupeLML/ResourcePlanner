"""Add Delivery persistence, board versioning and audit foundation.

Revision ID: 0046_delivery_persistence
Revises: 0045_approval_scope_resource_classes
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0046_delivery_persistence"
down_revision: Union[str, Sequence[str], None] = "0045_approval_scope_resource_classes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delivery_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_package_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'DRAFT'"), nullable=False),
        sa.Column("lead_user_id", sa.String(length=36), nullable=True),
        sa.Column("delivery_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('DRAFT','ACTIVE','ARCHIVED')", name="ck_delivery_plans_delivery_plan_status"),
        sa.CheckConstraint("delivery_version >= 1", name="ck_delivery_plans_delivery_plan_version_positive"),
        sa.ForeignKeyConstraint(["work_package_id"], ["work_packages.id"], name="fk_delivery_plans_work_package_id_work_packages"),
        sa.ForeignKeyConstraint(["lead_user_id"], ["app_users.id"], name="fk_delivery_plans_lead_user_id_app_users"),
        sa.PrimaryKeyConstraint("id", name="pk_delivery_plans"),
    )
    op.create_index(
        "ux_delivery_plans_work_package_non_archived",
        "delivery_plans",
        ["work_package_id"],
        unique=True,
        sqlite_where=sa.text("status <> 'ARCHIVED'"),
        postgresql_where=sa.text("status <> 'ARCHIVED'"),
        mssql_where=sa.text("status <> 'ARCHIVED'"),
    )
    op.create_index("ix_delivery_plans_lead_user", "delivery_plans", ["lead_user_id"], unique=False)

    op.create_table(
        "delivery_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("delivery_plan_id", sa.String(length=36), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("item_type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'BACKLOG'"), nullable=False),
        sa.Column("assignee_user_id", sa.String(length=36), nullable=True),
        sa.Column("current_estimate_hours", sa.Numeric(12, 2), nullable=True),
        sa.Column("reference_estimate_hours", sa.Numeric(12, 2), nullable=True),
        sa.Column("remaining_hours", sa.Numeric(12, 2), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sprint", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("item_type IN ('EPIC','STORY')", name="ck_delivery_items_delivery_item_type"),
        sa.CheckConstraint(
            "status IN ('BACKLOG','TODO','IN_PROGRESS','BLOCKED','DONE','CANCELLED')",
            name="ck_delivery_items_delivery_item_status",
        ),
        sa.CheckConstraint(
            "current_estimate_hours IS NULL OR current_estimate_hours > 0",
            name="ck_delivery_items_delivery_item_current_estimate_positive",
        ),
        sa.CheckConstraint(
            "reference_estimate_hours IS NULL OR reference_estimate_hours > 0",
            name="ck_delivery_items_delivery_item_reference_estimate_positive",
        ),
        sa.CheckConstraint(
            "remaining_hours IS NULL OR remaining_hours >= 0",
            name="ck_delivery_items_delivery_item_remaining_non_negative",
        ),
        sa.CheckConstraint("position >= 0", name="ck_delivery_items_delivery_item_position_non_negative"),
        sa.ForeignKeyConstraint(["delivery_plan_id"], ["delivery_plans.id"], name="fk_delivery_items_delivery_plan_id_delivery_plans"),
        sa.ForeignKeyConstraint(["parent_id"], ["delivery_items.id"], name="fk_delivery_items_parent_id_delivery_items"),
        sa.ForeignKeyConstraint(["assignee_user_id"], ["app_users.id"], name="fk_delivery_items_assignee_user_id_app_users"),
        sa.PrimaryKeyConstraint("id", name="pk_delivery_items"),
    )
    op.create_index(
        "ix_delivery_items_plan_status_position",
        "delivery_items",
        ["delivery_plan_id", "status", "position"],
        unique=False,
    )
    op.create_index(
        "ix_delivery_items_plan_parent",
        "delivery_items",
        ["delivery_plan_id", "parent_id"],
        unique=False,
    )
    op.create_index("ix_delivery_items_assignee", "delivery_items", ["assignee_user_id"], unique=False)

    op.create_table(
        "delivery_change_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("delivery_plan_id", sa.String(length=36), nullable=False),
        sa.Column("delivery_item_id", sa.String(length=36), nullable=True),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("delivery_version", sa.Integer(), nullable=False),
        sa.Column("details_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("delivery_version >= 1", name="ck_delivery_change_history_delivery_history_version_positive"),
        sa.ForeignKeyConstraint(["delivery_plan_id"], ["delivery_plans.id"], name="fk_delivery_change_history_delivery_plan_id_delivery_plans"),
        sa.ForeignKeyConstraint(["delivery_item_id"], ["delivery_items.id"], name="fk_delivery_change_history_delivery_item_id_delivery_items"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["app_users.id"], name="fk_delivery_change_history_actor_user_id_app_users"),
        sa.PrimaryKeyConstraint("id", name="pk_delivery_change_history"),
    )
    op.create_index(
        "ix_delivery_change_history_plan_occurred",
        "delivery_change_history",
        ["delivery_plan_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_change_history_plan_occurred", table_name="delivery_change_history")
    op.drop_table("delivery_change_history")
    op.drop_index("ix_delivery_items_assignee", table_name="delivery_items")
    op.drop_index("ix_delivery_items_plan_parent", table_name="delivery_items")
    op.drop_index("ix_delivery_items_plan_status_position", table_name="delivery_items")
    op.drop_table("delivery_items")
    op.drop_index("ix_delivery_plans_lead_user", table_name="delivery_plans")
    op.drop_index("ux_delivery_plans_work_package_non_archived", table_name="delivery_plans")
    op.drop_table("delivery_plans")
