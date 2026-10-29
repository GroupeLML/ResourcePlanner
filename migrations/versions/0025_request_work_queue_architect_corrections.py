"""ADR-034 architect correction: logical slot identity and reconciliation provenance.

Revision ID: 0025_request_work_queue_architect_corrections
Revises: 0024_request_work_queue_command_identity
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision: str = "0025_request_work_queue_architect_corrections"
down_revision: str | None = "0024_request_work_queue_command_identity"
branch_labels: str | None = None
depends_on: str | None = None


def _normalize_existing_slot_keys() -> None:
    context = op.get_context()
    if context.as_sql:
        return
    connection = op.get_bind()
    duplicates = connection.execute(
        sa.text(
            """
            SELECT workforce_request_id, approved_entry_key, slot_index, COUNT(*) AS row_count
            FROM request_requirement_planning_intents
            GROUP BY workforce_request_id, approved_entry_key, slot_index
            HAVING COUNT(*) > 1
            """
        )
    ).mappings().all()
    if duplicates:
        raise RuntimeError(
            "ADR-034 logical slot migration is ambiguous; duplicate durable slots exist: "
            + ", ".join(
                f"{row['workforce_request_id']}/{row['approved_entry_key']}/{row['slot_index']}"
                for row in duplicates
            )
        )
    rows = connection.execute(
        sa.text(
            """
            SELECT id, workforce_request_id, approved_entry_key, slot_index
            FROM request_requirement_planning_intents
            """
        )
    ).mappings().all()
    for row in rows:
        key = json.dumps(
            [
                str(row["workforce_request_id"]),
                str(row["approved_entry_key"]),
                int(row["slot_index"]),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        connection.execute(
            sa.text(
                """
                UPDATE request_requirement_planning_intents
                SET slot_identity_key = :slot_identity_key
                WHERE id = :intent_id
                """
            ),
            {"slot_identity_key": key, "intent_id": row["id"]},
        )


def _backfill_reduction_watermarks() -> None:
    context = op.get_context()
    if context.as_sql:
        return
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE request_completion_reductions
            SET authorization_fingerprint = (
                SELECT authorization_fingerprint
                FROM request_approval_revisions
                WHERE request_approval_revisions.id =
                    request_completion_reductions.historical_approval_revision_id
            ),
                reconciled_approval_revision_id = historical_approval_revision_id
            WHERE authorization_fingerprint IS NULL
               OR reconciled_approval_revision_id IS NULL
            """
        )
    )


def upgrade() -> None:
    with op.batch_alter_table("request_completion_reductions") as batch:
        batch.add_column(
            sa.Column("authorization_fingerprint", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "reconciled_approval_revision_id",
                sa.String(length=36),
                nullable=True,
            )
        )
        batch.create_foreign_key(
            "fk_request_completion_reductions_reconciled_revision",
            "request_approval_revisions",
            ["reconciled_approval_revision_id"],
            ["id"],
        )

    _normalize_existing_slot_keys()
    _backfill_reduction_watermarks()

    with op.batch_alter_table("request_requirement_planning_intents") as batch:
        batch.create_unique_constraint(
            "uq_request_requirement_planning_intent_logical_slot",
            ["workforce_request_id", "approved_entry_key", "slot_index"],
        )

    op.create_index(
        "uq_request_requirement_planning_intent_current_requirement",
        "request_requirement_planning_intents",
        ["current_resource_requirement_id"],
        unique=True,
        sqlite_where=sa.text("current_resource_requirement_id IS NOT NULL"),
        mssql_where=sa.text("current_resource_requirement_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_request_requirement_planning_intent_current_requirement",
        table_name="request_requirement_planning_intents",
    )
    with op.batch_alter_table("request_requirement_planning_intents") as batch:
        batch.drop_constraint(
            "uq_request_requirement_planning_intent_logical_slot",
            type_="unique",
        )
    with op.batch_alter_table("request_completion_reductions") as batch:
        batch.drop_constraint(
            "fk_request_completion_reductions_reconciled_revision",
            type_="foreignkey",
        )
        batch.drop_column("reconciled_approval_revision_id")
        batch.drop_column("authorization_fingerprint")
