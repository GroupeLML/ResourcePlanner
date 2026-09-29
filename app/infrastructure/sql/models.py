from __future__ import annotations

from datetime import date, datetime, time
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
    Time,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id


ID_LENGTH = 36
ORIGIN_REQUEST = "REQUEST"
ORIGIN_QUICK_SHIFT = "QUICK_SHIFT"
ORIGIN_AD_HOC = "AD_HOC"


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    erp_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    number: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, server_default=text("''"))
    client: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project_manager_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    project_manager_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project_manager_contact_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("business_contacts.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'active'"), index=True)


class TaskCatalogEntry(TimestampMixin, Base):
    __tablename__ = "task_catalog_items"
    __table_args__ = (
        UniqueConstraint(
            "project_number",
            "task_code",
            name="uq_task_catalog_items_project_code",
        ),
        Index("ix_task_catalog_items_project_active", "project_number", "active"),
        Index(
            "ix_task_catalog_items_project_workforce",
            "project_number",
            "active",
            "workforce_eligible",
        ),
        Index(
            "ux_task_catalog_items_erp_task_id_not_null",
            "erp_task_id",
            unique=True,
            sqlite_where=text("erp_task_id IS NOT NULL"),
            mssql_where=text("erp_task_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    project_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'Actif'"), index=True
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true(), index=True
    )
    billing_rule: Mapped[str | None] = mapped_column(String(128), nullable=True)
    allocation_rule: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completion_percent: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    erp_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approver_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operational_responsible_contact_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("business_contacts.id"), nullable=True, index=True
    )
    coordinator_contact_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("business_contacts.id"), nullable=True, index=True
    )
    cv_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    time_entry_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expenses_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    erp_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    account_group: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cost_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    inventory_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    budget_amount_cad: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 10), nullable=True
    )
    budget_actual_cad: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 10), nullable=True
    )
    budget_diagnostic: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workforce_eligible: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    resource_class_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    average_hourly_cost_cad: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    budget_hours: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    workforce_diagnostics: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaskCatalogProjectSyncState(TimestampMixin, Base):
    __tablename__ = "task_catalog_project_sync_state"

    project_number: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_success_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    task_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rejected_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Competency(TimestampMixin, Base):
    __tablename__ = "competencies"
    __table_args__ = (
        UniqueConstraint("name", name="uq_competencies_name"),
        Index("ix_competencies_active_order", "active", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true(), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class Resource(TimestampMixin, Base):
    __tablename__ = "resources"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    coordinator_contact_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("business_contacts.id"), nullable=True, index=True
    )
    resource_class: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    competencies: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Local RessourcePlanner activation, controlled by administrators.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true(), index=True)
    # ERP employment state is deliberately separate from local authorization.
    erp_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    erp_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true(), index=True
    )
    erp_department_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    erp_department_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    erp_employee_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    erp_supervisor_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    erp_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    erp_branch_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    erp_contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class ResourceCompetency(Base):
    __tablename__ = "resource_competencies"

    resource_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("resources.id"),
        primary_key=True,
    )
    competency_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("competencies.id"),
        primary_key=True,
    )


class WorkPackage(TimestampMixin, Base):
    __tablename__ = "work_packages"
    __table_args__ = (
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="work_package_date_window",
        ),
        CheckConstraint(
            "planned_hours IS NULL OR planned_hours >= 0",
            name="work_package_hours_non_negative",
        ),
        CheckConstraint("version >= 1", name="work_package_version_positive"),
        Index("ix_work_packages_project_status", "project_id", "status"),
        Index("ix_work_packages_task_catalog_item", "task_catalog_item_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("projects.id"), nullable=False, index=True
    )
    task_catalog_item_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("task_catalog_items.id"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    planned_hours: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'planned'"), index=True)
    legacy_effort_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)


