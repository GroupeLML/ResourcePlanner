"""Persist ERP budget freshness on each task catalog entry.

Revision ID: 0016_task_erp_budget_freshness
Revises: 0015_verification_executions
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0016_task_erp_budget_freshness"
down_revision: str | None = "0015_verification_executions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "task_catalog_items",
        sa.Column("erp_budget_last_success_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_task_catalog_items_erp_budget_last_success_at",
        "task_catalog_items",
        ["erp_budget_last_success_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_catalog_items_erp_budget_last_success_at",
        table_name="task_catalog_items",
    )
    op.drop_column("task_catalog_items", "erp_budget_last_success_at")
