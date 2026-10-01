"""Persist Acumatica project-task global synchronization runs.

Revision ID: 0005_task_sync_runs
Revises: 0004_asset_approval_authority
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0005_task_sync_runs"
down_revision: str | None = "0004_asset_approval_authority"
branch_labels: str | None = None
depends_on: str | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def upgrade() -> None:
    created_at, updated_at = _timestamps()
    op.create_table(
        "acumatica_project_task_sync_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("active_key", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projects_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("projects_processed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("projects_synchronized", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("projects_ignored", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("projects_rejected", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_requests", sa.Integer(), nullable=True),
        sa.Column("source_rows_scanned", sa.Integer(), nullable=True),
        sa.Column("source_read_duration_ms", sa.Integer(), nullable=True),
        sa.Column("source_rows_received", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_rows_rejected", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tasks_received", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tasks_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tasks_updated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tasks_unchanged", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tasks_deactivated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tasks_rejected", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("diagnostic", sa.Text(), nullable=True),
        created_at,
        updated_at,
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','COMPLETED_WITH_ERRORS','FAILED','INTERRUPTED')",
            name="status_values",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_acumatica_project_task_sync_runs"),
    )
    op.create_index(
        "ix_acumatica_project_task_sync_runs_status",
        "acumatica_project_task_sync_runs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_acumatica_project_task_sync_runs_created",
        "acumatica_project_task_sync_runs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ux_acumatica_project_task_sync_runs_active",
        "acumatica_project_task_sync_runs",
        ["active_key"],
        unique=True,
        sqlite_where=sa.text("active_key IS NOT NULL"),
        mssql_where=sa.text("active_key IS NOT NULL"),
    )

    result_created_at, result_updated_at = _timestamps()
    op.create_table(
        "acumatica_project_task_sync_project_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("project_number", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("reason_code", sa.String(length=128), nullable=True),
        sa.Column("source_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("source_rows_rejected", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tasks_received", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("unchanged", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deactivated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rejected", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        result_created_at,
        result_updated_at,
        sa.CheckConstraint(
            "status IN ('synchronized','ignored','rejected')",
            name="status_values",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["acumatica_project_task_sync_runs.id"],
            name="fk_acumatica_project_task_sync_project_results_run_id_acumatica_project_task_sync_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_acumatica_project_task_sync_project_results",
        ),
        sa.UniqueConstraint(
            "run_id",
            "project_id",
            name="uq_acumatica_project_task_sync_project_result",
        ),
    )
    op.create_index(
        "ix_acumatica_project_task_sync_project_results_run",
        "acumatica_project_task_sync_project_results",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_acumatica_project_task_sync_project_results_run",
        table_name="acumatica_project_task_sync_project_results",
    )
    op.drop_table("acumatica_project_task_sync_project_results")

    op.drop_index(
        "ux_acumatica_project_task_sync_runs_active",
        table_name="acumatica_project_task_sync_runs",
    )
    op.drop_index(
        "ix_acumatica_project_task_sync_runs_created",
        table_name="acumatica_project_task_sync_runs",
    )
    op.drop_index(
        "ix_acumatica_project_task_sync_runs_status",
        table_name="acumatica_project_task_sync_runs",
    )
    op.drop_table("acumatica_project_task_sync_runs")