class WorkPackageAudit(TimestampMixin, Base):
    __tablename__ = "work_package_audit"
    __table_args__ = (
        CheckConstraint("resulting_version >= 1", name="work_package_audit_version_positive"),
        Index("ix_work_package_audit_package_created", "work_package_id", "created_at"),
        Index("ix_work_package_audit_actor", "actor_user_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    work_package_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("work_packages.id"), nullable=False
    )
    actor_user_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resulting_version: Mapped[int] = mapped_column(Integer, nullable=False)
    old_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    new_values_json: Mapped[str] = mapped_column(Text, nullable=False)


class WorkforceRequest(TimestampMixin, Base):
    __tablename__ = "workforce_requests"
    __table_args__ = (
        CheckConstraint("resource_count >= 1", name="workforce_request_resource_count"),
        CheckConstraint(
            "desired_end IS NULL OR desired_start IS NULL OR desired_end >= desired_start",
            name="workforce_request_date_window",
        ),
        CheckConstraint(
            "estimated_hours IS NULL OR estimated_hours >= 0",
            name="workforce_request_hours_non_negative",
        ),
        CheckConstraint(
            "estimated_days IS NULL OR estimated_days >= 0",
            name="workforce_request_days_non_negative",
        ),
        CheckConstraint(
            "cancellation_state IS NULL OR cancellation_state IN ('PENDING','REJECTED','ACCEPTED')",
            name="workforce_request_cancellation_state",
        ),
        Index("ix_workforce_requests_project_status", "project_id", "status"),
        Index("ix_workforce_requests_window", "desired_start", "desired_end"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    legacy_demand_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    project_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("projects.id"), nullable=False, index=True
    )
    work_package_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("work_packages.id"), nullable=True, index=True
    )
    erp_task_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    erp_task_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requester_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True, index=True
    )
    requester_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requester_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operational_responsible_override_contact_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("business_contacts.id"), nullable=True, index=True
    )
    request_type: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'Projet'"))
    priority: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'Normale'"), index=True)
    confirmation: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'Confirmée'"), index=True)
    desired_start: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    desired_end: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    site_client: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    required_competencies: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_hours: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    estimated_days: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    proposed_resource_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("resources.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'Brouillon'"), index=True)
    cancellation_request_id: Mapped[str | None] = mapped_column(String(ID_LENGTH), nullable=True)
    cancellation_state: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    cancellation_requested_by_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True, index=True
    )
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_resolved_by_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True, index=True
    )
    cancellation_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_resolution_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    emergency_override_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), index=True
    )
    emergency_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    emergency_override_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    emergency_override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    aggregate_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    line_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), index=True
    )


class RequestLine(TimestampMixin, Base):
    """Planifiable child need under one workforce-request workflow header.

    New multi-line requests will normally use one slot per line. slot_count exists
    to preserve grouped legacy requests during the additive transition without
    multiplying historical effort or breaking shared alternatives.
    """

    __tablename__ = "request_lines"
    __table_args__ = (
        CheckConstraint("position >= 0", name="request_line_position_non_negative"),
        CheckConstraint("slot_count >= 1", name="request_line_slot_count_positive"),
        CheckConstraint(
            "kind IN ('WORKFORCE', 'ASSET', 'WORKCENTER')",
            name="request_line_kind_supported",
        ),
        CheckConstraint(
            "desired_end IS NULL OR desired_start IS NULL OR desired_end >= desired_start",
            name="request_line_date_window",
        ),
        CheckConstraint(
            "estimated_hours IS NULL OR estimated_hours >= 0",
            name="request_line_hours_non_negative",
        ),
        CheckConstraint(
            "desired_active_days IS NULL OR desired_active_days >= 0",
            name="request_line_days_non_negative",
        ),
        Index(
            "ix_request_lines_request_position",
            "workforce_request_id",
            "position",
        ),
        Index(
            "ix_request_lines_request_active",
            "workforce_request_id",
            "active",
        ),
        Index("ix_request_lines_window", "desired_start", "desired_end"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("workforce_requests.id"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'WORKFORCE'"), index=True
    )
    slot_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    required_resource_class: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    required_competencies_snapshot: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    desired_start: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    desired_end: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    desired_active_days: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    estimated_hours: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    estimated_hours_source: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    default_hours_per_day: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    confirmation: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'Confirmée'"),
        index=True,
    )
    work_package_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("work_packages.id"),
        nullable=True,
        index=True,
    )
    task_catalog_item_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("task_catalog_items.id"),
        nullable=True,
        index=True,
    )
    erp_task_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    erp_task_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proposed_resource_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("resources.id"),
        nullable=True,
        index=True,
    )
    asset_type_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("asset_types.id"), nullable=True, index=True
    )
    proposed_asset_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("assets.id"), nullable=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true(), index=True
    )


class RequestLineCompetency(Base):
    __tablename__ = "request_line_competencies"

    request_line_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("request_lines.id"),
        primary_key=True,
    )
    competency_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("competencies.id"),
        primary_key=True,
    )


class WorkforceRequestCompetency(Base):
    __tablename__ = "workforce_request_competencies"

    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("workforce_requests.id"),
        primary_key=True,
    )
    competency_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("competencies.id"),
        primary_key=True,
    )


