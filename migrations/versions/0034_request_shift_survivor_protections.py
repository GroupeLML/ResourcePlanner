"""ADR-034 #793 durable survivor Shift protection authority.

Revision ID: 0034_request_shift_survivor_protections
Revises: 0033_erp_sync_scheduler_observation
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0034_request_shift_survivor_protections"
down_revision: str | None = "0033_erp_sync_scheduler_observation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Composite uniqueness lets the protection FK prove that its originating
    # reduction belongs to the exact same durable PlanningIntent.
    with op.batch_alter_table("request_completion_reductions") as batch:
        batch.create_unique_constraint(
            "uq_request_completion_reduction_id_intent",
            ["id", "planning_intent_id"],
        )

    op.create_table(
        "request_shift_survivor_protections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("planning_intent_id", sa.String(length=36), nullable=False),
        sa.Column("shift_id", sa.String(length=36), nullable=True),
        sa.Column("original_shift_id", sa.String(length=36), nullable=False),
        sa.Column("originating_reduction_id", sa.String(length=36), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(active = 1 AND shift_id IS NOT NULL AND retired_at IS NULL) OR "
            "(active = 0 AND retired_at IS NOT NULL)",
            name="request_shift_survivor_protection_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["planning_intent_id"],
            ["request_requirement_planning_intents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["shift_id"],
            ["shifts.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["originating_reduction_id", "planning_intent_id"],
            [
                "request_completion_reductions.id",
                "request_completion_reductions.planning_intent_id",
            ],
            name="fk_request_shift_survivor_protection_reduction_intent",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_request_shift_survivor_protection_intent_active",
        "request_shift_survivor_protections",
        ["planning_intent_id", "active"],
        unique=False,
    )
    op.create_index(
        "uq_request_shift_survivor_protection_active_shift",
        "request_shift_survivor_protections",
        ["shift_id"],
        unique=True,
        sqlite_where=sa.text("active = 1 AND shift_id IS NOT NULL"),
        mssql_where=sa.text("active = 1 AND shift_id IS NOT NULL"),
    )


def downgrade() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "0034_request_shift_survivor_protections refuses offline downgrade "
            "because #793 survivor authority cannot be inspected safely."
        )

    connection = op.get_bind()
    count = connection.execute(
        sa.text("SELECT COUNT(*) FROM request_shift_survivor_protections")
    ).scalar_one()
    if int(count or 0) > 0:
        raise RuntimeError(
            "0034_request_shift_survivor_protections refuses downgrade while "
            "durable survivor provenance exists."
        )

    op.drop_index(
        "uq_request_shift_survivor_protection_active_shift",
        table_name="request_shift_survivor_protections",
    )
    op.drop_index(
        "ix_request_shift_survivor_protection_intent_active",
        table_name="request_shift_survivor_protections",
    )
    op.drop_table("request_shift_survivor_protections")

    with op.batch_alter_table("request_completion_reductions") as batch:
        batch.drop_constraint(
            "uq_request_completion_reduction_id_intent",
            type_="unique",
        )
