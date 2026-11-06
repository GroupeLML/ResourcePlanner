"""Persist independent ERP scheduler observation timestamp.

Revision ID: 0033_erp_sync_scheduler_observation
Revises: 0032_erp_sync_plausibility_review_decisions
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0033_erp_sync_scheduler_observation"
down_revision: str | None = "0032_erp_sync_plausibility_review_decisions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "erp_sync_control",
        sa.Column("scheduler_observed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("erp_sync_control", "scheduler_observed_at")
