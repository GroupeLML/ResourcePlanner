"""Add persistent operational planning-window overrides.

Revision ID: 0013_planning_window_overrides
Revises: 0012_holiday_resource_classes
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0013_planning_window_overrides"
down_revision: str | None = "0012_holiday_resource_classes"
branch_labels: str | None = None
depends_on: str | None = None


TABLE = "planning_window_overrides"
ACTIVE_REQUIREMENT_INDEX = "ux_planning_window_overrides_active_requirement"


def upgrade() -> None:
    active = sa.text("status = 'ACTIVE'")
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workforce_request_id", sa.String(length=36), nullable=False),
        sa.Column("resource_requirement_id", sa.String(length=36), nullable=False),
        sa.Column("approval_revision_id", sa.String(length=36), nullable=False),
        sa.Column("approved_entry_key", sa.String(length=512), nullable=False),
        sa.Column("approved_start_date", sa.Date(), nullable=False),
        sa.Column("approved_end_date", sa.Date(), nullable=False),
        sa.Column("effective_start_date", sa.Date(), nullable=False),
        sa.Column("effective_end_date", sa.Date(), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("resolved_by_revision_id", sa.String(length=36), nullable=True),
        sa.Column("resolution_reason", sa.String(length=64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "approved_end_date >= approved_start_date",
            name=op.f("ck_planning_window_overrides_planning_window_override_approved_window"),
        ),
        sa.CheckConstraint(
            "effective_end_date >= effective_start_date",
            name=op.f("ck_planning_window_overrides_planning_window_override_effective_window"),
        ),
        sa.CheckConstraint(
            "effective_start_date <= approved_start_date "
            "AND effective_end_date >= approved_end_date",
            name=op.f("ck_planning_window_overrides_planning_window_override_widens_approved_window"),
        ),
        sa.CheckConstraint(
            "effective_start_date < approved_start_date "
            "OR effective_end_date > approved_end_date",
            name=op.f("ck_planning_window_overrides_planning_window_override_strict_widening"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'ABSORBED', 'SUPERSEDED')",
            name=op.f("ck_planning_window_overrides_planning_window_override_status"),
        ),
        sa.ForeignKeyConstraint(
            ["workforce_request_id"],
            ["workforce_requests.id"],
            name=op.f("fk_planning_window_overrides_workforce_request_id_workforce_requests"),
        ),
        sa.ForeignKeyConstraint(
            ["resource_requirement_id"],
            ["resource_requirements.id"],
            name=op.f("fk_planning_window_overrides_resource_requirement_id_resource_requirements"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_revision_id"],
            ["request_approval_revisions.id"],
            name=op.f("fk_planning_window_overrides_approval_revision_id_request_approval_revisions"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_users.id"],
            name=op.f("fk_planning_window_overrides_actor_user_id_app_users"),
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_revision_id"],
            ["request_approval_revisions.id"],
            name=op.f("fk_planning_window_overrides_resolved_by_revision_id_request_approval_revisions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planning_window_overrides")),
    )
    op.create_index(
        "ix_planning_window_overrides_revision_entry_status",
        TABLE,
        ["approval_revision_id", "approved_entry_key", "status"],
        unique=False,
    )
    op.create_index(
        "ix_planning_window_overrides_request_status",
        TABLE,
        ["workforce_request_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_planning_window_overrides_actor_correlation",
        TABLE,
        ["actor_user_id", "correlation_id"],
        unique=False,
    )
    op.create_index(
        ACTIVE_REQUIREMENT_INDEX,
        TABLE,
        ["resource_requirement_id"],
        unique=True,
        sqlite_where=active,
        postgresql_where=active,
        mssql_where=active,
    )


def downgrade() -> None:
    op.drop_table(TABLE)
