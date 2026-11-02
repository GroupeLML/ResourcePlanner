from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id


ERP_SYNC_CONTROL_ROW_ID = "ERP_PROJECTS_TASKS"

ERP_OCCURRENCE_DAILY = "DAILY"
ERP_OCCURRENCE_MANUAL = "MANUAL"

ERP_OCCURRENCE_PENDING = "PENDING"
ERP_OCCURRENCE_MISSED = "MISSED"
ERP_OCCURRENCE_DISABLED = "DISABLED"
ERP_OCCURRENCE_SUCCEEDED = "SUCCEEDED"
ERP_OCCURRENCE_SUCCEEDED_WITH_ERRORS = "SUCCEEDED_WITH_ERRORS"
ERP_OCCURRENCE_FAILED = "FAILED"
ERP_OCCURRENCE_INTERRUPTED = "INTERRUPTED"

ERP_ATTEMPT_PENDING = "PENDING"
ERP_ATTEMPT_RUNNING = "RUNNING"
ERP_ATTEMPT_SUCCEEDED = "SUCCEEDED"
ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS = "SUCCEEDED_WITH_ERRORS"
ERP_ATTEMPT_FAILED = "FAILED"
ERP_ATTEMPT_INTERRUPTED = "INTERRUPTED"

ERP_ATTEMPT_TERMINAL = {
    ERP_ATTEMPT_SUCCEEDED,
    ERP_ATTEMPT_SUCCEEDED_WITH_ERRORS,
    ERP_ATTEMPT_FAILED,
    ERP_ATTEMPT_INTERRUPTED,
}


class ErpSyncControl(TimestampMixin, Base):
    __tablename__ = "erp_sync_control"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    owner_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    owner_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_evaluated_business_date: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_success_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ErpSyncOccurrence(TimestampMixin, Base):
    __tablename__ = "erp_sync_occurrences"
    __table_args__ = (
        CheckConstraint(
            "occurrence_type IN ('DAILY','MANUAL')",
            name="occurrence_type_values",
        ),
        CheckConstraint(
            "status IN ('PENDING','MISSED','DISABLED','SUCCEEDED','SUCCEEDED_WITH_ERRORS','FAILED','INTERRUPTED')",
            name="occurrence_status_values",
        ),
        UniqueConstraint("occurrence_key", name="uq_erp_sync_occurrence_key"),
        UniqueConstraint("command_identity", name="uq_erp_sync_command_identity"),
        Index("ix_erp_sync_occurrence_business_date", "business_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    occurrence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    occurrence_type: Mapped[str] = mapped_column(String(16), nullable=False)
    intent: Mapped[str] = mapped_column(String(64), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduled_local_time: Mapped[str] = mapped_column(String(5), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    command_identity: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ERP_OCCURRENCE_PENDING)
    last_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    success_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    diagnostic: Mapped[str | None] = mapped_column(Text, nullable=True)


class ErpSyncAttempt(TimestampMixin, Base):
    __tablename__ = "erp_sync_attempts"
    __table_args__ = (
        CheckConstraint(
            "trigger IN ('SCHEDULED','CATCHUP','MANUAL','RETRY')",
            name="attempt_trigger_values",
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','SUCCEEDED_WITH_ERRORS','FAILED','INTERRUPTED')",
            name="attempt_status_values",
        ),
        UniqueConstraint(
            "occurrence_id",
            "attempt_number",
            name="uq_erp_sync_attempt_number",
        ),
        UniqueConstraint(
            "command_identity",
            name="uq_erp_sync_attempt_command_identity",
        ),
        Index("ix_erp_sync_attempt_occurrence", "occurrence_id"),
        Index("ix_erp_sync_attempt_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    occurrence_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("erp_sync_occurrences.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    intent: Mapped[str] = mapped_column(String(64), nullable=False)
    command_identity: Mapped[str | None] = mapped_column(String(160), nullable=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ERP_ATTEMPT_PENDING)
    phase: Mapped[str] = mapped_column(String(64), nullable=False, default="PREFLIGHT")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    project_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projects_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projects_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projects_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    task_sync_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    diagnostic: Mapped[str | None] = mapped_column(Text, nullable=True)
