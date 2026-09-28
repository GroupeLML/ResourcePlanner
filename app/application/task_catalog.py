from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from time import perf_counter
from typing import Protocol

from .errors import ApplicationValidationError


@dataclass(frozen=True, slots=True)
class TaskCatalogItem:
    """Transport-neutral ERP task reference.

    The historical #271 file source identifies tasks with (project_number, code).
    OData sources may additionally provide erp_task_id (RP_ProjectTasks.TaskID),
    which is the authoritative ERP identity while the local catalog row id remains
    stable for existing request-line/history references.
    """

    project_number: str
    code: str
    label: str
    status: str = "Actif"
    active: bool = True
    billing_rule: str | None = None
    allocation_rule: str | None = None
    completion_percent: float | None = None
    erp_created_at: datetime | None = None
    branch: str | None = None
    approver_name: str | None = None
    cv_enabled: bool | None = None
    time_entry_enabled: bool | None = None
    expenses_enabled: bool | None = None
    erp_task_id: str | None = None
    account_group: str | None = None
    cost_code: str | None = None
    inventory_id: str | None = None
    budget_amount_cad: Decimal | None = None
    budget_actual_cad: Decimal | None = None
    budget_diagnostic: str | None = None
    workforce_eligible: bool | None = None
    resource_class_code: str | None = None
    average_hourly_cost_cad: Decimal | None = None
    budget_hours: Decimal | None = None
    workforce_diagnostics: tuple[str, ...] = ()
    id: str | None = None
    operational_responsible_contact_id: str | None = None
    coordinator_contact_id: str | None = None

    @property
    def external_key(self) -> tuple[str, str]:
        """Historical #271 identity retained for file-import compatibility."""

        return (self.project_number, self.code)

    @property
    def synchronization_key(self) -> tuple[str, ...]:
        """Prefer stable ERP TaskID without breaking legacy file-backed rows."""

        erp_task_id = str(self.erp_task_id or "").strip()
        if erp_task_id:
            return ("erp_task_id", erp_task_id)
        return (
            "legacy_project_task",
            str(self.project_number or "").strip(),
            str(self.code or "").strip(),
        )


class TaskCatalogSourcePort(Protocol):
    """Read-only source of the historical/global task-catalog snapshot."""

    def list_tasks(self) -> Sequence[TaskCatalogItem]: ...


@dataclass(frozen=True, slots=True)
class TaskCatalogProjectSnapshot:
    """Complete transport snapshot for one explicitly targeted ERP project."""

    project_number: str
    source_rows: int
    rejected_rows: int
    items: tuple[TaskCatalogItem, ...]


class ProjectTaskCatalogSourcePort(Protocol):
    """Source contract for targeted RP_ProjectTasks synchronization."""

    def fetch_project_snapshot(
        self,
        *,
        project_external_id: str,
        project_number: str,
    ) -> TaskCatalogProjectSnapshot: ...


@dataclass(frozen=True, slots=True)
class TaskCatalogWorkforceProjection:
    eligible: bool
    resource_class_code: str | None
    average_hourly_cost_cad: Decimal | None
    budget_hours: Decimal | None
    diagnostics: tuple[str, ...]


class TaskCatalogWorkforcePolicyPort(Protocol):
    """Resolve #454 workforce class/cost rules for one ERP task."""

    def project_task_projection(
        self,
        *,
        project_number: str,
        task_code: str,
        budget_amount_cad: Decimal | None,
    ) -> TaskCatalogWorkforceProjection: ...


class TaskCatalogRepositoryPort(Protocol):
    """Persistence/search contract independent from SQLite, SQL Server and Acumatica."""

    def upsert(self, item: TaskCatalogItem) -> str: ...

    def search(
        self,
        *,
        project_number: str | None = None,
        query: str | None = None,
        active_only: bool = True,
        limit: int = 200,
    ) -> Sequence[TaskCatalogItem]: ...