class WorkforceRequestHistory(Base):
    __tablename__ = "workforce_request_history"
    __table_args__ = (
        Index("ix_workforce_request_history_request_time", "workforce_request_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    workforce_request_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("workforce_requests.id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("app_users.id"), nullable=True, index=True
    )
    actor_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResourceAvailabilityRule(TimestampMixin, Base):
    __tablename__ = "resource_availability_rules"
    __table_args__ = (
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="availability_date_window",
        ),
        CheckConstraint(
            "resource_id IS NOT NULL OR availability_type = 'Jour férié'",
            name="availability_resource_or_global_holiday",
        ),
        Index("ix_availability_resource_window", "resource_id", "start_date", "end_date"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    legacy_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    resource_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("resources.id"), nullable=True, index=True
    )
    availability_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    weekdays: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true(), index=True)


class ResourceRequirement(TimestampMixin, Base):
    __tablename__ = "resource_requirements"
    __table_args__ = (
        CheckConstraint("planned_hours > 0", name="resource_requirement_hours_positive"),
        CheckConstraint("end_date >= start_date", name="resource_requirement_date_window"),
        CheckConstraint(
            "workforce_request_id IS NOT NULL OR origin IN ('QUICK_SHIFT', 'AD_HOC')",
            name="resource_requirement_request_or_adhoc",
        ),
        CheckConstraint(
            "approved_contact_context_status IN ('CAPTURED', 'LEGACY_UNKNOWN', 'NOT_APPLICABLE')",
            name="resource_requirement_approved_contact_context_status",
        ),
        CheckConstraint(
            "approval_reference_status IN ('CAPTURED', 'LEGACY_UNKNOWN', 'NOT_APPLICABLE')",
            name="resource_requirement_approval_reference_status",
        ),
        Index("ix_resource_requirements_project_window", "project_id", "start_date", "end_date"),
        Index("ix_resource_requirements_resource_window", "assigned_resource_id", "start_date", "end_date"),
        Index("ix_resource_requirements_request_status", "workforce_request_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    legacy_segment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    project_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("projects.id"), nullable=False, index=True
    )
    workforce_request_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("workforce_requests.id"), nullable=True, index=True
    )
    source_request_line_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("request_lines.id"), nullable=True, index=True
    )
    approved_task_catalog_item_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("task_catalog_items.id"), nullable=True, index=True
    )
    approved_operational_responsible_override_contact_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("business_contacts.id"), nullable=True, index=True
    )
    approved_request_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_contact_context_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'LEGACY_UNKNOWN'"), index=True
    )
    approval_revision_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("request_approval_revisions.id", name="fk_resource_requirements_approval_revision"),
        nullable=True,
        index=True,
    )
    approved_entry_key: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        index=True,
    )
    approval_reference_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'LEGACY_UNKNOWN'"),
        index=True,
    )
    assigned_resource_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("resources.id"), nullable=True, index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_hours: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    desired_active_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    load_profile: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'UNIFORM'")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'À assigner'"), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_effort_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    required_resource_class: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    required_competency: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    required_competency_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("competencies.id"), nullable=True, index=True
    )
    planning_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'Flexible'"), index=True)
    priority: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'Normale'"), index=True)
    outside_standard_hours_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    confirmation: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'Confirmée'"), index=True
    )
    confirmation_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'REQUEST'"), index=True)
    created_by_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ResourceRequirementCompetency(Base):
    """Canonical competency snapshot for one materialized requirement."""

    __tablename__ = "resource_requirement_competencies"

    resource_requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey(
            "resource_requirements.id",
            name="fk_req_req_comp_requirement",
        ),
        primary_key=True,
    )
    competency_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey(
            "competencies.id",
            name="fk_req_req_comp_competency",
        ),
        primary_key=True,
    )


class Shift(TimestampMixin, Base):
    __tablename__ = "shifts"
    __table_args__ = (
        CheckConstraint("hours > 0", name="shift_hours_positive"),
        Index("ix_shifts_resource_date", "resource_id", "work_date"),
        Index("ix_shifts_requirement_date", "resource_requirement_id", "work_date"),
        Index("ix_shifts_locked_date", "locked", "work_date"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    legacy_allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    resource_requirement_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("resource_requirements.id"), nullable=False, index=True
    )
    resource_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("resources.id"), nullable=False, index=True
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    hours: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    allocation_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'AUTO'"), index=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false(), index=True)
    outside_standard_hours: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    confirmation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
