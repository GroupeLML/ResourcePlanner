"""ADR-034 cumulative work-queue restoration attribution.

Revision ID: 0026_request_work_queue_restorations
Revises: 0025_request_work_queue_architect_corrections
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0026_request_work_queue_restorations"
down_revision: str | None = "0025_request_work_queue_architect_corrections"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "planning_work_queue_restorations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("release_id", sa.String(length=36), nullable=False),
        sa.Column("shift_id", sa.String(length=36), nullable=False),
        sa.Column("restored_hours", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "restored_hours > 0",
            name="planning_work_queue_restoration_hours_positive",
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["planning_work_queue_releases.id"],
            name="fk_planning_work_queue_restorations_release",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "shift_id",
            name="uq_planning_work_queue_restoration_shift",
        ),
    )
    op.create_index(
        "ix_planning_work_queue_restoration_release",
        "planning_work_queue_restorations",
        ["release_id"],
        unique=False,
    )


def downgrade() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "0026_request_work_queue_restorations refuses offline downgrade "
            "because ADR-034 restoration provenance cannot be inspected safely."
        )
    connection = op.get_bind()
    count = connection.execute(
        sa.text("SELECT COUNT(*) FROM planning_work_queue_restorations")
    ).scalar_one()
    if int(count or 0) > 0:
        raise RuntimeError(
            "0026_request_work_queue_restorations refuses downgrade while "
            "durable restoration provenance exists."
        )
    op.drop_index(
        "ix_planning_work_queue_restoration_release",
        table_name="planning_work_queue_restorations",
    )
    op.drop_table("planning_work_queue_restorations")
