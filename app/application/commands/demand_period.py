from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ...domain.demand_periods import DemandPeriodDefinition
from ..errors import ApplicationValidationError


def _required(value: object, *, field: str, message: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ApplicationValidationError(message, code=f"{field}_required", context={"field": field})
    return text


@dataclass(frozen=True, slots=True)
class DemandPeriodInput:
    period_id: str
    start_date: date
    end_date: date
    hours: float | None
    kind: str = "CUMULATIVE"
    alternative_group: str | None = None
    confirmation: str = "Tentative"
    confirmation_mode: str | None = None
    proposed_resource: str | None = None
    proposed_resource_mode: str | None = None
    same_as_period_id: str | None = None
    resource_count: int = 1
    desired_active_days: int | None = None
    note: str = ""

    def to_definition(self) -> DemandPeriodDefinition:
        return DemandPeriodDefinition(
            period_id=self.period_id,
            start_date=self.start_date,
            end_date=self.end_date,
            hours=self.hours,
            kind=self.kind,
            alternative_group=self.alternative_group,
            confirmation=self.confirmation,
            confirmation_mode=self.confirmation_mode,
            proposed_resource=self.proposed_resource,
            proposed_resource_mode=self.proposed_resource_mode,
            same_as_period_id=self.same_as_period_id,
            resource_count=self.resource_count,
            desired_active_days=self.desired_active_days,
            note=self.note,
        )


@dataclass(frozen=True, slots=True)
class DemandPeriodsReplaceCommand:
    number: str
    periods: tuple[DemandPeriodInput, ...]
    request_line_id: str | None = None
    expected_request_version: int | None = None

    def __post_init__(self) -> None:
        _required(self.number, field="demand_number", message="Le numéro de demande est requis.")
        if self.expected_request_version is not None and int(self.expected_request_version) < 1:
            raise ApplicationValidationError(
                "La version candidate attendue doit être au moins 1.",
                code="demand_version_invalid",
                context={"expected_request_version": self.expected_request_version},
            )


@dataclass(frozen=True, slots=True)
class DemandAlternativeSelectCommand:
    number: str
    alternative_group: str
    period_id: str
    request_line_id: str | None = None
    expected_operational_version: int | None = None
    operational: bool = False

    def __post_init__(self) -> None:
        _required(self.number, field="demand_number", message="Le numéro de demande est requis.")
        _required(
            self.alternative_group,
            field="alternative_group",
            message="Le groupe alternatif est requis.",
        )
        _required(self.period_id, field="period_id", message="La période sélectionnée est requise.")
        if self.operational and self.expected_operational_version is None:
            raise ApplicationValidationError(
                "La version opérationnelle attendue est requise.",
                code="operational_choice_expected_version_required",
                context={"field": "expected_operational_version"},
            )


@dataclass(frozen=True, slots=True)
class DemandOperationalConfirmationCommand:
    number: str
    confirmation: str
    request_line_id: str | None = None
    period_id: str | None = None
    expected_operational_version: int | None = None

    def __post_init__(self) -> None:
        _required(
            self.number,
            field="demand_number",
            message="Le numéro de demande est requis.",
        )
        _required(
            self.confirmation,
            field="confirmation",
            message="La confirmation est requise.",
        )
        if self.expected_operational_version is None:
            raise ApplicationValidationError(
                "La version opérationnelle attendue est requise.",
                code="operational_choice_expected_version_required",
                context={"field": "expected_operational_version"},
            )
