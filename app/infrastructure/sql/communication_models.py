from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id, utc_now


ID_LENGTH = 36


class CommunicationContact(TimestampMixin, Base):
    __tablename__ = "communication_contacts"
    __table_args__ = (
        Index("ux_communication_contacts_recipient", "recipient_id", unique=True),
        Index("ix_communication_contacts_audience_active", "audience", "active"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    recipient_id: Mapped[str] = mapped_column(String(180), nullable=False)
    audience: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())


class CommunicationBatchRow(Base):
    __tablename__ = "communication_batches"
    __table_args__ = (
        Index("ix_communication_batches_week_status", "week_start", "status"),
        Index(
            "ix_communication_batches_fingerprint",
            "week_start",
            "kind",
            "snapshot_fingerprint",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    prepared_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    communicated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    communicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    drafts_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    drafts_created_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    drafts_created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    drafts_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="legacy"
    )
    project_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class CommunicationMessageRow(Base):
    __tablename__ = "communication_messages"
    __table_args__ = (
        Index("ix_communication_messages_batch", "batch_id", "audience"),
        Index("ix_communication_messages_batch_key", "batch_id", "message_key"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("communication_batches.id", ondelete="CASCADE"), nullable=False
    )
    audience: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(180), nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    to_recipients_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), nullable=True)
    cc_recipients_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approvable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true()
    )
    diagnostics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())


class CommunicationSnapshotLine(Base):
    __tablename__ = "communication_snapshot_lines"
    __table_args__ = (Index("ix_communication_snapshot_batch", "batch_id", "day"),)

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("communication_batches.id", ondelete="CASCADE"), nullable=False
    )
    segment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(180), nullable=False)
    resource_name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_manager_id: Mapped[str] = mapped_column(String(180), nullable=False, server_default="")
    project_number: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    project_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    day: Mapped[date] = mapped_column(Date, nullable=False)
    hours: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    allocation_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default="Flexible")
    outside_schedule: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confirmation: Mapped[str] = mapped_column(String(32), nullable=False, server_default="Confirmée")


class SmtpConfigurationRow(TimestampMixin, Base):
    __tablename__ = "smtp_configuration"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="smtp")
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int] = mapped_column(Integer, nullable=False, server_default="587")
    security: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="STARTTLS"
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    from_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reply_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="20"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SmtpConfigurationAuditRow(Base):
    __tablename__ = "smtp_configuration_audit"
    __table_args__ = (
        Index(
            "ix_smtp_configuration_audit_created",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class CommunicationDeliveryRow(Base):
    __tablename__ = "communication_deliveries"
    __table_args__ = (
        Index(
            "ux_communication_deliveries_message_provider",
            "message_id",
            "provider",
            unique=True,
        ),
        Index(
            "ix_communication_deliveries_batch_status",
            "batch_id",
            "provider",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        # message_id owns the delete cascade. Keeping this denormalized
        # query key non-cascading avoids SQL Server multiple cascade paths.
        ForeignKey("communication_batches.id"),
        nullable=False,
    )
    message_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("communication_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="PENDING"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(320), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
