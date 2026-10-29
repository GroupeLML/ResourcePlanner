"""ADR-034 architect correction: logical slot identity and reconciliation provenance.

Revision ID: 0025_request_work_queue_architect_corrections
Revises: 0024_request_work_queue_command_identity
"""
from __future__ import annotations

import hashlib
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
    rows = connection.execute(
        sa.text(
            """
            SELECT
                r.id AS reduction_id,
                r.approved_entry_key AS approved_entry_key,
                r.historical_approval_revision_id AS revision_id,
                v.payload_text AS payload_text
            FROM request_completion_reductions AS r
            JOIN request_approval_revisions AS v
              ON v.id = r.historical_approval_revision_id
            """
        )
    ).mappings().all()
    request_keys = ("project_id", "site_client", "location", "line_mode")
    entry_keys = (
        "identity",
        "project_id",
        "site_id",
        "location",
        "slot_count",
        "line_kind",
        "required_resource_class",
        "competency_ids",
        "task_ref",
        "work_package_ref",
        "start_date",
        "end_date",
        "hours",
        "kind",
        "group",
        "confirmation_mode",
        "proposed_resource_mode",
        "same_as_period_key",
        "asset_type_id",
        "occupancy_policy",
    )
    for row in rows:
        payload = json.loads(row["payload_text"])
        request_context = (
            payload.get("request")
            if isinstance(payload.get("request"), dict)
            else {}
        )
        entries = payload.get("authorization", {}).get("entries", [])
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and str(entry.get("identity") or "") == str(row["approved_entry_key"])
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "ADR-034 reduction migration cannot identify the historical approved entry "
                f"for reduction {row['reduction_id']}."
            )
        entry = matches[0]
        canonical = {
            "request": {
                key: request_context.get(key)
                for key in request_keys
            },
            "entry": {
                key: entry.get(key)
                for key in entry_keys
            },
        }
        competencies = canonical["entry"].get("competency_ids")
        if isinstance(competencies, list):
            canonical["entry"]["competency_ids"] = sorted(
                str(value) for value in competencies if str(value)
            )
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        connection.execute(
            sa.text(
                """
                UPDATE request_completion_reductions
                SET authorization_fingerprint = :fingerprint,
                    reconciled_approval_revision_id = :revision_id
                WHERE id = :reduction_id
                """
            ),
            {
                "fingerprint": fingerprint,
                "revision_id": row["revision_id"],
                "reduction_id": row["reduction_id"],
            },
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
    context = op.get_context()
    if not context.as_sql:
        connection = op.get_bind()
        protected_tables = (
            "request_requirement_planning_intents",
            "planning_work_queue_releases",
            "request_completion_reductions",
        )
        populated = []
        for table_name in protected_tables:
            count = connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {table_name}")
            ).scalar_one()
            if int(count or 0) > 0:
                populated.append(table_name)
        if populated:
            raise RuntimeError(
                "0025_request_work_queue_architect_corrections refuses downgrade "
                "while ADR-034 durable planning/reduction provenance exists in: "
                + ", ".join(populated)
            )

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
