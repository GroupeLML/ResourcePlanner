"""Add ADR-033 ERP scheduler/orchestration control plane.

Revision ID: 0029_erp_sync_scheduler_control
Revises: 0028_resource_residence_city
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0029_erp_sync_scheduler_control"
down_revision: str | None = "0028_resource_residence_city"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "erp_sync_control",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("owner_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_business_date", sa.Date(), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("last_success_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO erp_sync_control "
            "(id, generation, protocol_version, created_at, updated_at) "
            "VALUES ('ERP_PROJECTS_TASKS', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    )

    op.create_table(
        "erp_sync_occurrences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("occurrence_key", sa.String(length=160), nullable=False),
        sa.Column("occurrence_type", sa.String(length=16), nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("scheduled_local_time", sa.String(length=5), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("command_identity", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("success_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("diagnostic", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "occurrence_type IN ('DAILY','MANUAL')",
            name="ck_erp_sync_occurrence_type_values",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','MISSED','DISABLED','SUCCEEDED','SUCCEEDED_WITH_ERRORS','FAILED','INTERRUPTED')",
            name="ck_erp_sync_occurrence_status_values",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("occurrence_key", name="uq_erp_sync_occurrence_key"),
        sa.UniqueConstraint("command_identity", name="uq_erp_sync_command_identity"),
    )
    op.create_index(
        "ix_erp_sync_occurrence_business_date",
        "erp_sync_occurrences",
        ["business_date"],
    )

    op.create_table(
        "erp_sync_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("occurrence_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("actor_name", sa.String(length=255), nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("project_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("projects_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("projects_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("projects_unchanged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("task_sync_run_id", sa.String(length=36), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("diagnostic", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trigger IN ('SCHEDULED','CATCHUP','MANUAL','RETRY')",
            name="ck_erp_sync_attempt_trigger_values",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','SUCCEEDED_WITH_ERRORS','FAILED','INTERRUPTED')",
            name="ck_erp_sync_attempt_status_values",
        ),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["erp_sync_occurrences.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "occurrence_id",
            "attempt_number",
            name="uq_erp_sync_attempt_number",
        ),
    )
    op.create_index(
        "ix_erp_sync_attempt_occurrence",
        "erp_sync_attempts",
        ["occurrence_id"],
    )
    op.create_index(
        "ix_erp_sync_attempt_status",
        "erp_sync_attempts",
        ["status"],
    )

    op.add_column(
        "acumatica_project_task_sync_runs",
        sa.Column("parent_attempt_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "acumatica_project_task_sync_runs",
        sa.Column("erp_generation", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_acumatica_project_task_sync_runs_parent_attempt",
        "acumatica_project_task_sync_runs",
        ["parent_attempt_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_acumatica_project_task_sync_runs_parent_attempt",
        table_name="acumatica_project_task_sync_runs",
    )
    op.drop_column("acumatica_project_task_sync_runs", "erp_generation")
    op.drop_column("acumatica_project_task_sync_runs", "parent_attempt_id")
    op.drop_index("ix_erp_sync_attempt_status", table_name="erp_sync_attempts")
    op.drop_index("ix_erp_sync_attempt_occurrence", table_name="erp_sync_attempts")
    op.drop_table("erp_sync_attempts")
    op.drop_index(
        "ix_erp_sync_occurrence_business_date",
        table_name="erp_sync_occurrences",
    )
    op.drop_table("erp_sync_occurrences")
    op.drop_table("erp_sync_control")
