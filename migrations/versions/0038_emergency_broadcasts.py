"""#894 emergency broadcast persistence foundation (additive).

Revision ID: 0038_emergency_broadcasts
Revises: 0037_workforce_request_project_manager_phone
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0038_emergency_broadcasts"
down_revision: str | None = "0037_workforce_request_project_manager_phone"
branch_labels: str | None = None
depends_on: str | None = None


def _utc_default(bind) -> sa.TextClause:
    if bind.dialect.name == "mssql":
        return sa.text("TODATETIMEOFFSET(SYSUTCDATETIME(), '+00:00')")
    return sa.text("CURRENT_TIMESTAMP")


def _set_mssql_filtered_index_options(bind) -> None:
    if bind.dialect.name != "mssql":
        return
    op.execute(
        sa.text(
            "SET ANSI_NULLS ON; "
            "SET ANSI_PADDING ON; "
            "SET ANSI_WARNINGS ON; "
            "SET ARITHABORT ON; "
            "SET CONCAT_NULL_YIELDS_NULL ON; "
            "SET QUOTED_IDENTIFIER ON; "
            "SET NUMERIC_ROUNDABORT OFF"
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    utc_default = _utc_default(bind)

    op.create_table(
        "emergency_broadcasts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("title", sa.Unicode(160), nullable=False),
        sa.Column("message", sa.Unicode(4000), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("creation_command_id", sa.String(36), nullable=False),
        sa.Column("creation_payload_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=utc_default, nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), server_default=utc_default, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by_user_id", sa.String(36), nullable=True),
        sa.CheckConstraint("version = 1", name="version_one"),
        sa.CheckConstraint(
            "state IN ('ACTIVE', 'ENDED')",
            name="state_valid",
        ),
        sa.CheckConstraint(
            "(state = 'ACTIVE' AND ended_at IS NULL AND ended_by_user_id IS NULL) "
            "OR (state = 'ENDED' AND ended_at IS NOT NULL AND ended_by_user_id IS NOT NULL)",
            name="end_fields_match_state",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= activated_at",
            name="ended_after_activation",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_users.id"],
            name="fk_emergency_broadcasts_created_by_user_id_app_users",
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"],
            ["app_users.id"],
            name="fk_emergency_broadcasts_ended_by_user_id_app_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_emergency_broadcasts"),
    )
    op.create_index(
        "ux_emergency_broadcasts_creator_command",
        "emergency_broadcasts",
        ["created_by_user_id", "creation_command_id"],
        unique=True,
    )
    _set_mssql_filtered_index_options(bind)
    op.create_index(
        "ux_emergency_broadcasts_single_active",
        "emergency_broadcasts",
        ["state"],
        unique=True,
        sqlite_where=sa.text("state = 'ACTIVE'"),
        mssql_where=sa.text("state = 'ACTIVE'"),
    )
    op.create_index(
        "ix_emergency_broadcasts_history",
        "emergency_broadcasts",
        ["state", "ended_at", "id"],
        unique=False,
    )

    op.create_table(
        "emergency_broadcast_acknowledgments",
        sa.Column("broadcast_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), server_default=utc_default, nullable=False),
        sa.ForeignKeyConstraint(
            ["broadcast_id"],
            ["emergency_broadcasts.id"],
            name="fk_emergency_broadcast_acknowledgments_broadcast_id_emergency_broadcasts",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_users.id"],
            name="fk_emergency_broadcast_acknowledgments_user_id_app_users",
        ),
        sa.PrimaryKeyConstraint(
            "broadcast_id",
            "user_id",
            name="pk_emergency_broadcast_acknowledgments",
        ),
    )


def downgrade() -> None:
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError(
            "0038_emergency_broadcasts refuses offline downgrade generation because "
            "durable #894 data cannot be inspected safely."
        )

    bind = op.get_bind()
    protected_tables = (
        "emergency_broadcast_acknowledgments",
        "emergency_broadcasts",
    )
    populated: list[str] = []
    for table_name in protected_tables:
        count = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table_name}")
        ).scalar_one()
        if int(count or 0) > 0:
            populated.append(table_name)
    if populated:
        raise RuntimeError(
            "0038_emergency_broadcasts refuses downgrade while durable #894 "
            "broadcast/acknowledgment data exists in: "
            + ", ".join(populated)
        )

    op.drop_table("emergency_broadcast_acknowledgments")
    op.drop_table("emergency_broadcasts")
