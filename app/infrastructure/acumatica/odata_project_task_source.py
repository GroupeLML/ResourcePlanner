from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import logging
from typing import Final
from urllib.parse import urljoin

import httpx

from ...application import (
    ApplicationOperationError,
    TaskCatalogItem,
    TaskCatalogProjectSnapshot,
)
from .odata_atom import (
    ODataAtomFeedError,
    classify_http_failure as _http_failure_kind,
    optional_text as _optional_text,
    parse_atom_feed,
)


logger = logging.getLogger(__name__)

_INT32_MIN: Final = -(2**31)
_INT32_MAX: Final = 2**31 - 1
_WORKFORCE_ACCOUNT_GROUP: Final = "DEPMO"


class ODataProjectTaskFeedError(ValueError):
    """Contract error without retaining or exposing offending business values."""

    def __init__(
        self,
        reason: str,
        *,
        field: str | None = None,
        entry_index: int | None = None,
    ) -> None:
        self.reason = reason
        self.field = field
        self.entry_index = entry_index
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ODataProjectTaskRecord:
    project_erp_id: int
    project_code: str
    task_id: int
    task_code: str
    task_description: str
    status: str
    account_group: str
    budget_amount_cad: Decimal | None
    budget_actual_cad: Decimal | None
    cost_code: str | None = None
    inventory_id: str | None = None


def _required_text(
    values: Mapping[str, str | None],
    field: str,
    *,
    entry_index: int,
    aliases: tuple[str, ...] = (),
) -> str:
    for candidate in (field, *aliases):
        value = _optional_text(values.get(candidate))
        if value is not None:
            return value
    raise ODataProjectTaskFeedError(
        "required_field_missing",
        field=field,
        entry_index=entry_index,
    )


def _parse_int32(value: str, field: str, *, entry_index: int) -> int:
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise ODataProjectTaskFeedError(
            "invalid_int32",
            field=field,
            entry_index=entry_index,
        ) from exc
    if not _INT32_MIN <= parsed <= _INT32_MAX:
        raise ODataProjectTaskFeedError(
            "invalid_int32",
            field=field,
            entry_index=entry_index,
        )
    return parsed


def _parse_decimal(
    value: str | None,
    field: str,
    *,
    entry_index: int,
) -> Decimal | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ODataProjectTaskFeedError(
            "invalid_decimal",
            field=field,
            entry_index=entry_index,
        ) from exc


def parse_rp_project_tasks_feed(
    xml_payload: bytes | str,
) -> tuple[ODataProjectTaskRecord, ...]:
    """Parse one RP_ProjectTasks Atom page without applying workforce filtering."""

    try:
        feed = parse_atom_feed(xml_payload)
    except ODataAtomFeedError as exc:
        raise ODataProjectTaskFeedError(
            exc.reason,
            entry_index=exc.entry_index,
        ) from exc

    records: list[ODataProjectTaskRecord] = []
    for entry_index, entry in enumerate(feed.entries):
        values = entry.values()
        project_erp_id_text = _required_text(
            values,
            "ProjetID",
            entry_index=entry_index,
        )
        project_code = _required_text(
            values,
            "ProjectID",
            aliases=("ProjectCD", "ProjetCD"),
            entry_index=entry_index,
        )
        task_id_text = _required_text(values, "TaskID", entry_index=entry_index)
        records.append(
            ODataProjectTaskRecord(
                project_erp_id=_parse_int32(
                    project_erp_id_text,
                    "ProjetID",
                    entry_index=entry_index,
                ),
                project_code=project_code,
                task_id=_parse_int32(
                    task_id_text,
                    "TaskID",
                    entry_index=entry_index,
                ),
                task_code=_required_text(
                    values,
                    "TaskCD",
                    entry_index=entry_index,
                ),
                task_description=_required_text(
                    values,
                    "TaskDescription",
                    entry_index=entry_index,
                ),
                status=_required_text(
                    values,
                    "Status",
                    entry_index=entry_index,
                ),
                account_group=_required_text(
                    values,
                    "AccountGroup",
                    entry_index=entry_index,
                ),
                budget_amount_cad=_parse_decimal(
                    values.get("BudgetAmount"),
                    "BudgetAmount",
                    entry_index=entry_index,
                ),
                budget_actual_cad=_parse_decimal(
                    values.get("BudgetActual"),
                    "BudgetActual",
                    entry_index=entry_index,
                ),
                cost_code=_optional_text(values.get("CostCode")),
                inventory_id=_optional_text(values.get("InventoryID")),
            )
        )
    return tuple(records)


