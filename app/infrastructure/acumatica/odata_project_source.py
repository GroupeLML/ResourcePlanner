from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Final
from urllib.parse import urljoin

import httpx

from ...application import ApplicationOperationError, ExternalProjectRecord, ProjectSourcePort
from .odata_atom import (
    ATOM_NAMESPACE,
    DATA_NAMESPACE,
    METADATA_NAMESPACE,
    ODataAtomFeedError,
    classify_http_failure as _http_failure_kind,
    optional_text as _optional_text,
    parse_atom_feed,
)


logger = logging.getLogger(__name__)

_INT32_MIN: Final = -(2**31)
_INT32_MAX: Final = 2**31 - 1


class ODataProjectFeedError(ValueError):
    """Contract error without retaining or exposing the offending business value."""

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
class ODataProjectRecord:
    project_id: int
    project_code: str
    project_name: str
    customer_id: str | None = None
    customer_name: str | None = None
    project_manager_id: str | None = None
    project_manager_name: str | None = None
    status: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    default_branch_code: str | None = None
    default_branch_description: str | None = None
    last_modified_at: datetime | None = None
    base_type: str | None = None

    def to_external_record(self) -> ExternalProjectRecord:
        return ExternalProjectRecord(
            external_id=str(self.project_id),
            number=self.project_code,
            name=self.project_name,
            client=self.customer_name,
            project_manager_external_id=self.project_manager_id,
            project_manager_name=self.project_manager_name,
            status=self.status or "active",
        )


def _required_text(values: Mapping[str, str | None], field: str, *, entry_index: int) -> str:
    value = _optional_text(values.get(field))
    if value is None:
        raise ODataProjectFeedError(
            "required_field_missing",
            field=field,
            entry_index=entry_index,
        )
    return value


def _parse_int32(value: str, field: str, *, entry_index: int) -> int:
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise ODataProjectFeedError(
            "invalid_int32",
            field=field,
            entry_index=entry_index,
        ) from exc
    if not _INT32_MIN <= parsed <= _INT32_MAX:
        raise ODataProjectFeedError(
            "invalid_int32",
            field=field,
            entry_index=entry_index,
        )
    return parsed


def _parse_datetime(
    value: str | None,
    field: str,
    *,
    entry_index: int,
) -> datetime | None:
    text = _optional_text(value)
    if text is None:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ODataProjectFeedError(
            "invalid_datetime",
            field=field,
            entry_index=entry_index,
        ) from exc


def parse_rp_projects_feed(xml_payload: bytes | str) -> tuple[ODataProjectRecord, ...]:
    """Parse one complete RP_Projects Atom feed using the shared OData envelope parser."""

    try:
        feed = parse_atom_feed(xml_payload)
    except ODataAtomFeedError as exc:
        raise ODataProjectFeedError(
            exc.reason,
            entry_index=exc.entry_index,
        ) from exc

    records: list[ODataProjectRecord] = []
    for entry_index, entry in enumerate(feed.entries):
        values = entry.values()
        project_id_text = _required_text(values, "ProjectId", entry_index=entry_index)
        project_code = _required_text(values, "ProjectCode", entry_index=entry_index)
        project_name = _required_text(values, "ProjectName", entry_index=entry_index)
        records.append(
            ODataProjectRecord(
                project_id=_parse_int32(project_id_text, "ProjectId", entry_index=entry_index),
                project_code=project_code,
                project_name=project_name,
                customer_id=_optional_text(values.get("CustomerID")),
                customer_name=_optional_text(values.get("CustomerName")),
                project_manager_id=_optional_text(values.get("ProjectManagerId")),
                project_manager_name=_optional_text(values.get("ProjectManagerName")),
                status=_optional_text(values.get("Status")),
                start_date=_parse_datetime(values.get("StartDate"), "StartDate", entry_index=entry_index),
                end_date=_parse_datetime(values.get("EndDate"), "EndDate", entry_index=entry_index),
                default_branch_code=_optional_text(values.get("DefaultBranchCode")),
                default_branch_description=_optional_text(values.get("DefaultBranchCode_Desc")),
                last_modified_at=_parse_datetime(
                    values.get("LastModifiedDateTime"),
                    "LastModifiedDateTime",
                    entry_index=entry_index,
                ),
                base_type=_optional_text(values.get("BaseType")),
            )
        )
    return tuple(records)


