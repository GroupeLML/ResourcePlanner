"""Execution, assignment and evidence contracts for Verification / issue #363D."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from .verification import VerificationExecutionResult, VerificationProjectedStatus


VerificationMeasure = str | int | float | bool | None


def normalize_verification_measurements(
    values: Mapping[str, VerificationMeasure] | None,
) -> dict[str, VerificationMeasure]:
    normalized: dict[str, VerificationMeasure] = {}
    for raw_key, raw_value in dict(values or {}).items():
        key = str(raw_key or "").strip()
        if not key:
            raise ValueError("Verification measurement keys cannot be empty")
        if not isinstance(raw_value, (str, int, float, bool, type(None))):
            raise ValueError(
                "Verification measurements accept only scalar JSON values"
            )
        if isinstance(raw_value, float) and not math.isfinite(raw_value):
            raise ValueError("Verification measurements must be finite")
        normalized[key] = raw_value
    return normalized


def normalize_https_evidence_url(value: str) -> str:
    normalized = str(value or "").strip()
    parts = urlsplit(normalized)
    if (
        parts.scheme.lower() != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("Verification evidence must be an external HTTPS link")
    return normalized


@dataclass(frozen=True, slots=True)
class VerificationExecutorAssignment:
    id: str
    requirement_id: str
    executor_user_id: str
    assigned_by_user_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "requirement_id",
            "executor_user_id",
            "assigned_by_user_id",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} requires a stable identifier")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class VerificationExecution:
    id: str
    requirement_id: str
    revision_id: str
    sequence: int
    result: VerificationExecutionResult
    executor_user_id: str
    executed_at: datetime | None = None
    measurements: Mapping[str, VerificationMeasure] | None = None
    comments: str | None = None
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "requirement_id",
            "revision_id",
            "executor_user_id",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} requires a stable identifier")
            object.__setattr__(self, field_name, value)
        if self.sequence < 1:
            raise ValueError("Verification execution sequence must be at least 1")
        object.__setattr__(self, "result", VerificationExecutionResult(self.result))
        object.__setattr__(
            self,
            "measurements",
            normalize_verification_measurements(self.measurements),
        )
        object.__setattr__(
            self,
            "comments",
            str(self.comments or "").strip() or None,
        )


@dataclass(frozen=True, slots=True)
class VerificationEvidenceLink:
    id: str
    execution_id: str
    url: str
    added_by_user_id: str
    label: str | None = None
    provenance: str = "external_https"
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("id", "execution_id", "added_by_user_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} requires a stable identifier")
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "url", normalize_https_evidence_url(self.url))
        object.__setattr__(self, "label", str(self.label or "").strip() or None)
        provenance = str(self.provenance or "").strip()
        if not provenance:
            raise ValueError("Verification evidence provenance is required")
        object.__setattr__(self, "provenance", provenance)


def project_requirement_status(
    *,
    current_revision_id: str,
    executions: Sequence[VerificationExecution],
    retest_after_sequence: int = 0,
) -> VerificationProjectedStatus:
    revision_id = str(current_revision_id or "").strip()
    if not revision_id:
        raise ValueError("current_revision_id is required")
    threshold = int(retest_after_sequence)
    if threshold < 0:
        raise ValueError("retest_after_sequence cannot be negative")
    applicable = [
        execution
        for execution in executions
        if execution.revision_id == revision_id
        and execution.sequence > threshold
    ]
    if not applicable:
        return VerificationProjectedStatus.NOT_RUN
    latest = max(applicable, key=lambda execution: execution.sequence)
    return VerificationProjectedStatus(latest.result.value)
