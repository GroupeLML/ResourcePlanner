from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id
from .models import ID_LENGTH


class RequestApprovalCycle(TimestampMixin, Base):
    __tablename__ = "request_approval_cycles"
    __table_args__ = (
        CheckConstraint(
            "submitted_request_version >= 1",
            name="approval_cycle_request_version_positive",
        ),
        CheckConstraint(
            "state IN ('OPEN','INVALIDATED','COMPLETED')",
            name="approval_cycle_state_supported",
        ),
        Index(
            "ix_request_approval_cycles_request_state",
            "workforce_request_id",
            "state",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        primary_key=True,
        default=new_id,
    )
    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("workforce_requests.id"),
        nullable=False,
        index=True,
    )
    submitted_request_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'OPEN'"),
        index=True,
    )
    subject_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    initialization_reason: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'SUBMISSION'"),
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    invalidation_reason: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    approved_revision_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("request_approval_revisions.id"),
        nullable=True,
        index=True,
    )


class ApprovalRequirement(TimestampMixin, Base):
    __tablename__ = "approval_requirements"
    __table_args__ = (
        UniqueConstraint(
            "approval_cycle_id",
            "request_line_id",
            name="uq_approval_requirement_cycle_line",
        ),
        Index(
            "ix_approval_requirements_cycle",
            "approval_cycle_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        primary_key=True,
        default=new_id,
    )
    approval_cycle_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("request_approval_cycles.id"),
        nullable=False,
    )
    request_line_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("request_lines.id"),
        nullable=False,
        index=True,
    )
    task_catalog_item_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("task_catalog_items.id"),
        nullable=True,
        index=True,
    )
    approval_scope_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("approval_scopes.id"),
        nullable=True,
        index=True,
    )
    proposed_resource_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("resources.id"),
        nullable=True,
        index=True,
    )
    asset_type_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("asset_types.id"),
        nullable=True,
        index=True,
    )
    proposed_asset_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("assets.id"),
        nullable=True,
        index=True,
    )
    routing_sources_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'[]'"),
    )


class ApprovalRequirementApprover(Base):
    __tablename__ = "approval_requirement_approvers"
    __table_args__ = (
        Index(
            "ix_approval_requirement_approvers_user",
            "app_user_id",
        ),
    )

    requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("approval_requirements.id"),
        primary_key=True,
    )
    app_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("app_users.id"),
        primary_key=True,
    )
    sources_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        Index(
            "ix_approval_decisions_requirement_actor",
            "requirement_id",
            "app_user_id",
        ),
        Index(
            "ix_approval_decisions_action",
            "action_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        primary_key=True,
        default=new_id,
    )
    requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("approval_requirements.id"),
        nullable=False,
    )
    app_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("app_users.id"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    comment: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    action_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        nullable=False,
    )
