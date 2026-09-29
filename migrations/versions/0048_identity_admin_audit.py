"""Add durable identity administration audit.

Revision ID: 0048_identity_admin_audit
Revises: 0047_preprovision_app_users
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0048_identity_admin_audit"
down_revision: str | None = "0047_preprovision_app_users"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "identity_admin_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("target_user_id", sa.String(length=36), nullable=False),
        sa.Column("erp_user_id", sa.String(length=128), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("old_state_json", sa.Text(), nullable=False),
        sa.Column("new_state_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_users.id"],
            name="fk_identity_admin_audit_actor_user_id_app_users",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["app_users.id"],
            name="fk_identity_admin_audit_target_user_id_app_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_identity_admin_audit"),
    )
    op.create_index(
        "ix_identity_admin_audit_actor_user_id",
        "identity_admin_audit",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_identity_admin_audit_target_user_id",
        "identity_admin_audit",
        ["target_user_id"],
    )
    op.create_index(
        "ix_identity_admin_audit_erp_user_id",
        "identity_admin_audit",
        ["erp_user_id"],
    )
    op.create_index(
        "ix_identity_admin_audit_action",
        "identity_admin_audit",
        ["action"],
    )
    op.create_index(
        "ix_identity_admin_audit_created_at",
        "identity_admin_audit",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_identity_admin_audit_created_at", table_name="identity_admin_audit")
    op.drop_index("ix_identity_admin_audit_action", table_name="identity_admin_audit")
    op.drop_index("ix_identity_admin_audit_erp_user_id", table_name="identity_admin_audit")
    op.drop_index("ix_identity_admin_audit_target_user_id", table_name="identity_admin_audit")
    op.drop_index("ix_identity_admin_audit_actor_user_id", table_name="identity_admin_audit")
    op.drop_table("identity_admin_audit")
