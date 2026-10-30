"""ADR-034 durable request planning intent, queue returns and completion reductions.

Revision ID: 0023_request_work_queue_intent
Revises: 0022_continuous_vehicle_resource_period
"""
from __future__ import annotations

from alembic import op
import json
import sqlalchemy as sa


revision: str = "0023_request_work_queue_intent"
down_revision: str | None = "0022_continuous_vehicle_resource_period"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "request_requirement_planning_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slot_identity_key", sa.String(length=768), nullable=False),
        sa.Column("workforce_request_id", sa.String(length=36), nullable=False),
        sa.Column("source_request_line_id", sa.String(length=36), nullable=True),
        sa.Column("approval_revision_id", sa.String(length=36), nullable=True),
        sa.Column("approved_entry_key", sa.String(length=512), nullable=False),
        sa.Column("source_period_id", sa.String(length=36), nullable=True),
        sa.Column("slot_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("current_resource_requirement_id", sa.String(length=36), nullable=True),
        sa.Column("intent_mode", sa.String(length=32), server_default=sa.text("'AUTO'"), nullable=False),
        sa.Column("target_mode", sa.String(length=32), server_default=sa.text("'INHERIT'"), nullable=False),
        sa.Column("target_resource_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("slot_index >= 0", name="request_requirement_planning_slot_index"),
        sa.CheckConstraint(
            "intent_mode IN ('AUTO','PRESERVE_EXISTING','QUEUED')",
            name="request_requirement_planning_intent_mode",
        ),
        sa.CheckConstraint(
            "target_mode IN ('INHERIT','EXPLICIT_RESOURCE','EXPLICIT_NONE')",
            name="request_requirement_planning_target_mode",
        ),
        sa.CheckConstraint(
            "(target_mode = 'EXPLICIT_RESOURCE' AND target_resource_id IS NOT NULL) OR "
            "(target_mode != 'EXPLICIT_RESOURCE' AND target_resource_id IS NULL)",
            name="request_requirement_planning_target_resource",
        ),
        sa.ForeignKeyConstraint(["approval_revision_id"], ["request_approval_revisions.id"]),
        sa.ForeignKeyConstraint(["current_resource_requirement_id"], ["resource_requirements.id"]),
        sa.ForeignKeyConstraint(["source_period_id"], ["workforce_request_periods.id"]),
        sa.ForeignKeyConstraint(["source_request_line_id"], ["request_lines.id"]),
        sa.ForeignKeyConstraint(["target_resource_id"], ["resources.id"]),
        sa.ForeignKeyConstraint(["workforce_request_id"], ["workforce_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slot_identity_key", name="uq_request_requirement_planning_intent_slot"),
    )
    op.create_index(
        "ix_request_requirement_planning_intent_request",
        "request_requirement_planning_intents",
        ["workforce_request_id"],
    )
    op.create_index(
        "ix_request_requirement_planning_intent_requirement",
        "request_requirement_planning_intents",
        ["current_resource_requirement_id"],
    )

    if not op.get_context().as_sql:
        # Backfill only legacy REQUEST slots whose approved-entry identity is
        # unambiguous. Multi-slot legacy entries intentionally remain without an
        # intent and therefore fail closed until explicitly regularized.
        connection = op.get_bind()
        groups = connection.execute(sa.text(
            "SELECT workforce_request_id, approved_entry_key "
            "FROM resource_requirements "
            "WHERE origin = 'REQUEST' AND status <> 'Annulé' "
            "AND workforce_request_id IS NOT NULL "
            "AND approved_entry_key IS NOT NULL "
            "AND approval_revision_id IS NOT NULL "
            "GROUP BY workforce_request_id, approved_entry_key HAVING COUNT(*) = 1"
        )).mappings().all()
        for group in groups:
            row = connection.execute(
                sa.text(
                    "SELECT r.id, r.workforce_request_id, r.source_request_line_id, "
                    "r.approval_revision_id, r.approved_entry_key, p.period_id "
                    "FROM resource_requirements r "
                    "LEFT JOIN workforce_request_period_requirements p "
                    "ON p.resource_requirement_id = r.id "
                    "WHERE r.workforce_request_id = :request_id "
                    "AND r.approved_entry_key = :entry_key "
                    "AND r.origin = 'REQUEST' AND r.status <> 'Annulé'"
                ),
                {
                    "request_id": group["workforce_request_id"],
                    "entry_key": group["approved_entry_key"],
                },
            ).mappings().one()
            slot_key = json.dumps(
                [
                    str(row["workforce_request_id"]),
                    str(row["source_request_line_id"] or ""),
                    str(row["approved_entry_key"]),
                    str(row["period_id"] or ""),
                    0,
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            connection.execute(
                sa.text(
                    "INSERT INTO request_requirement_planning_intents "
                    "(id, slot_identity_key, workforce_request_id, source_request_line_id, "
                    "approval_revision_id, approved_entry_key, source_period_id, slot_index, "
                    "current_resource_requirement_id, intent_mode, target_mode, target_resource_id, "
                    "created_at, updated_at) "
                    "VALUES (:id, :slot_key, :request_id, :line_id, :revision_id, :entry_key, "
                    ":period_id, 0, :requirement_id, 'AUTO', 'INHERIT', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": row["id"],
                    "slot_key": slot_key,
                    "request_id": row["workforce_request_id"],
                    "line_id": row["source_request_line_id"],
                    "revision_id": row["approval_revision_id"],
                    "entry_key": row["approved_entry_key"],
                    "period_id": row["period_id"],
                    "requirement_id": row["id"],
                },
            )


    op.create_table(
        "planning_work_queue_releases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("planning_intent_id", sa.String(length=36), nullable=False),
        sa.Column("resource_requirement_id", sa.String(length=36), nullable=True),
        sa.Column("original_shift_id", sa.String(length=36), nullable=False),
        sa.Column("workforce_request_id", sa.String(length=36), nullable=False),
        sa.Column("approval_revision_id", sa.String(length=36), nullable=True),
        sa.Column("approved_entry_key", sa.String(length=512), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("hours", sa.Numeric(12, 2), nullable=False),
        sa.Column("shift_snapshot_text", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(length=64), server_default=sa.text("'RETURN_SHIFT_TO_WORK_QUEUE'"), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("hours > 0", name="planning_work_queue_release_hours_positive"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["app_users.id"]),
        sa.ForeignKeyConstraint(["approval_revision_id"], ["request_approval_revisions.id"]),
        sa.ForeignKeyConstraint(["planning_intent_id"], ["request_requirement_planning_intents.id"]),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["app_users.id"]),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"]),
        sa.ForeignKeyConstraint(["workforce_request_id"], ["workforce_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_planning_work_queue_release_slot_active",
        "planning_work_queue_releases",
        ["planning_intent_id", "active"],
    )
    op.create_index(
        "ix_planning_work_queue_release_exclusion",
        "planning_work_queue_releases",
        ["resource_requirement_id", "resource_id", "work_date"],
    )
    op.create_index(
        "ix_planning_work_queue_releases_command_id",
        "planning_work_queue_releases",
        ["command_id"],
    )

    op.create_table(
        "request_completion_reductions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("planning_intent_id", sa.String(length=36), nullable=False),
        sa.Column("workforce_request_id", sa.String(length=36), nullable=False),
        sa.Column("historical_approval_revision_id", sa.String(length=36), nullable=False),
        sa.Column("source_request_line_id", sa.String(length=36), nullable=True),
        sa.Column("approved_entry_key", sa.String(length=512), nullable=False),
        sa.Column("resource_requirement_id", sa.String(length=36), nullable=True),
        sa.Column("original_shift_id", sa.String(length=36), nullable=False),
        sa.Column("shift_snapshot_text", sa.Text(), nullable=False),
        sa.Column("reduction_hours", sa.Numeric(12, 2), nullable=False),
        sa.Column("effective_need_before", sa.Numeric(12, 2), nullable=False),
        sa.Column("effective_need_after", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.String(length=64), server_default=sa.text("'LESS_WORK_REQUIRED'"), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("reconciliation_state", sa.String(length=32), server_default=sa.text("'ACTIVE'"), nullable=False),
        sa.Column("absorbed_by_revision_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reduction_hours > 0", name="request_completion_reduction_hours_positive"),
        sa.CheckConstraint(
            "effective_need_before >= effective_need_after AND effective_need_after >= 0",
            name="request_completion_reduction_need_order",
        ),
        sa.CheckConstraint(
            "reconciliation_state IN ('ACTIVE','ABSORBED','SUPERSEDED')",
            name="request_completion_reduction_state",
        ),
        sa.ForeignKeyConstraint(["absorbed_by_revision_id"], ["request_approval_revisions.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["app_users.id"]),
        sa.ForeignKeyConstraint(["historical_approval_revision_id"], ["request_approval_revisions.id"]),
        sa.ForeignKeyConstraint(["planning_intent_id"], ["request_requirement_planning_intents.id"]),
        sa.ForeignKeyConstraint(["source_request_line_id"], ["request_lines.id"]),
        sa.ForeignKeyConstraint(["workforce_request_id"], ["workforce_requests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_request_completion_reduction_slot_state",
        "request_completion_reductions",
        ["planning_intent_id", "reconciliation_state"],
    )
    op.create_index(
        "ix_request_completion_reduction_request",
        "request_completion_reductions",
        ["workforce_request_id"],
    )
    op.create_index(
        "ix_request_completion_reductions_command_id",
        "request_completion_reductions",
        ["command_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_request_completion_reductions_command_id", table_name="request_completion_reductions")
    op.drop_index("ix_request_completion_reduction_request", table_name="request_completion_reductions")
    op.drop_index("ix_request_completion_reduction_slot_state", table_name="request_completion_reductions")
    op.drop_table("request_completion_reductions")

    op.drop_index("ix_planning_work_queue_releases_command_id", table_name="planning_work_queue_releases")
    op.drop_index("ix_planning_work_queue_release_exclusion", table_name="planning_work_queue_releases")
    op.drop_index("ix_planning_work_queue_release_slot_active", table_name="planning_work_queue_releases")
    op.drop_table("planning_work_queue_releases")

    op.drop_index("ix_request_requirement_planning_intent_requirement", table_name="request_requirement_planning_intents")
    op.drop_index("ix_request_requirement_planning_intent_request", table_name="request_requirement_planning_intents")
    op.drop_table("request_requirement_planning_intents")