@dataclass(frozen=True, slots=True)
class TaskCatalogProjectSyncMetadata:
    project_number: str
    last_success_at: datetime
    source_rows: int
    task_count: int
    rejected_rows: int
    duration_ms: int | None = None
    last_error_code: str | None = None


class TaskCatalogProjectSyncMetadataRepositoryPort(Protocol):
    def record_project_sync_success(
        self,
        *,
        project_number: str,
        source_rows: int,
        task_count: int,
        rejected_rows: int,
        duration_ms: int | None,
    ) -> None: ...

    def get_project_sync_metadata(
        self,
        project_number: str,
    ) -> TaskCatalogProjectSyncMetadata | None: ...


@dataclass(frozen=True, slots=True)
class TaskCatalogSyncResult:
    received: int
    created: int
    updated: int
    unchanged: int
    deactivated: int
    ignored: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deactivated": self.deactivated,
        }


@dataclass(frozen=True, slots=True)
class TaskCatalogProjectSyncResult:
    project_number: str
    source_rows: int
    task_count: int
    rejected_rows: int
    created: int
    updated: int
    unchanged: int
    deactivated: int
    ignored: int = 0
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "project_number": self.project_number,
            "source_rows": self.source_rows,
            "task_count": self.task_count,
            "rejected_rows": self.rejected_rows,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deactivated": self.deactivated,
            "ignored": self.ignored,
            "duration_ms": self.duration_ms,
        }


