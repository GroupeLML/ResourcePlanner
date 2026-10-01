from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, new_id


ID_LENGTH = 36
GLOBAL_SYNC_RUN_KEY = "GLOBAL"
SYNC_RUN_PENDING = "PENDING"
SYNC_RUN_RUNNING = "RUNNING"
SYNC_RUN_COMPLETED = "COMPLETED"
SYNC_RUN_COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
SYNC_RUN_FAILED = "FAILED"
SYNC_RUN_INTERRUPTED = "INTERRUPTED"
SYNC_RUN_ACTIVE_STATUSES = (SYNC_RUN_PENDING, SYNC_RUN_RUNNING)
SYNC_RUN_TERMINAL_STATUSES = (
    SYNC_RUN_COMPLETED,
    SYNC_RUN_COMPLETED_WITH_ERRORS,
    SYNC_RUN_FAILED,
    SYNC_RUN_INTERRUPTED,
)


class AcumaticaProjectTaskSyncRun(TimestampMixin, Base):
    __tablename__ = "acumatica_project_task_sync_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','COMPLETED_WITH_ERRORS','FAILED','INTERRUPTED')",
            name="status_values",
        ),
        Index(
            "ux_acumatica_project_task_sync_runs_active",
            "active_key",
            unique=True,
            sqlite_where=text("active_key IS NOT NULL"),
            mssql_where=text("active_key IS NOT NULL"),
        ),
        Index(
            "ix_acumatica_project_task_sync_runs_created",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    active_key: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        default=GLOBAL_SYNC_RUN_KEY,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SYNC_RUN_PENDING,
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    projects_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projects_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projects_synchronized: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projects_ignored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projects_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    source_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_rows_scanned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_read_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_rows_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    tasks_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_deactivated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    diagnostic: Mapped[str | None] = mapped_column(Text, nullable=True)


class AcumaticaProjectTaskSyncProjectResult(TimestampMixin, Base):
    __tablename__ = "acumatica_project_task_sync_project_results"
    __table_args__ = (
        CheckConstraint(
            "status IN ('synchronized','ignored','rejected')",
            name="status_values",
        ),
        UniqueConstraint(
            "run_id",
            "project_id",
            name="uq_acumatica_project_task_sync_project_result",
        ),
        Index(
            "ix_acumatica_project_task_sync_project_results_run",
            "run_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("acumatica_project_task_sync_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    project_number: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)

    source_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tasks_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deactivated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
