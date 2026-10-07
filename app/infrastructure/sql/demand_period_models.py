from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id
from .models import ID_LENGTH


class WorkforceRequestPeriod(TimestampMixin, Base):
    """Versioned requested period that may later materialize into a requirement.

    ``id`` identifies the physical version row. ``period_key`` is the stable logical
    identifier exposed to the application so editing a demand can retain old versions
    without reusing a primary key.
    """

    __tablename__ = "workforce_request_periods"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="request_period_date_window"),
        CheckConstraint("hours IS NULL OR hours > 0", name="request_period_hours_positive"),
        CheckConstraint("resource_count >= 1", name="request_period_resource_count"),
        CheckConstraint(
            "inheritance_contract_version IS NULL OR inheritance_contract_version = 1",
            name="request_period_inheritance_contract_version",
        ),
        CheckConstraint(
            "confirmation_mode IS NULL OR confirmation_mode IN ('INHERIT_MASTER', 'EXPLICIT')",
            name="request_period_confirmation_mode",
        ),
        CheckConstraint(
            "proposed_resource_mode IS NULL OR proposed_resource_mode IN "
            "('INHERIT_MASTER', 'EXPLICIT', 'SAME_AS_PERIOD')",
            name="request_period_proposed_resource_mode",
        ),
        CheckConstraint(
            "(proposed_resource_mode = 'SAME_AS_PERIOD' AND same_as_period_key IS NOT NULL) OR "
            "(proposed_resource_mode IS NULL) OR "
            "(proposed_resource_mode != 'SAME_AS_PERIOD' AND same_as_period_key IS NULL)",
            name="request_period_same_as_consistency",
        ),
        CheckConstraint(
            "(kind = 'ALTERNATIVE' AND alternative_group IS NOT NULL) OR "
            "(kind = 'CUMULATIVE' AND alternative_group IS NULL)",
            name="request_period_kind_group_consistency",
        ),
        Index(
            "ix_request_periods_request_kind_group",
            "workforce_request_id",
            "kind",
            "alternative_group",
        ),
        Index(
            "ix_request_periods_request_active",
            "workforce_request_id",
            "active",
        ),
        Index(
            "ix_request_periods_request_key_active",
            "workforce_request_id",
            "period_key",
            "active",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    period_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("workforce_requests.id"),
        nullable=False,
        index=True,
    )
    request_line_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("request_lines.id"),
        nullable=True,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'CUMULATIVE'"), index=True
    )
    alternative_group: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    hours: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    inheritance_contract_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmation_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirmation: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'Tentative'"), index=True
    )
    proposed_resource_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proposed_resource_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("resources.id"), nullable=True, index=True
    )
    same_as_period_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resource_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    desired_active_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true(), index=True)


class WorkforceRequestPeriodSelection(Base):
    """Single selected option for one exclusive group of one workforce request."""

    __tablename__ = "workforce_request_period_selections"
    __table_args__ = (
        Index("ix_request_period_selection_period", "period_id"),
    )

    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("workforce_requests.id"),
        nullable=False,
        index=True,
    )
    request_line_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey(
            "request_lines.id",
            name="fk_period_selection_request_line",
        ),
        primary_key=True,
        nullable=False,
        index=True,
    )
    alternative_group: Mapped[str] = mapped_column(String(64), primary_key=True)
    period_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("workforce_request_periods.id"),
        nullable=False,
    )
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    selected_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class WorkforceRequestPeriodRequirement(Base):
    """Audit-safe link between one generated requirement and its period version."""

    __tablename__ = "workforce_request_period_requirements"
    __table_args__ = (
        Index("ix_request_period_requirement_period", "period_id"),
    )

    resource_requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("resource_requirements.id"),
        primary_key=True,
    )
    period_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("workforce_request_periods.id"),
        nullable=False,
    )