@dataclass(frozen=True, slots=True)
class ODataProjectSourceSettings:
    """Runtime settings for the observed RP_Projects OData contract."""

    base_url: str
    feed_path: str = "/oDATA/RP_Projects"
    timeout_seconds: float = 30.0
    page_size: int = 100
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
            "pagination": {
                "mode": "top_skip",
                "orderby": "ProjectId asc",
                "page_size": self.page_size,
            },
        }


class ODataProjectSource(ProjectSourcePort):
    """Read-only RP_Projects source using an Atom/XML OData feed."""

    def __init__(
        self,
        settings: ODataProjectSourceSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        # Generic seam for #207B. 207A deliberately does not choose Basic, bearer,
        # OAuth or cookie/session authentication.
        self._request_headers = dict(request_headers or {})
        self._last_read_evidence: dict[str, object] | None = None

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
        # Never log URL/host, injected auth headers, exception details, response body,
        # or project data. Only stable contract metadata is emitted.
        logger.warning(
            "Acumatica OData project read failed kind=%s retryable=%s status=%s feed=%s reason=%s",
            failure_kind,
            retryable,
            http_status if http_status is not None else "-",
            self._settings.feed_path,
            reason or "-",
        )

    def list_projects(self) -> tuple[ExternalProjectRecord, ...]:
        headers = {
            "Accept": "application/atom+xml, application/xml;q=0.9",
            **self._request_headers,
        }
        auth = (
            httpx.BasicAuth(self._settings.username, self._settings.credential)
            if self._settings.username and self._settings.credential
            else None
        )
        records: list[ODataProjectRecord] = []
        seen_project_ids: set[int] = set()
        page_count = 0
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
                            "$orderby": "ProjectId asc",
                            "$top": str(self._settings.page_size),
                            "$skip": str(skip),
                        },
                    )
                    response.raise_for_status()
                    try:
                        page = parse_rp_projects_feed(response.content)
                    except ODataProjectFeedError as exc:
                        self._log_failure(
                            failure_kind="invalid_payload",
                            retryable=False,
                            http_status=response.status_code,
                            reason=exc.reason,
                        )
                        raise ApplicationOperationError(
                            "La réponse OData Acumatica des projets est invalide.",
                            code="acumatica_project_response_invalid",
                            context=self._safe_context(
                                failure_kind="invalid_payload",
                                retryable=False,
                                http_status=response.status_code,
                                reason=exc.reason,
                                field=exc.field,
                                entry_index=exc.entry_index,
                            ),
                        ) from exc

                    page_count += 1
                    duplicate_id = next(
                        (record.project_id for record in page if record.project_id in seen_project_ids),
                        None,
                    )
                    if duplicate_id is not None:
                        self._log_failure(
                            failure_kind="invalid_payload",
                            retryable=False,
                            http_status=response.status_code,
                            reason="pagination_duplicate_project",
                        )
                        raise ApplicationOperationError(
                            "La pagination OData Acumatica des projets est incohérente.",
                            code="acumatica_project_response_invalid",
                            context=self._safe_context(
                                failure_kind="invalid_payload",
                                retryable=False,
                                http_status=response.status_code,
                                reason="pagination_duplicate_project",
                            ),
                        )

                    records.extend(page)
                    seen_project_ids.update(record.project_id for record in page)
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
                "Impossible de lire les projets depuis Acumatica.",
                code="acumatica_project_read_failed",
                context=self._safe_context(
                    failure_kind=failure_kind,
                    retryable=retryable,
                    http_status=status_code,
                ),
            ) from exc
        except httpx.TimeoutException as exc:
            self._log_failure(failure_kind="timeout", retryable=True)
            raise ApplicationOperationError(
                "Impossible de lire les projets depuis Acumatica.",
                code="acumatica_project_read_failed",
                context=self._safe_context(failure_kind="timeout", retryable=True),
            ) from exc
        except httpx.RequestError as exc:
            self._log_failure(failure_kind="network", retryable=True)
            raise ApplicationOperationError(
                "Impossible de lire les projets depuis Acumatica.",
                code="acumatica_project_read_failed",
                context=self._safe_context(failure_kind="network", retryable=True),
            ) from exc

        self._last_read_evidence = {
            "protocol": "odata",
            "feed_path": self._settings.feed_path,
            "pagination_mode": "top_skip",
            "orderby": "ProjectId asc",
            "page_size": int(self._settings.page_size),
            "pages": page_count,
            "rows": len(records),
            "complete": True,
        }
        return tuple(record.to_external_record() for record in records)

    def last_read_evidence(self) -> dict[str, object] | None:
        return (
            dict(self._last_read_evidence)
            if self._last_read_evidence is not None
            else None
        )