def _sum_optional_decimal(
    left: Decimal | None,
    right: Decimal | None,
) -> Decimal | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _common_text(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    return left if left == right else None


def _budget_diagnostic(amount: Decimal | None) -> str | None:
    if amount is None:
        return "budget_amount_missing"
    if amount == 0:
        return "budget_amount_zero"
    if amount < 0:
        return "budget_amount_negative"
    return None


@dataclass(slots=True)
class _AggregatedTask:
    project_code: str
    task_id: int
    task_code: str
    label: str
    status: str
    active: bool
    budget_amount_cad: Decimal | None
    budget_actual_cad: Decimal | None
    cost_code: str | None
    inventory_id: str | None

    def add(self, row: ODataProjectTaskRecord, *, entry_index: int) -> None:
        if (
            row.project_code != self.project_code
            or row.task_code != self.task_code
            or row.task_description != self.label
        ):
            raise ODataProjectTaskFeedError(
                "task_identity_inconsistent",
                field="TaskID",
                entry_index=entry_index,
            )
        if row.status != self.status:
            raise ODataProjectTaskFeedError(
                "task_status_inconsistent",
                field="Status",
                entry_index=entry_index,
            )
        self.budget_amount_cad = _sum_optional_decimal(
            self.budget_amount_cad,
            row.budget_amount_cad,
        )
        self.budget_actual_cad = _sum_optional_decimal(
            self.budget_actual_cad,
            row.budget_actual_cad,
        )
        self.cost_code = _common_text(self.cost_code, row.cost_code)
        self.inventory_id = _common_text(self.inventory_id, row.inventory_id)

    def to_item(self) -> TaskCatalogItem:
        return TaskCatalogItem(
            project_number=self.project_code,
            code=self.task_code,
            label=self.label,
            status=self.status,
            active=self.active,
            erp_task_id=str(self.task_id),
            account_group=_WORKFORCE_ACCOUNT_GROUP,
            cost_code=self.cost_code,
            inventory_id=self.inventory_id,
            budget_amount_cad=self.budget_amount_cad,
            budget_actual_cad=self.budget_actual_cad,
            budget_diagnostic=_budget_diagnostic(self.budget_amount_cad),
        )


@dataclass(frozen=True, slots=True)
class ODataProjectTaskSourceSettings:
    """Settings for the validated RP_ProjectTasks targeted synchronization contract."""

    base_url: str
    feed_path: str = "/oDATA/RP_ProjectTasks"
    timeout_seconds: float = 30.0
    page_size: int = 500
    username: str | None = field(default=None, repr=False)
    credential: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.page_size <= 0:
            raise ValueError("page_size must be positive")
        if bool(self.username) != bool(self.credential):
            raise ValueError("username and credential must be configured together")

    def safe_summary(self) -> dict[str, object]:
        return {
            "protocol": "odata",
            "feed_path": self.feed_path,
            "authentication": "basic" if self.username else "none",
            "scope": "project",
            "workforce_account_group": _WORKFORCE_ACCOUNT_GROUP,
            "pagination": {
                "mode": "top_skip",
                "orderby": "TaskID asc",
                "page_size": self.page_size,
            },
            "runtime_wired": True,
        }


class ODataProjectTaskSource:
    """Read-only targeted RP_ProjectTasks source.

    The server-side ProjetID and DEPMO filters reduce volume. The technical project
    identity and DEPMO constraint are reapplied after parsing; the padded business
    project code is checked separately before any TaskCatalogItem reaches persistence.
    Multiple DEPMO budget rows with the same TaskID are aggregated into one task.
    """

    def __init__(
        self,
        settings: ODataProjectTaskSourceSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._request_headers = dict(request_headers or {})

    def _url(self) -> str:
        base = self._settings.base_url.rstrip("/") + "/"
        return urljoin(base, self._settings.feed_path.lstrip("/"))

    def _safe_context(
        self,
        *,
        failure_kind: str,
        retryable: bool,
        http_status: int | None = None,
        reason: str | None = None,
        field: str | None = None,
        entry_index: int | None = None,
    ) -> dict[str, object]:
        context: dict[str, object] = {
            "protocol": "odata",
            "feed_path": self._settings.feed_path,
            "failure_kind": failure_kind,
            "retryable": retryable,
        }
        if http_status is not None:
            context["http_status"] = int(http_status)
        if reason:
            context["reason"] = reason
        if field:
            context["field"] = field
        if entry_index is not None:
            context["entry_index"] = int(entry_index)
        return context

    def _log_failure(
        self,
        *,
        failure_kind: str,
        retryable: bool,
        http_status: int | None = None,
        reason: str | None = None,
    ) -> None:
        logger.warning(
            "Acumatica OData project-task read failed kind=%s retryable=%s status=%s feed=%s reason=%s",
            failure_kind,
            retryable,
            http_status if http_status is not None else "-",
            self._settings.feed_path,
            reason or "-",
        )

    def fetch_project_snapshot(
        self,
        *,
        project_external_id: str,
        project_number: str,
    ) -> TaskCatalogProjectSnapshot:
        external_id = str(project_external_id or "").strip()
        project = str(project_number or "").strip()
        if not external_id:
            raise ValueError("project_external_id is required")
        if not project:
            raise ValueError("project_number is required")
        try:
            project_erp_id = int(external_id, 10)
        except ValueError as exc:
            raise ValueError("project_external_id must be an Edm.Int32 value") from exc
        if not _INT32_MIN <= project_erp_id <= _INT32_MAX:
            raise ValueError("project_external_id must be an Edm.Int32 value")

        headers = {
            "Accept": "application/atom+xml, application/xml;q=0.9",
            **self._request_headers,
        }
        auth = (
            httpx.BasicAuth(self._settings.username, self._settings.credential)
            if self._settings.username and self._settings.credential
            else None
        )
        raw_rows: list[ODataProjectTaskRecord] = []
        skip = 0
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._settings.timeout_seconds,
                follow_redirects=True,
                auth=auth,
            ) as client:
                while True:
                    response = client.get(
                        self._url(),
                        headers=headers,
                        params={
                            "$filter": (
                                f"ProjetID eq {project_erp_id} "
                                "and AccountGroup eq 'DEPMO'"
                            ),
                            "$orderby": "TaskID asc",
                            "$top": str(self._settings.page_size),
                            "$skip": str(skip),
                        },
                    )
                    response.raise_for_status()
                    try:
                        page = parse_rp_project_tasks_feed(response.content)
                    except ODataProjectTaskFeedError as exc:
                        self._log_failure(
                            failure_kind="invalid_payload",
                            retryable=False,
                            http_status=response.status_code,
                            reason=exc.reason,
                        )
                        raise ApplicationOperationError(
                            "La réponse OData Acumatica des tâches projet est invalide.",
                            code="acumatica_project_task_response_invalid",
                            context=self._safe_context(
                                failure_kind="invalid_payload",
                                retryable=False,
                                http_status=response.status_code,
                                reason=exc.reason,
                                field=exc.field,
                                entry_index=exc.entry_index,
                            ),
                        ) from exc

                    raw_rows.extend(page)
                    if len(page) < self._settings.page_size:
                        break
                    skip += len(page)
        except ApplicationOperationError:
            raise
        except httpx.HTTPStatusError as exc:
            status_code = int(exc.response.status_code)
            failure_kind, retryable = _http_failure_kind(status_code)
            self._log_failure(
                failure_kind=failure_kind,
                retryable=retryable,
                http_status=status_code,
            )
            raise ApplicationOperationError(
                "Impossible de lire les tâches projet depuis Acumatica.",
                code="acumatica_project_task_read_failed",
                context=self._safe_context(
                    failure_kind=failure_kind,
                    retryable=retryable,
                    http_status=status_code,
                ),
            ) from exc
        except httpx.TimeoutException as exc:
            self._log_failure(failure_kind="timeout", retryable=True)
            raise ApplicationOperationError(
                "Impossible de lire les tâches projet depuis Acumatica.",
                code="acumatica_project_task_read_failed",
                context=self._safe_context(
                    failure_kind="timeout",
                    retryable=True,
                ),
            ) from exc
        except httpx.RequestError as exc:
            self._log_failure(failure_kind="network", retryable=True)
            raise ApplicationOperationError(
                "Impossible de lire les tâches projet depuis Acumatica.",
                code="acumatica_project_task_read_failed",
                context=self._safe_context(
                    failure_kind="network",
                    retryable=True,
                ),
            ) from exc

        aggregated: dict[int, _AggregatedTask] = {}
        rejected_rows = 0
        for entry_index, row in enumerate(raw_rows):
            if (
                row.project_erp_id != project_erp_id
                or row.account_group.strip() != _WORKFORCE_ACCOUNT_GROUP
            ):
                rejected_rows += 1
                continue
            if row.project_code != project:
                rejected_rows += 1
                continue

            existing = aggregated.get(row.task_id)
            if existing is None:
                aggregated[row.task_id] = _AggregatedTask(
                    project_code=row.project_code,
                    task_id=row.task_id,
                    task_code=row.task_code,
                    label=row.task_description,
                    status=row.status,
                    active=row.status.casefold() == "actif",
                    budget_amount_cad=row.budget_amount_cad,
                    budget_actual_cad=row.budget_actual_cad,
                    cost_code=row.cost_code,
                    inventory_id=row.inventory_id,
                )
                continue
            try:
                existing.add(row, entry_index=entry_index)
            except ODataProjectTaskFeedError as exc:
                self._log_failure(
                    failure_kind="invalid_payload",
                    retryable=False,
                    reason=exc.reason,
                )
                raise ApplicationOperationError(
                    "La réponse OData Acumatica des tâches projet est incohérente.",
                    code="acumatica_project_task_response_invalid",
                    context=self._safe_context(
                        failure_kind="invalid_payload",
                        retryable=False,
                        reason=exc.reason,
                        field=exc.field,
                        entry_index=exc.entry_index,
                    ),
                ) from exc

        items = tuple(
            row.to_item()
            for _task_id, row in sorted(aggregated.items(), key=lambda pair: pair[0])
        )
        return TaskCatalogProjectSnapshot(
            project_number=project,
            source_rows=len(raw_rows),
            rejected_rows=rejected_rows,
            items=items,
        )
