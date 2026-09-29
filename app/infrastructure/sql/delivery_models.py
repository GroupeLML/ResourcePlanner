from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id, utc_now


ID_LENGTH = 36


class DeliveryPlanRow(TimestampMixin, Base):
    __tablename__ = "delivery_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','ACTIVE','ARCHIVED')",
            name="delivery_plan_status",
        ),
        CheckConstraint(
            "delivery_version >= 1",
            name="delivery_plan_version_positive",
        ),
        Index(
            "ux_delivery_plans_work_package_non_archived",
            "work_package_id",
            unique=True,
            sqlite_where=text("status <> 'ARCHIVED'"),
            postgresql_where=text("status <> 'ARCHIVED'"),
            mssql_where=text("status <> 'ARCHIVED'"),
        ),
        Index("ix_delivery_plans_lead_user", "lead_user_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    work_package_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("work_packages.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'DRAFT'")
    )
    lead_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True
    )
    delivery_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DeliveryItemRow(TimestampMixin, Base):
    __tablename__ = "delivery_items"
    __table_args__ = (
        CheckConstraint("item_type IN ('EPIC','STORY')", name="delivery_item_type"),
        CheckConstraint(
            "status IN ('BACKLOG','TODO','IN_PROGRESS','BLOCKED','DONE','CANCELLED')",
            name="delivery_item_status",
        ),
        CheckConstraint(
            "current_estimate_hours IS NULL OR current_estimate_hours > 0",
            name="delivery_item_current_estimate_positive",
        ),
        CheckConstraint(
            "reference_estimate_hours IS NULL OR reference_estimate_hours > 0",
            name="delivery_item_reference_estimate_positive",
        ),
        CheckConstraint(
            "remaining_hours IS NULL OR remaining_hours >= 0",
            name="delivery_item_remaining_non_negative",
        ),
        CheckConstraint("position >= 0", name="delivery_item_position_non_negative"),
        Index(
            "ix_delivery_items_plan_status_position",
            "delivery_plan_id",
            "status",
            "position",
        ),
        Index("ix_delivery_items_plan_parent", "delivery_plan_id", "parent_id"),
        Index("ix_delivery_items_assignee", "assignee_user_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    delivery_plan_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("delivery_plans.id"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("delivery_items.id"), nullable=True
    )
    item_type: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'BACKLOG'")
    )
    assignee_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True
    )
    current_estimate_hours: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    reference_estimate_hours: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    remaining_hours: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sprint: Mapped[str | None] = mapped_column(String(128), nullable=True)


class DeliveryChangeHistory(Base):
    __tablename__ = "delivery_change_history"
    __table_args__ = (
        CheckConstraint(
            "delivery_version >= 1",
            name="delivery_history_version_positive",
        ),
        Index(
            "ix_delivery_change_history_plan_occurred",
            "delivery_plan_id",
            "occurred_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    delivery_plan_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("delivery_plans.id"), nullable=False
    )
    delivery_item_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("delivery_items.id"), nullable=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_version: Mapped[int] = mapped_column(Integer, nullable=False)
    details_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'{}'")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
