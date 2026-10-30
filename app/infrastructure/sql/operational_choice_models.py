from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String,
    Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id
from .models import ID_LENGTH


class RequestOperationalState(TimestampMixin, Base):
    """Versioned operational choices applied to one active approved revision."""

    __tablename__ = "request_operational_states"
    __table_args__ = (
        CheckConstraint(
            "version >= 1",
            name="request_operational_state_version_positive",
        ),
    )

    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey(
            "workforce_requests.id",
            name="fk_request_operational_states_request",
        ),
        primary_key=True,
    )
    approval_revision_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey(
            "request_approval_revisions.id",
            name="fk_request_operational_states_revision",
        ),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    selections_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'{}'"),
    )
    confirmations_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'{}'"),
    )
    budget_overrides_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'{}'"),
    )
    updated_by_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )


PLANNING_INTENT_AUTO = "AUTO"
PLANNING_INTENT_PRESERVE_EXISTING = "PRESERVE_EXISTING"
PLANNING_INTENT_QUEUED = "QUEUED"
PLANNING_TARGET_INHERIT = "INHERIT"
PLANNING_TARGET_EXPLICIT_RESOURCE = "EXPLICIT_RESOURCE"
PLANNING_TARGET_EXPLICIT_NONE = "EXPLICIT_NONE"


class RequestRequirementPlanningIntent(TimestampMixin, Base):
    """Durable ADR-034 identity and operational intent for one approved quantity slot."""

    __tablename__ = "request_requirement_planning_intents"
    __table_args__ = (
        UniqueConstraint("slot_identity_key", name="uq_request_requirement_planning_intent_slot"),
        UniqueConstraint(
            "workforce_request_id",
            "approved_entry_key",
            "slot_index",
            name="uq_request_requirement_planning_intent_logical_slot",
        ),
        CheckConstraint(
            "intent_mode IN ('AUTO','PRESERVE_EXISTING','QUEUED')",
            name="request_requirement_planning_intent_mode",
        ),
        CheckConstraint(
            "target_mode IN ('INHERIT','EXPLICIT_RESOURCE','EXPLICIT_NONE')",
            name="request_requirement_planning_target_mode",
        ),
        CheckConstraint(
            "(target_mode = 'EXPLICIT_RESOURCE' AND target_resource_id IS NOT NULL) OR "
            "(target_mode != 'EXPLICIT_RESOURCE' AND target_resource_id IS NULL)",
            name="request_requirement_planning_target_resource",
        ),
        CheckConstraint("slot_index >= 0", name="request_requirement_planning_slot_index"),
        Index("ix_request_requirement_planning_intent_request", "workforce_request_id"),
        Index("ix_request_requirement_planning_intent_requirement", "current_resource_requirement_id"),
        Index(
            "uq_request_requirement_planning_intent_current_requirement",
            "current_resource_requirement_id",
            unique=True,
            sqlite_where=text("current_resource_requirement_id IS NOT NULL"),
            mssql_where=text("current_resource_requirement_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    slot_identity_key: Mapped[str] = mapped_column(String(768), nullable=False)
    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("workforce_requests.id"), nullable=False
    )
    source_request_line_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_lines.id"), nullable=True
    )
    approval_revision_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_approval_revisions.id"), nullable=True
    )
    approved_entry_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_period_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("workforce_request_periods.id"), nullable=True
    )
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    current_resource_requirement_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("resource_requirements.id"), nullable=True
    )
    intent_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'AUTO'")
    )
    target_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'INHERIT'")
    )
    target_resource_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("resources.id"), nullable=True
    )
    placement_not_before_date: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )


class PlanningWorkQueueRelease(TimestampMixin, Base):
    """Append-only evidence that one real Shift was explicitly returned to the queue."""

    __tablename__ = "planning_work_queue_releases"
    __table_args__ = (
        CheckConstraint("hours > 0", name="planning_work_queue_release_hours_positive"),
        UniqueConstraint("actor_user_id", "command_id", name="uq_planning_work_queue_release_actor_command"),
        Index("ix_planning_work_queue_release_slot_active", "planning_intent_id", "active"),
        Index("ix_planning_work_queue_release_exclusion", "resource_requirement_id", "resource_id", "work_date"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    planning_intent_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_requirement_planning_intents.id"), nullable=False
    )
    resource_requirement_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), nullable=True)
    original_shift_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("workforce_requests.id"), nullable=False
    )
    approval_revision_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_approval_revisions.id"), nullable=True
    )
    approved_entry_key: Mapped[str] = mapped_column(String(512), nullable=False)
    resource_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("resources.id"), nullable=False
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    hours: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    shift_snapshot_text: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'RETURN_SHIFT_TO_WORK_QUEUE'")
    )
    actor_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=False
    )
    command_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True
    )


class PlanningWorkQueueRestoration(TimestampMixin, Base):
    """Durable attribution of restored coverage to one work-queue release."""

    __tablename__ = "planning_work_queue_restorations"
    __table_args__ = (
        CheckConstraint(
            "restored_hours > 0",
            name="planning_work_queue_restoration_hours_positive",
        ),
        UniqueConstraint(
            "shift_id",
            name="uq_planning_work_queue_restoration_shift",
        ),
        Index(
            "ix_planning_work_queue_restoration_release",
            "release_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    release_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("planning_work_queue_releases.id"),
        nullable=False,
    )
    shift_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    restored_hours: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)



class RequestCompletionReduction(TimestampMixin, Base):
    """Append-only post-approval reduction of the current effective need for one slot."""

    __tablename__ = "request_completion_reductions"
    __table_args__ = (
        CheckConstraint("reduction_hours > 0", name="request_completion_reduction_hours_positive"),
        UniqueConstraint("actor_user_id", "command_id", name="uq_request_completion_reduction_actor_command"),
        CheckConstraint(
            "effective_need_before >= effective_need_after AND effective_need_after >= 0",
            name="request_completion_reduction_need_order",
        ),
        CheckConstraint(
            "reconciliation_state IN ('ACTIVE','ABSORBED','SUPERSEDED')",
            name="request_completion_reduction_state",
        ),
        Index("ix_request_completion_reduction_slot_state", "planning_intent_id", "reconciliation_state"),
        Index("ix_request_completion_reduction_request", "workforce_request_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    planning_intent_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_requirement_planning_intents.id"), nullable=False
    )
    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("workforce_requests.id"), nullable=False
    )
    historical_approval_revision_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_approval_revisions.id"), nullable=False
    )
    source_request_line_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_lines.id"), nullable=True
    )
    approved_entry_key: Mapped[str] = mapped_column(String(512), nullable=False)
    resource_requirement_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), nullable=True)
    original_shift_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    shift_snapshot_text: Mapped[str] = mapped_column(Text, nullable=False)
    reduction_hours: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    effective_need_before: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    effective_need_after: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'LESS_WORK_REQUIRED'")
    )
    actor_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=False
    )
    command_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    reconciliation_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ACTIVE'")
    )
    absorbed_by_revision_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_approval_revisions.id"), nullable=True
    )
    authorization_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    reconciled_approval_revision_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_approval_revisions.id"), nullable=True
    )
