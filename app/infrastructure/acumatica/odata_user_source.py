from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
from urllib.parse import urljoin

import httpx

from ...application import ApplicationOperationError
from ...application.erp_user_directory import ErpUserSourcePort, ExternalErpUserRecord
from .odata_atom import (
    ODataAtomFeedError,
    classify_http_failure,
    optional_text,
    parse_atom_feed,
)


logger = logging.getLogger(__name__)


class ODataUserFeedError(ValueError):
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
class ODataUserRecord:
    user_id: str
    employee_id: str
    display_name: str
    first_name: str | None
    last_name: str | None
    email: str | None
    user_active: bool
    employee_status: str | None

    def to_external_record(self) -> ExternalErpUserRecord:
        return ExternalErpUserRecord(
            user_id=self.user_id,
            employee_external_id=self.employee_id,
            display_name=self.display_name,
            first_name=self.first_name,
            last_name=self.last_name,
            email=self.email,
            erp_user_active=self.user_active,
            employee_status=self.employee_status,
        )


def _required(
    values: Mapping[str, str | None],
    field: str,
    *,
    entry_index: int,
) -> str:
    value = optional_text(values.get(field))
    if value is None:
        raise ODataUserFeedError(
            "required_field_missing",
            field=field,
            entry_index=entry_index,
        )
    return value


def _boolean(value: str | None, field: str, *, entry_index: int) -> bool:
    text = optional_text(value)
    if text is None:
        raise ODataUserFeedError(
            "required_field_missing",
            field=field,
            entry_index=entry_index,
        )
    normalized = text.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ODataUserFeedError(
        "invalid_boolean",
        field=field,
        entry_index=entry_index,
    )


def parse_rp_users_feed(xml_payload: bytes | str) -> tuple[ODataUserRecord, ...]:
    try:
        feed = parse_atom_feed(xml_payload)
    except ODataAtomFeedError as exc:
        raise ODataUserFeedError(
            exc.reason,
            entry_index=exc.entry_index,
        ) from exc

    rows: list[ODataUserRecord] = []
    for entry_index, entry in enumerate(feed.entries):
        values = entry.values()
        rows.append(
            ODataUserRecord(
                user_id=_required(values, "UserID", entry_index=entry_index),
                employee_id=_required(values, "EmployeID", entry_index=entry_index),
                display_name=_required(
                    values,
                    "UserDisplayName",
                    entry_index=entry_index,
                ),
                first_name=optional_text(values.get("UserFirstName")),
                last_name=optional_text(values.get("UserLastName")),
                email=optional_text(values.get("UserEmail")),
                user_active=_boolean(
                    values.get("UserActif"),
                    "UserActif",
                    entry_index=entry_index,
                ),
                employee_status=optional_text(values.get("EmployeStatus")),
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class ODataUserSourceSettings:
    base_url: str
    feed_path: str = "/oDATA/RP_Users"
    timeout_seconds: float = 30.0
    page_size: int = 100
    username: str | None = field(default=None, repr=False)
    credential: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.page_size <= 0:
            raise ValueError("page_size must be positive")
        if bool(self.username) != bool(self.credential):
            raise ValueError("username and credential must be configured together")


class ODataUserSource(ErpUserSourcePort):
    """Read RP_Users without provisioning AppUser or inferring OIDC identity."""

    def __init__(
        self,
        settings: ODataUserSourceSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._request_headers = dict(request_headers or {})

    def _url(self) -> str:
        return urljoin(
            self._settings.base_url.rstrip("/") + "/",
            self._settings.feed_path.lstrip("/"),
        )

    def list_users(self) -> tuple[ExternalErpUserRecord, ...]:
        auth = (
            httpx.BasicAuth(self._settings.username, self._settings.credential)
            if self._settings.username and self._settings.credential
            else None
        )
        headers = {
            "Accept": "application/atom+xml, application/xml;q=0.9",
            **self._request_headers,
        }
        rows: list[ODataUserRecord] = []
        seen: set[str] = set()
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
                            "$orderby": "UserID asc",
                            "$top": str(self._settings.page_size),
                            "$skip": str(skip),
                        },
                    )
                    response.raise_for_status()
                    try:
                        page = parse_rp_users_feed(response.content)
                    except ODataUserFeedError as exc:
                        raise ApplicationOperationError(
                            "La réponse OData Acumatica des utilisateurs est invalide.",
                            code="acumatica_user_response_invalid",
                            context={
                                "failure_kind": "invalid_payload",
                                "retryable": False,
                                "feed_path": self._settings.feed_path,
                                "reason": exc.reason,
                                **({"field": exc.field} if exc.field else {}),
                                **(
                                    {"entry_index": exc.entry_index}
                                    if exc.entry_index is not None
                                    else {}
                                ),
                            },
                        ) from exc
                    page_ids = [row.user_id for row in page]
                    duplicate = next(
                        (user_id for user_id in page_ids if user_id in seen),
                        None,
                    )
                    if duplicate is None and len(page_ids) != len(set(page_ids)):
                        duplicate = next(
                            user_id
                            for user_id in page_ids
                            if page_ids.count(user_id) > 1
                        )
                    if duplicate is not None:
                        raise ApplicationOperationError(
                            "La pagination OData Acumatica des utilisateurs est incohérente.",
                            code="acumatica_user_response_invalid",
                            context={
                                "failure_kind": "invalid_payload",
                                "retryable": False,
                                "feed_path": self._settings.feed_path,
                                "reason": "pagination_duplicate_user",
                            },
                        )
                    rows.extend(page)
                    seen.update(page_ids)
                    if len(page) < self._settings.page_size:
                        break
                    skip += len(page)
        except ApplicationOperationError:
            raise
        except httpx.HTTPStatusError as exc:
            status = int(exc.response.status_code)
            failure_kind, retryable = classify_http_failure(status)
            raise ApplicationOperationError(
                "Impossible de lire les utilisateurs depuis Acumatica.",
                code="acumatica_user_read_failed",
                context={
                    "failure_kind": failure_kind,
                    "retryable": retryable,
                    "http_status": status,
                    "feed_path": self._settings.feed_path,
                },
            ) from exc
        except httpx.TimeoutException as exc:
            raise ApplicationOperationError(
                "Impossible de lire les utilisateurs depuis Acumatica.",
                code="acumatica_user_read_failed",
                context={
                    "failure_kind": "timeout",
                    "retryable": True,
                    "feed_path": self._settings.feed_path,
                },
            ) from exc
        except httpx.RequestError as exc:
            logger.warning(
                "Acumatica OData user read failed kind=network feed=%s",
                self._settings.feed_path,
            )
            raise ApplicationOperationError(
                "Impossible de lire les utilisateurs depuis Acumatica.",
                code="acumatica_user_read_failed",
                context={
                    "failure_kind": "network",
                    "retryable": True,
                    "feed_path": self._settings.feed_path,
                },
            ) from exc

        return tuple(row.to_external_record() for row in rows)
