"""Add idempotency identity to ERP synchronization attempts.

Revision ID: 0030_erp_sync_retry_identity
Revises: 0029_erp_sync_scheduler_control
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0030_erp_sync_retry_identity"
down_revision: str | None = "0029_erp_sync_scheduler_control"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "erp_sync_attempts",
        sa.Column("command_identity", sa.String(length=160), nullable=True),
    )
    op.create_unique_constraint(
        "uq_erp_sync_attempt_command_identity",
        "erp_sync_attempts",
        ["command_identity"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_erp_sync_attempt_command_identity",
        "erp_sync_attempts",
        type_="unique",
    )
    op.drop_column("erp_sync_attempts", "command_identity")
