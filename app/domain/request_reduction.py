from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


_REQUEST_KEYS = (
    "project_id",
    "site_client",
    "location",
    "line_mode",
)

_ENTRY_KEYS = (
    "identity",
    "project_id",
    "site_id",
    "location",
    "slot_count",
    "line_kind",
    "required_resource_class",
    "competency_ids",
    "task_ref",
    "work_package_ref",
    "start_date",
    "end_date",
    "hours",
    "kind",
    "group",
    "confirmation_mode",
    "proposed_resource_mode",
    "same_as_period_key",
    "asset_type_id",
    "occupancy_policy",
)


def canonical_reduction_subject(
    *,
    request_context: Mapping[str, object] | None,
    authorization_entry: Mapping[str, object],
) -> dict[str, object]:
    request_values = dict(request_context or {})
    entry_values = dict(authorization_entry)
    subject = {
        "request": {
            key: request_values.get(key)
            for key in _REQUEST_KEYS
        },
        "entry": {
            key: entry_values.get(key)
            for key in _ENTRY_KEYS
        },
    }
    competencies = subject["entry"].get("competency_ids")
    if isinstance(competencies, (list, tuple)):
        subject["entry"]["competency_ids"] = sorted(
            str(value) for value in competencies if str(value)
        )
    return subject


def reduction_subject_fingerprint(
    *,
    request_context: Mapping[str, object] | None,
    authorization_entry: Mapping[str, object],
) -> str:
    canonical = canonical_reduction_subject(
        request_context=request_context,
        authorization_entry=authorization_entry,
    )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
