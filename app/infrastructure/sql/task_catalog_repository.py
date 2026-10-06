from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json

from sqlalchemy import or_, select, true, update
from sqlalchemy.orm import Session

from ...application.errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
)
from ...application.task_catalog import (
    TaskCatalogItem,
    TaskCatalogProjectSyncMetadata,
    TaskCatalogProjectSyncMetadataRepositoryPort,
    TaskCatalogRepositoryPort,
    TaskPreferredResourceMutationResult,
)
from .identity_models import AppUser
from .models import (
    Resource,
    TaskCatalogEntry,
    TaskCatalogPreferredResourceAudit,
    TaskCatalogProjectSyncState,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


def _diagnostics_text(values: tuple[str, ...]) -> str | None:
    normalized = tuple(str(value).strip() for value in values if str(value).strip())
    return json.dumps(list(dict.fromkeys(normalized)), ensure_ascii=False) if normalized else None


def _diagnostics(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item).strip() for item in parsed if str(item).strip())


class SqlTaskCatalogRepository(
    TaskCatalogRepositoryPort,
    TaskCatalogProjectSyncMetadataRepositoryPort,
):
    """SQL-backed durable task catalog with legacy and ERP identities.

    Existing #271 rows remain keyed locally by their stable row id. When an OData
    record first arrives, matching (project_number, task_code) rows adopt TaskID in
    place so RequestLine/approval-history foreign keys never need to be rewritten.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _item(row: TaskCatalogEntry) -> TaskCatalogItem:
        return TaskCatalogItem(
            project_number=row.project_number,
            code=row.task_code,
            label=row.label,
            status=row.status,
            active=bool(row.active),
            billing_rule=row.billing_rule,
            allocation_rule=row.allocation_rule,
            completion_percent=(
                float(row.completion_percent)
                if row.completion_percent is not None
                else None
            ),
            erp_created_at=row.erp_created_at,
            branch=row.branch,
            approver_name=row.approver_name,
            cv_enabled=row.cv_enabled,
            time_entry_enabled=row.time_entry_enabled,
            expenses_enabled=row.expenses_enabled,
            erp_task_id=row.erp_task_id,
            account_group=row.account_group,
            cost_code=row.cost_code,
            inventory_id=row.inventory_id,
            budget_amount_cad=row.budget_amount_cad,
            budget_actual_cad=row.budget_actual_cad,
            budget_diagnostic=row.budget_diagnostic,
            workforce_eligible=row.workforce_eligible,
            resource_class_code=row.resource_class_code,
            average_hourly_cost_cad=row.average_hourly_cost_cad,
            budget_hours=row.budget_hours,
            workforce_diagnostics=_diagnostics(row.workforce_diagnostics),
            id=row.id,
            operational_responsible_contact_id=row.operational_responsible_contact_id,
            coordinator_contact_id=row.coordinator_contact_id,
            preferred_resource_id=row.preferred_resource_id,
            preferred_resource_version=int(row.preferred_resource_version or 1),
        )

    def upsert(self, item: TaskCatalogItem) -> str:
        project_number = _text(item.project_number)
        task_code = _text(item.code)
        erp_task_id = _optional_text(item.erp_task_id)

        business_row = self._session.scalar(
            select(TaskCatalogEntry).where(
                TaskCatalogEntry.project_number == project_number,
                TaskCatalogEntry.task_code == task_code,
            )
        )
        erp_row = None
        if erp_task_id is not None:
            erp_row = self._session.scalar(
                select(TaskCatalogEntry).where(
                    TaskCatalogEntry.erp_task_id == erp_task_id,
                )
            )
            if (
                erp_row is not None
                and business_row is not None
                and erp_row.id != business_row.id
            ):
                raise ApplicationConflictError(
                    "L'identité ERP TaskID entre en conflit avec une tâche locale existante.",
                    code="task_catalog_erp_identity_conflict",
                    context={
                        "project_number": project_number,
                        "task_code": task_code,
                    },
                )

        row = erp_row or business_row
        if erp_task_id is not None and item.workforce_eligible is False and row is None:
            return "ignored"

        values: dict[str, object] = {
            "project_number": project_number,
            "task_code": task_code,
            "label": _text(item.label),
            "status": _text(item.status) or ("Actif" if item.active else "Inactif"),
            "active": bool(item.active),
            "billing_rule": _optional_text(item.billing_rule),
            "allocation_rule": _optional_text(item.allocation_rule),
            "completion_percent": (
                Decimal(str(item.completion_percent))
                if item.completion_percent is not None
                else None
            ),
            "erp_created_at": item.erp_created_at,
            "branch": _optional_text(item.branch),
            "approver_name": _optional_text(item.approver_name),
            "cv_enabled": item.cv_enabled,
            "time_entry_enabled": item.time_entry_enabled,
            "expenses_enabled": item.expenses_enabled,
        }

        # File fallback rows do not own OData identity/budget fields and therefore
        # must never clear them when #271 is replayed after an OData sync.
        if erp_task_id is not None:
            values.update(
                {
                    "erp_task_id": erp_task_id,
                    "account_group": _optional_text(item.account_group),
                    "cost_code": _optional_text(item.cost_code),
                    "inventory_id": _optional_text(item.inventory_id),
                    "budget_amount_cad": item.budget_amount_cad,
                    "budget_actual_cad": item.budget_actual_cad,
                    "budget_diagnostic": _optional_text(item.budget_diagnostic),
                    "workforce_eligible": item.workforce_eligible,
                    "resource_class_code": _optional_text(item.resource_class_code),
                    "average_hourly_cost_cad": item.average_hourly_cost_cad,
                    "budget_hours": item.budget_hours,
                    "workforce_diagnostics": _diagnostics_text(item.workforce_diagnostics),
                }
            )

        freshness_at = datetime.now(timezone.utc) if erp_task_id is not None else None
        if row is None:
            if freshness_at is not None:
                values["erp_budget_last_success_at"] = freshness_at
            self._session.add(TaskCatalogEntry(**values))
            self._session.flush()
            return "created"

        was_active = bool(row.active)
        changed = False
        for field, value in values.items():
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed = True

        # ERP freshness is provenance, not a business-value change. Refresh it for
        # every task actually received/integrated while preserving idempotent
        # created/updated/unchanged counters.
        if freshness_at is not None:
            row.erp_budget_last_success_at = freshness_at
        self._session.flush()
        if not changed:
            return "unchanged"
        return "deactivated" if was_active and not bool(row.active) else "updated"

    def search(
        self,
        *,
        project_number: str | None = None,
        query: str | None = None,
        active_only: bool = True,
        limit: int = 200,
    ) -> tuple[TaskCatalogItem, ...]:
        statement = select(TaskCatalogEntry)
        project = _text(project_number)
        if project:
            statement = statement.where(TaskCatalogEntry.project_number == project)
        if active_only:
            statement = statement.where(
                TaskCatalogEntry.active == true(),
                or_(
                    TaskCatalogEntry.workforce_eligible == true(),
                    TaskCatalogEntry.workforce_eligible.is_(None),
                ),
            )
        wanted = _text(query)
        if wanted:
            pattern = f"%{wanted}%"
            statement = statement.where(
                or_(
                    TaskCatalogEntry.task_code.ilike(pattern),
                    TaskCatalogEntry.label.ilike(pattern),
                )
            )
        statement = statement.order_by(
            TaskCatalogEntry.project_number,
            TaskCatalogEntry.task_code,
        ).limit(max(1, min(int(limit), 500)))
        rows = self._session.scalars(statement).all()
        return tuple(self._item(row) for row in rows)


    def set_preferred_resource(
        self,
        task_catalog_item_id: str,
        resource_id: str | None,
        *,
        actor_user_id: str | None,
        expected_version: int,
    ) -> TaskPreferredResourceMutationResult:
        task_id = _text(task_catalog_item_id)
        if not task_id:
            raise ApplicationValidationError(
                "La tâche projet est requise.",
                code="task_preferred_resource_task_required",
            )
        actor_id = _text(actor_user_id)
        if not actor_id:
            raise ApplicationAuthorizationError(
                "Une identité locale authentifiée est requise pour administrer la ressource attitrée.",
                code="task_preferred_resource_actor_required",
            )
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ApplicationValidationError(
                "La version attendue de la ressource attitrée est invalide.",
                code="task_preferred_resource_version_invalid",
            ) from exc
        if expected < 1:
            raise ApplicationValidationError(
                "La version attendue de la ressource attitrée doit être positive.",
                code="task_preferred_resource_version_invalid",
                context={"expected_version": expected},
            )

        preferred_id = _optional_text(resource_id)
        with self._session.begin_nested():
            actor = self._session.get(AppUser, actor_id)
            if actor is None:
                raise ApplicationNotFoundError(
                    "Utilisateur acteur introuvable.",
                    code="task_preferred_resource_actor_not_found",
                    context={"actor_user_id": actor_id},
                )

            task = self._session.get(TaskCatalogEntry, task_id)
            if task is None:
                raise ApplicationNotFoundError(
                    "Tâche projet introuvable.",
                    code="task_catalog_item_not_found",
                    context={"task_catalog_item_id": task_id},
                )
            old_resource_id = _optional_text(task.preferred_resource_id)

            if preferred_id is not None:
                resource = self._session.get(Resource, preferred_id)
                if resource is None:
                    raise ApplicationNotFoundError(
                        "Ressource attitrée introuvable.",
                        code="task_preferred_resource_not_found",
                        context={"resource_id": preferred_id},
                    )
                if not bool(resource.active):
                    raise ApplicationValidationError(
                        "Une ressource locale inactive ne peut pas être nouvellement attitrée.",
                        code="task_preferred_resource_inactive",
                        context={"resource_id": preferred_id},
                    )
                if not bool(resource.erp_active):
                    raise ApplicationValidationError(
                        "Une ressource inactive côté ERP ne peut pas être nouvellement attitrée.",
                        code="task_preferred_resource_erp_inactive",
                        context={"resource_id": preferred_id},
                    )

            result = self._session.execute(
                update(TaskCatalogEntry)
                .where(
                    TaskCatalogEntry.id == task_id,
                    TaskCatalogEntry.preferred_resource_version == expected,
                )
                .values(
                    preferred_resource_id=preferred_id,
                    preferred_resource_version=(
                        TaskCatalogEntry.preferred_resource_version + 1
                    ),
                )
            )
            if int(result.rowcount or 0) != 1:
                current = self._session.scalar(
                    select(TaskCatalogEntry.preferred_resource_version).where(
                        TaskCatalogEntry.id == task_id
                    )
                )
                if current is None:
                    raise ApplicationNotFoundError(
                        "Tâche projet introuvable.",
                        code="task_catalog_item_not_found",
                        context={"task_catalog_item_id": task_id},
                    )
                raise ApplicationConflictError(
                    "La ressource attitrée a été modifiée depuis sa lecture.",
                    code="task_preferred_resource_version_conflict",
                    context={
                        "task_catalog_item_id": task_id,
                        "expected_version": expected,
                        "current_version": int(current),
                    },
                )

            resulting_version = expected + 1
            action = (
                "TASK_PREFERRED_RESOURCE_CLEARED"
                if preferred_id is None
                else "TASK_PREFERRED_RESOURCE_SET"
            )
            self._session.add(
                TaskCatalogPreferredResourceAudit(
                    task_catalog_item_id=task_id,
                    actor_user_id=actor_id,
                    action=action,
                    old_resource_id=old_resource_id,
                    new_resource_id=preferred_id,
                    resulting_version=resulting_version,
                )
            )
            self._session.flush()

        return TaskPreferredResourceMutationResult(
            task_catalog_item_id=task_id,
            preferred_resource_id=preferred_id,
            version=resulting_version,
            action=action,
        )

    def record_project_sync_success(
        self,
        *,
        project_number: str,
        source_rows: int,
        task_count: int,
        rejected_rows: int,
        duration_ms: int | None,
    ) -> None:
        project = _text(project_number)
        now = datetime.now(timezone.utc)
        row = self._session.get(TaskCatalogProjectSyncState, project)
        if row is None:
            row = TaskCatalogProjectSyncState(
                project_number=project,
                last_attempt_at=now,
                last_success_at=now,
                source_rows=max(0, int(source_rows)),
                task_count=max(0, int(task_count)),
                rejected_rows=max(0, int(rejected_rows)),
                duration_ms=duration_ms,
                last_error_code=None,
            )
            self._session.add(row)
        else:
            row.last_attempt_at = now
            row.last_success_at = now
            row.source_rows = max(0, int(source_rows))
            row.task_count = max(0, int(task_count))
            row.rejected_rows = max(0, int(rejected_rows))
            row.duration_ms = duration_ms
            row.last_error_code = None
        self._session.flush()

    def get_project_sync_metadata(
        self,
        project_number: str,
    ) -> TaskCatalogProjectSyncMetadata | None:
        project = _text(project_number)
        if not project:
            return None
        row = self._session.get(TaskCatalogProjectSyncState, project)
        if row is None:
            return None
        return TaskCatalogProjectSyncMetadata(
            project_number=row.project_number,
            last_success_at=row.last_success_at,
            source_rows=int(row.source_rows),
            task_count=int(row.task_count),
            rejected_rows=int(row.rejected_rows),
            duration_ms=row.duration_ms,
            last_error_code=row.last_error_code,
        )
