from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id
from .models import ID_LENGTH


PLANNING_WINDOW_OVERRIDE_ACTIVE = "ACTIVE"
PLANNING_WINDOW_OVERRIDE_ABSORBED = "ABSORBED"
PLANNING_WINDOW_OVERRIDE_SUPERSEDED = "SUPERSEDED"


class PlanningWindowOverride(TimestampMixin, Base):
    """Persistent operational widening of one approved REQUEST segment window."""

    __tablename__ = "planning_window_overrides"
    __table_args__ = (
        CheckConstraint(
            "approved_end_date >= approved_start_date",
            name="planning_window_override_approved_window",
        ),
        CheckConstraint(
            "effective_end_date >= effective_start_date",
            name="planning_window_override_effective_window",
        ),
        CheckConstraint(
            "effective_start_date <= approved_start_date "
            "AND effective_end_date >= approved_end_date",
            name="planning_window_override_widens_approved_window",
        ),
        CheckConstraint(
            "effective_start_date < approved_start_date "
            "OR effective_end_date > approved_end_date",
            name="planning_window_override_strict_widening",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'ABSORBED', 'SUPERSEDED')",
            name="planning_window_override_status",
        ),
        Index(
            "ix_planning_window_overrides_revision_entry_status",
            "approval_revision_id",
            "approved_entry_key",
            "status",
        ),
        Index(
            "ix_planning_window_overrides_request_status",
            "workforce_request_id",
            "status",
        ),
        Index(
            "ix_planning_window_overrides_actor_correlation",
            "actor_user_id",
            "correlation_id",
        ),
        Index(
            "ux_planning_window_overrides_active_requirement",
            "resource_requirement_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
            mssql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("workforce_requests.id"),
        nullable=False,
    )
    resource_requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("resource_requirements.id"),
        nullable=False,
    )
    approval_revision_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("request_approval_revisions.id"),
        nullable=False,
    )
    approved_entry_key: Mapped[str] = mapped_column(String(512), nullable=False)
    approved_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    approved_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    actor_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("app_users.id"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    resolved_by_revision_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("request_approval_revisions.id"),
        nullable=True,
    )
    resolution_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
