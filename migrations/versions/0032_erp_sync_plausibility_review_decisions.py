"""Add durable plausibility-review decline audit.

Revision ID: 0032_erp_sync_plausibility_review_decisions
Revises: 0031_erp_sync_evidence_checkpoint
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0032_erp_sync_plausibility_review_decisions"
down_revision: str | None = "0031_erp_sync_evidence_checkpoint"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "erp_sync_plausibility_review_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor_name", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_identity_json", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('DECLINED')",
            name="erp_sync_plausibility_review_decision_values",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["erp_sync_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id",
            name="uq_erp_sync_plausibility_review_decision_attempt",
        ),
    )
    op.create_index(
        "ix_erp_sync_plausibility_review_decision_attempt",
        "erp_sync_plausibility_review_decisions",
        ["attempt_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_erp_sync_plausibility_review_decision_attempt",
        table_name="erp_sync_plausibility_review_decisions",
    )
    op.drop_table("erp_sync_plausibility_review_decisions")