class TaskCatalogSyncService:
    """Idempotently synchronize task snapshots into the durable local catalog.

    synchronize preserves the historical #271 global/file path.
    synchronize_project is the targeted OData path introduced by #452. Neither
    path infers deactivation from an absent row; only an explicit source status can
    deactivate an existing catalog item.
    """

    def __init__(
        self,
        source: TaskCatalogSourcePort | ProjectTaskCatalogSourcePort,
        repository: TaskCatalogRepositoryPort,
        *,
        sync_metadata_repository: TaskCatalogProjectSyncMetadataRepositoryPort | None = None,
        workforce_policy: TaskCatalogWorkforcePolicyPort | None = None,
    ) -> None:
        self._source = source
        self._repository = repository
        self._sync_metadata_repository = sync_metadata_repository
        self._workforce_policy = workforce_policy

    def _apply_workforce_policy(self, item: TaskCatalogItem) -> TaskCatalogItem:
        if self._workforce_policy is None or not str(item.erp_task_id or "").strip():
            return item
        projection = self._workforce_policy.project_task_projection(
            project_number=str(item.project_number or "").strip(),
            task_code=str(item.code or "").strip(),
            budget_amount_cad=item.budget_amount_cad,
        )
        diagnostics = tuple(
            dict.fromkeys(
                [
                    *item.workforce_diagnostics,
                    *projection.diagnostics,
                ]
            )
        )
        return replace(
            item,
            workforce_eligible=projection.eligible,
            resource_class_code=projection.resource_class_code,
            average_hourly_cost_cad=projection.average_hourly_cost_cad,
            budget_hours=projection.budget_hours,
            workforce_diagnostics=diagnostics,
        )

    def _synchronize_rows(
        self,
        rows: Sequence[TaskCatalogItem],
        *,
        expected_project_number: str | None = None,
        apply_workforce_policy: bool = False,
    ) -> TaskCatalogSyncResult:
        expected_project = str(expected_project_number or "").strip()
        seen: set[tuple[str, ...]] = set()
        counts = {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "deactivated": 0,
            "ignored": 0,
        }

        for item in rows:
            if apply_workforce_policy:
                item = self._apply_workforce_policy(item)
            project_number = str(item.project_number or "").strip()
            code = str(item.code or "").strip()
            label = str(item.label or "").strip()
            if not project_number or not code or not label:
                raise ApplicationValidationError(
                    "Une tâche ERP doit avoir un projet, un code et un libellé.",
                    code="task_catalog_item_invalid",
                    context={
                        "project_number": project_number or None,
                        "task_code": code or None,
                    },
                )
            if expected_project and project_number != expected_project:
                raise ApplicationValidationError(
                    "La synchronisation ciblée contient une tâche d'un autre projet.",
                    code="task_catalog_project_mismatch",
                    context={
                        "expected_project_number": expected_project,
                        "project_number": project_number,
                    },
                )

            key = item.synchronization_key
            if key in seen:
                raise ApplicationValidationError(
                    "La même identité de tâche ERP apparaît plus d'une fois dans le snapshot.",
                    code="task_catalog_duplicate_key",
                    context={
                        "project_number": project_number,
                        "task_code": code,
                        "identity_kind": key[0],
                    },
                )
            seen.add(key)
            action = self._repository.upsert(item)
            if action not in counts:
                raise RuntimeError(f"Unsupported task catalog synchronization action: {action}")
            counts[action] += 1

        return TaskCatalogSyncResult(
            received=len(rows),
            created=counts["created"],
            updated=counts["updated"],
            unchanged=counts["unchanged"],
            deactivated=counts["deactivated"],
            ignored=counts["ignored"],
        )

    def synchronize(self) -> TaskCatalogSyncResult:
        list_tasks = getattr(self._source, "list_tasks", None)
        if not callable(list_tasks):
            raise ApplicationValidationError(
                "Cette source de tâches exige une synchronisation ciblée par projet.",
                code="task_catalog_project_sync_required",
            )
        return self._synchronize_rows(tuple(list_tasks()))

    def synchronize_project(
        self,
        *,
        project_external_id: str,
        project_number: str,
    ) -> TaskCatalogProjectSyncResult:
        project_external = str(project_external_id or "").strip()
        project = str(project_number or "").strip()
        if not project_external:
            raise ApplicationValidationError(
                "L'identifiant ERP du projet est requis pour synchroniser RP_ProjectTasks.",
                code="task_catalog_project_external_id_required",
            )
        if not project:
            raise ApplicationValidationError(
                "Un projet est requis pour synchroniser RP_ProjectTasks.",
                code="task_catalog_project_required",
            )

        fetch_snapshot = getattr(self._source, "fetch_project_snapshot", None)
        if not callable(fetch_snapshot):
            raise ApplicationValidationError(
                "Cette source ne supporte pas la synchronisation ciblée par projet.",
                code="task_catalog_project_sync_unsupported",
                context={"project_number": project},
            )

        started_at = perf_counter()
        snapshot = fetch_snapshot(
            project_external_id=project_external,
            project_number=project,
        )
        snapshot_project = str(snapshot.project_number or "").strip()
        if snapshot_project != project:
            raise ApplicationValidationError(
                "Le snapshot de tâches ne correspond pas au projet demandé.",
                code="task_catalog_project_mismatch",
                context={
                    "expected_project_number": project,
                    "project_number": snapshot_project or None,
                },
            )

        base = self._synchronize_rows(
            snapshot.items,
            expected_project_number=project,
            apply_workforce_policy=True,
        )
        result = TaskCatalogProjectSyncResult(
            project_number=project,
            source_rows=int(snapshot.source_rows),
            task_count=base.received,
            rejected_rows=int(snapshot.rejected_rows),
            created=base.created,
            updated=base.updated,
            unchanged=base.unchanged,
            deactivated=base.deactivated,
            ignored=base.ignored,
            duration_ms=max(0, int((perf_counter() - started_at) * 1000)),
        )
        if self._sync_metadata_repository is not None:
            self._sync_metadata_repository.record_project_sync_success(
                project_number=project,
                source_rows=result.source_rows,
                task_count=result.task_count,
                rejected_rows=result.rejected_rows,
                duration_ms=result.duration_ms,
            )
        return result
