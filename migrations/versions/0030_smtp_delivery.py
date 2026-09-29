"""Add SMTP admin configuration and per-message delivery audit.

Revision ID: 0030_smtp_delivery
Revises: 0029_project_communications
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa
from uuid import uuid4


revision: str = "0030_smtp_delivery"
down_revision: str | None = "0029_project_communications"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "smtp_configuration",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("port", sa.Integer(), server_default="587", nullable=False),
        sa.Column(
            "security",
            sa.String(length=32),
            server_default="STARTTLS",
            nullable=False,
        ),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("encrypted_password", sa.Text(), nullable=True),
        sa.Column("from_email", sa.String(length=320), nullable=True),
        sa.Column("from_name", sa.String(length=255), nullable=True),
        sa.Column("reply_to", sa.String(length=320), nullable=True),
        sa.Column(
            "timeout_seconds",
            sa.Integer(),
            server_default="20",
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_smtp_configuration"),
    )

    op.create_table(
        "smtp_configuration_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_name", sa.String(length=255), nullable=True),
        sa.Column("changed_fields_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_smtp_configuration_audit"),
    )
    op.create_index(
        "ix_smtp_configuration_audit_created",
        "smtp_configuration_audit",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "communication_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=320), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_detail", sa.String(length=255), nullable=True),
        sa.Column("last_actor", sa.String(length=255), nullable=True),
        # The message already belongs to the batch with ON DELETE CASCADE.
        # Keeping this denormalized batch FK non-cascading avoids a second
        # batch -> delivery cascade path, which SQL Server rejects.
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["communication_batches.id"],
            name="fk_communication_deliveries_batch_id_communication_batches",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["communication_messages.id"],
            name="fk_communication_deliveries_message_id_communication_messages",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_communication_deliveries"),
    )
    op.create_index(
        "ux_communication_deliveries_message_provider",
        "communication_deliveries",
        ["message_id", "provider"],
        unique=True,
    )
    op.create_index(
        "ix_communication_deliveries_batch_status",
        "communication_deliveries",
        ["batch_id", "provider", "status"],
        unique=False,
    )

    if context.is_offline_mode():
        return

    connection = op.get_bind()
    existing = connection.execute(
        sa.text(
            "SELECT m.id AS message_id, m.batch_id AS batch_id "
            "FROM communication_messages AS m "
            "JOIN communication_batches AS b ON b.id = m.batch_id "
            "WHERE b.model_version = 'project_v2' AND m.included = :included"
        ),
        {"included": True},
    ).mappings()
    for row in existing:
        connection.execute(
            sa.text(
                "INSERT INTO communication_deliveries "
                "(id, batch_id, message_id, provider, status, attempt_count) "
                "VALUES (:id, :batch_id, :message_id, :provider, :status, :attempt_count)"
            ),
            {
                "id": str(uuid4()),
                "batch_id": row["batch_id"],
                "message_id": row["message_id"],
                "provider": "SMTP",
                "status": "PENDING",
                "attempt_count": 0,
            },
        )


def downgrade() -> None:
    op.drop_index(
        "ix_communication_deliveries_batch_status",
        table_name="communication_deliveries",
    )
    op.drop_index(
        "ux_communication_deliveries_message_provider",
        table_name="communication_deliveries",
    )
    op.drop_table("communication_deliveries")
    op.drop_index(
        "ix_smtp_configuration_audit_created",
        table_name="smtp_configuration_audit",
    )
    op.drop_table("smtp_configuration_audit")
    op.drop_table("smtp_configuration")
