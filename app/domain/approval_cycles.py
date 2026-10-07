from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence


APPROVAL_CYCLE_STATE_OPEN = "OPEN"
APPROVAL_CYCLE_STATE_INVALIDATED = "INVALIDATED"
APPROVAL_CYCLE_STATE_COMPLETED = "COMPLETED"

APPROVAL_CYCLE_INIT_SUBMISSION = "SUBMISSION"
APPROVAL_CYCLE_INIT_LEGACY_EXPLICIT = "LEGACY_EXPLICIT"

LEGACY_APPROVAL_CYCLE_ACTIVE = "CYCLE_ACTIVE"
LEGACY_APPROVAL_CYCLE_SUBMITTED_REQUIRES_INITIALIZATION = (
    "SUBMITTED_REQUIRES_INITIALIZATION"
)
LEGACY_APPROVAL_CYCLE_APPROVED_HISTORICAL = "APPROVED_HISTORICAL"
LEGACY_APPROVAL_CYCLE_NONE = "NONE"


@dataclass(frozen=True, slots=True)
class ApprovalSubjectRoutingEntry:
    request_line_id: str
    task_catalog_item_id: str | None
    approval_scope_ids: tuple[str, ...]
    source_kinds: tuple[str, ...]
    proposed_resource_id: str | None = None
    proposed_asset_id: str | None = None
    period_proposed_resources: tuple[tuple[str, str | None], ...] = ()

    def canonical_payload(self) -> dict[str, object]:
        sources = tuple(sorted({str(value) for value in self.source_kinds if str(value)}))
        payload: dict[str, object] = {
            "request_line_id": str(self.request_line_id),
            "task_catalog_item_id": (
                str(self.task_catalog_item_id)
                if self.task_catalog_item_id is not None
                else None
            ),
            "approval_scope_ids": sorted(
                {str(value) for value in self.approval_scope_ids if str(value)}
            ),
            "source_kinds": list(sources),
            "proposed_resource_id": (
                str(self.proposed_resource_id)
                if "PROPOSED_RESOURCE" in sources
                and self.proposed_resource_id is not None
                else None
            ),
        }
        # ASSET_TYPE is emitted only by new #534 cycles. Omitting this key for older
        # cycles preserves their historical fingerprint instead of forcing a backfill.
        if self.period_proposed_resources:
            payload["period_proposed_resources"] = [
                {
                    "period_key": str(period_key),
                    "proposed_resource_id": (
                        str(resource_id) if resource_id is not None else None
                    ),
                }
                for period_key, resource_id in sorted(
                    self.period_proposed_resources,
                    key=lambda row: (str(row[0]), str(row[1] or "")),
                )
            ]
        if "ASSET_TYPE" in sources:
            payload["proposed_asset_id"] = (
                str(self.proposed_asset_id)
                if self.proposed_asset_id is not None
                else None
            )
        return payload


def _canonical_authorization_entry(raw: Mapping[str, object]) -> dict[str, object]:
    entry = dict(raw)
    competency_ids = entry.get("competency_ids")
    if isinstance(competency_ids, (list, tuple)):
        entry["competency_ids"] = sorted(
            {str(value) for value in competency_ids if str(value)}
        )
    return entry


def approval_subject_fingerprint(
    *,
    request_id: str,
    project_id: str,
    priority: str | None,
    site_client: str | None,
    location: str | None,
    line_mode: bool,
    authorization_entries: Sequence[Mapping[str, object]],
    routing_entries: Sequence[ApprovalSubjectRoutingEntry],
) -> str:
    """Hash the business subject submitted for line approval.

    Aggregate versions, votes, timestamps and mutable display labels are intentionally
    excluded.  The #13 authorization envelope carries the substantive line/period
    topology while routing entries capture the stable authority scope used at
    submission time.
    """

    canonical_authorization = [
        _canonical_authorization_entry(entry)
        for entry in authorization_entries
    ]
    canonical_authorization.sort(key=lambda entry: str(entry.get("identity") or ""))

    canonical_routing = [
        entry.canonical_payload()
        for entry in routing_entries
    ]
    canonical_routing.sort(key=lambda entry: str(entry["request_line_id"]))

    payload = {
        "request": {
            "request_id": str(request_id),
            "project_id": str(project_id),
            "priority": priority,
            "site_client": site_client,
            "location": location,
            "line_mode": bool(line_mode),
        },
        "authorization": canonical_authorization,
        "routing": canonical_routing,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
