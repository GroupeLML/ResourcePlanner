"""Physical assets are separate from people and their hourly shifts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, false, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...domain.reservable_assets import AssetRequirementOrigin
from .base import Base, TimestampMixin, new_id


class AssetType(TimestampMixin, Base):
    __tablename__ = "asset_types"
    __table_args__ = (UniqueConstraint("code", name="uq_asset_types_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    occupancy_policy: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'EXCLUSIVE_DAY'"))
    qualification_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ANY_ASSIGNED_WORKFORCE'")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class AssetTypeCompetency(Base):
    __tablename__ = "asset_type_competencies"

    asset_type_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("asset_types.id"), primary_key=True
    )
    competency_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("competencies.id"), primary_key=True
    )


class Asset(TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("code", name="uq_assets_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_type_id: Mapped[str] = mapped_column(String(36), ForeignKey("asset_types.id"), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class AssetApprover(Base):
    __tablename__ = "asset_approvers"

    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assets.id"), primary_key=True
    )
    app_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("app_users.id"), primary_key=True, index=True
    )


class AssetUnavailability(TimestampMixin, Base):
    __tablename__ = "asset_unavailability"
    __table_args__ = (CheckConstraint("end_date >= start_date", name="asset_unavailability_window"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id"), nullable=False, index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AssetRequirement(TimestampMixin, Base):
    __tablename__ = "asset_requirements"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="asset_requirement_window"),
        CheckConstraint("usage_hours IS NULL OR usage_hours > 0", name="asset_requirement_hours_positive"),
        CheckConstraint(
            "origin IN ('REQUEST', 'SHIFT_AD_HOC', 'PROJECT_DIRECT', 'SEGMENT', 'RESOURCE_PERIOD')",
            name="asset_requirement_origin_values",
        ),
        CheckConstraint(
            "("
            "origin = 'REQUEST' "
            "AND project_id IS NOT NULL "
            "AND shift_id IS NULL "
            "AND resource_requirement_id IS NULL "
            "AND context_resource_id IS NULL "
            "AND workforce_request_id IS NOT NULL "
            "AND source_request_line_id IS NOT NULL "
            "AND approved_entry_key IS NOT NULL"
            ") OR ("
            "origin = 'SHIFT_AD_HOC' "
            "AND project_id IS NOT NULL "
            "AND shift_id IS NOT NULL "
            "AND resource_requirement_id IS NULL "
            "AND context_resource_id IS NULL "
            "AND workforce_request_id IS NULL "
            "AND source_request_line_id IS NULL "
            "AND source_period_id IS NULL "
            "AND approval_revision_id IS NULL "
            "AND approved_entry_key IS NULL"
            ") OR ("
            "origin = 'PROJECT_DIRECT' "
            "AND project_id IS NOT NULL "
            "AND shift_id IS NULL "
            "AND resource_requirement_id IS NULL "
            "AND context_resource_id IS NULL "
            "AND workforce_request_id IS NULL "
            "AND source_request_line_id IS NULL "
            "AND source_period_id IS NULL "
            "AND approval_revision_id IS NULL "
            "AND approved_entry_key IS NULL"
            ") OR ("
            "origin = 'SEGMENT' "
            "AND project_id IS NOT NULL "
            "AND shift_id IS NULL "
            "AND resource_requirement_id IS NOT NULL "
            "AND context_resource_id IS NULL "
            "AND workforce_request_id IS NULL "
            "AND source_request_line_id IS NULL "
            "AND source_period_id IS NULL "
            "AND approval_revision_id IS NULL "
            "AND approved_entry_key IS NULL"
            ") OR ("
            "origin = 'RESOURCE_PERIOD' "
            "AND shift_id IS NULL "
            "AND resource_requirement_id IS NULL "
            "AND context_resource_id IS NOT NULL "
            "AND workforce_request_id IS NULL "
            "AND source_request_line_id IS NULL "
            "AND source_period_id IS NULL "
            "AND approval_revision_id IS NULL "
            "AND approved_entry_key IS NULL"
            ")",
            name="asset_requirement_origin_provenance",
        ),
        Index(
            "ux_asset_requirements_request_entry_slot",
            "workforce_request_id",
            "approved_entry_key",
            "slot_index",
            unique=True,
            sqlite_where=text("origin = 'REQUEST'"),
            mssql_where=text("origin = 'REQUEST'"),
        ),
        Index(
            "ux_asset_requirements_shift_ad_hoc",
            "shift_id",
            unique=True,
            sqlite_where=text("origin = 'SHIFT_AD_HOC'"),
            mssql_where=text("origin = 'SHIFT_AD_HOC'"),
        ),
        Index("ix_asset_requirements_request", "workforce_request_id", "source_request_line_id"),
        Index("ix_asset_requirements_resource_requirement_id", "resource_requirement_id"),
        Index("ix_asset_requirements_context_resource_id", "context_resource_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    origin: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text(f"'{AssetRequirementOrigin.REQUEST.value}'"),
    )
    shift_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("shifts.id"), nullable=True
    )
    resource_requirement_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("resource_requirements.id"), nullable=True
    )
    context_resource_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("resources.id"), nullable=True
    )
    workforce_request_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workforce_requests.id"), nullable=True
    )
    source_request_line_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("request_lines.id"), nullable=True
    )
    source_period_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("workforce_request_periods.id"), nullable=True)
    approval_revision_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("request_approval_revisions.id"), nullable=True)
    approved_entry_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    asset_type_id: Mapped[str] = mapped_column(String(36), ForeignKey("asset_types.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    usage_hours: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'À affecter'"))

    shift = relationship(
        "Shift",
        back_populates="asset_requirements",
        foreign_keys=[shift_id],
    )


class AssetAllocation(TimestampMixin, Base):
    __tablename__ = "asset_allocations"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="asset_allocation_window"),
        UniqueConstraint("asset_requirement_id", name="uq_asset_allocation_requirement"),
        Index("ix_asset_allocations_asset_window", "asset_id", "start_date", "end_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_requirement_id: Mapped[str] = mapped_column(String(36), ForeignKey("asset_requirements.id"), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("assets.id"), nullable=False)
    operator_resource_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("resources.id"), nullable=True, index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'MANUAL'"))
