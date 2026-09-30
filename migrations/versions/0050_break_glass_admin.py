"""Add production break-glass administrator support.

Revision ID: 0050_break_glass_admin
Revises: 0049_work_package_task_identity
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "0050_break_glass_admin"
down_revision: str | None = "0049_work_package_task_identity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("auth_sessions") as batch:
        batch.add_column(
            sa.Column(
                "auth_mode",
                sa.String(length=32),
                server_default="oidc",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_auth_sessions_auth_mode_valid",
            "auth_mode IN ('oidc', 'break_glass', 'local')",
        )
        batch.create_index(
            "ix_auth_sessions_auth_mode",
            ["auth_mode"],
            unique=False,
        )

    op.create_table(
        "break_glass_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("login_name", sa.String(length=128), nullable=False),
        sa.Column("secret_hash", sa.String(length=512), nullable=False),
        sa.Column(
            "credential_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "failed_attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
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
            "credential_version >= 1",
            name="ck_break_glass_credentials_credential_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_users.id"],
            name="fk_break_glass_credentials_user_id_app_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_break_glass_credentials"),
    )
    op.create_index(
        "ux_break_glass_credentials_user_id",
        "break_glass_credentials",
        ["user_id"],
        unique=True,
    )
    op.create_index(
        "ux_break_glass_credentials_login_name",
        "break_glass_credentials",
        ["login_name"],
        unique=True,
    )
    op.create_index(
        "ix_break_glass_credentials_active",
        "break_glass_credentials",
        ["active"],
        unique=False,
    )
    op.create_index(
        "ix_break_glass_credentials_locked_until",
        "break_glass_credentials",
        ["locked_until"],
        unique=False,
    )

    op.create_table(
        "auth_security_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("credential_id", sa.String(length=36), nullable=True),
        sa.Column("target_user_id", sa.String(length=36), nullable=True),
        sa.Column("login_name_hash", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["break_glass_credentials.id"],
            name="fk_auth_security_audit_credential_id_break_glass_credentials",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["app_users.id"],
            name="fk_auth_security_audit_target_user_id_app_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_security_audit"),
    )
    op.create_index(
        "ix_auth_security_audit_event_type",
        "auth_security_audit",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "ix_auth_security_audit_success",
        "auth_security_audit",
        ["success"],
        unique=False,
    )
    op.create_index(
        "ix_auth_security_audit_credential_id",
        "auth_security_audit",
        ["credential_id"],
        unique=False,
    )
    op.create_index(
        "ix_auth_security_audit_target_user_id",
        "auth_security_audit",
        ["target_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_auth_security_audit_login_name_hash",
        "auth_security_audit",
        ["login_name_hash"],
        unique=False,
    )
    op.create_index(
        "ix_auth_security_audit_created_at",
        "auth_security_audit",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_auth_security_audit_created_at", table_name="auth_security_audit")
    op.drop_index("ix_auth_security_audit_login_name_hash", table_name="auth_security_audit")
    op.drop_index("ix_auth_security_audit_target_user_id", table_name="auth_security_audit")
    op.drop_index("ix_auth_security_audit_credential_id", table_name="auth_security_audit")
    op.drop_index("ix_auth_security_audit_success", table_name="auth_security_audit")
    op.drop_index("ix_auth_security_audit_event_type", table_name="auth_security_audit")
    op.drop_table("auth_security_audit")

    op.drop_index("ix_break_glass_credentials_locked_until", table_name="break_glass_credentials")
    op.drop_index("ix_break_glass_credentials_active", table_name="break_glass_credentials")
    op.drop_index("ux_break_glass_credentials_login_name", table_name="break_glass_credentials")
    op.drop_index("ux_break_glass_credentials_user_id", table_name="break_glass_credentials")
    op.drop_table("break_glass_credentials")

    with op.batch_alter_table("auth_sessions") as batch:
        batch.drop_index("ix_auth_sessions_auth_mode")
        batch.drop_constraint("ck_auth_sessions_auth_mode_valid", type_="check")
        batch.drop_column("auth_mode")
