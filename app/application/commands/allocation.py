from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ...domain.confirmation import normalize_confirmation
from ...domain.manual_overallocation import normalize_overallocation_policy
from ..errors import ApplicationValidationError
from ..idempotency import normalize_idempotency_key
from .common import date_value, float_value, required_text


def _confirmation(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        return normalize_confirmation(value)
    except ValueError as exc:
        raise ApplicationValidationError(
            str(exc),
            code="allocation_confirmation_invalid",
            context={"field": "confirmation", "value": value},
        ) from exc


def _overallocation_policy(value: object) -> str | None:
    try:
        return normalize_overallocation_policy(value)
    except ValueError as exc:
        raise ApplicationValidationError(
            str(exc),
            code="allocation_overallocation_policy_invalid",
            context={"field": "overallocation_policy", "value": value},
        ) from exc


@dataclass(frozen=True, slots=True)
class ManualAllocationCreateCommand:
    segment_id: str
    technician: str
    day: date
    hours: float
    outside_standard_hours: bool = False
    note: str = ""
    confirmation: str | None = None
    overallocation_policy: str | None = None
    resource_id: str | None = None

    def __post_init__(self) -> None:
        if self.confirmation is not None:
            _confirmation(self.confirmation)
        if self.overallocation_policy is not None:
            _overallocation_policy(self.overallocation_policy)

    @classmethod
    def from_values(
        cls,
        segment_id: object,
        technician: object,
        day_value: object,
        hours_value: object,
        outside_standard_hours: bool = False,
        note: str = "",
        confirmation: object = None,
        overallocation_policy: object = None,
    ) -> "ManualAllocationCreateCommand":
        return cls(
            segment_id=required_text(
                segment_id,
                field="allocation_segment",
                message="Un segment est requis pour le quart manuel.",
            ),
            technician=required_text(
                technician,
                field="allocation_resource",
                message="Un technicien est requis pour le quart manuel.",
            ),
            day=date_value(day_value, field="allocation_day", required=True),  # type: ignore[arg-type]
            hours=float_value(
                hours_value,
                field="allocation_hours",
                required=True,
                minimum=0.01,
            ),  # type: ignore[arg-type]
            outside_standard_hours=bool(outside_standard_hours),
            note=str(note or ""),
            confirmation=_confirmation(confirmation),
            overallocation_policy=_overallocation_policy(overallocation_policy),
        )


@dataclass(frozen=True, slots=True)
class ManualAllocationUpdateCommand:
    allocation_id: str
    technician: str
    day: date
    hours: float
    outside_standard_hours: bool = False
    note: str = ""
    confirmation: str | None = None
    clear_confirmation_override: bool = False
    overallocation_policy: str | None = None
    resource_id: str | None = None

    def __post_init__(self) -> None:
        if self.confirmation is not None:
            _confirmation(self.confirmation)
        if self.clear_confirmation_override and self.confirmation is not None:
            raise ApplicationValidationError(
                "Impossible de définir et supprimer l'override de confirmation simultanément.",
                code="allocation_confirmation_conflict",
                context={"field": "confirmation"},
            )
        if self.overallocation_policy is not None:
            _overallocation_policy(self.overallocation_policy)

    @classmethod
    def from_values(
        cls,
        allocation_id: object,
        technician: object,
        day_value: object,
        hours_value: object,
        outside_standard_hours: bool = False,
        note: str = "",
        confirmation: object = None,
        overallocation_policy: object = None,
    ) -> "ManualAllocationUpdateCommand":
        return cls(
            allocation_id=required_text(
                allocation_id,
                field="allocation_id",
                message="Un identifiant d'allocation est requis.",
            ),
            technician=required_text(
                technician,
                field="allocation_resource",
                message="Un technicien est requis pour le quart manuel.",
            ),
            day=date_value(day_value, field="allocation_day", required=True),  # type: ignore[arg-type]
            hours=float_value(
                hours_value,
                field="allocation_hours",
                required=True,
                minimum=0.01,
            ),  # type: ignore[arg-type]
            outside_standard_hours=bool(outside_standard_hours),
            note=str(note or ""),
            confirmation=_confirmation(confirmation),
            # Compatibility callers historically use None to mean "do not touch".
            clear_confirmation_override=False,
            overallocation_policy=_overallocation_policy(overallocation_policy),
        )


@dataclass(frozen=True, slots=True)
class ManualAllocationMoveCommand:
    allocation_id: str
    technician: str
    day: date
    resource_id: str | None = None
    outside_standard_hours: bool = False
    expected_planning_version: int | None = None


@dataclass(frozen=True, slots=True)
class ManualAllocationReleaseCommand:
    allocation_id: str
    expected_planning_version: int | None = None


@dataclass(frozen=True, slots=True)
class ManualAllocationDeleteCommand:
    allocation_id: str
    expected_planning_version: int | None = None


@dataclass(frozen=True, slots=True)
class SegmentAssignCommand:
    segment_id: str
    technician: str
    resource_id: str | None = None


@dataclass(frozen=True, slots=True)
class AllocationSplitCommand:
    allocation_id: str
    resource_id: str
    day: date
    transfer_hours: float
    expected_planning_version: int
    outside_standard_hours: bool | None = None
    overallocation_policy: str | None = None
    expected_approval_revision_id: str | None = None
    expected_operational_version: int | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if float(self.transfer_hours) <= 0:
            raise ApplicationValidationError(
                "Les heures transférées doivent être supérieures à zéro.",
                code="allocation_split_hours_invalid",
                context={"transfer_hours": self.transfer_hours},
            )
        if int(self.expected_planning_version) < 1:
            raise ApplicationValidationError(
                "La version attendue du planning doit être au moins 1.",
                code="planning_version_invalid",
                context={"expected_planning_version": self.expected_planning_version},
            )
        if self.expected_operational_version is not None and int(self.expected_operational_version) < 1:
            raise ApplicationValidationError(
                "La version opérationnelle attendue doit être au moins 1.",
                code="operational_choice_version_invalid",
            )
        if self.overallocation_policy is not None:
            _overallocation_policy(self.overallocation_policy)


@dataclass(frozen=True, slots=True)
class AllocationDuplicateCommand:
    allocation_id: str
    resource_id: str
    day: date
    expected_planning_version: int
    outside_standard_hours: bool | None = None
    overallocation_policy: str | None = None
    expected_approval_revision_id: str | None = None
    expected_operational_version: int | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if int(self.expected_planning_version) < 1:
            raise ApplicationValidationError(
                "La version attendue du planning doit être au moins 1.",
                code="planning_version_invalid",
                context={"expected_planning_version": self.expected_planning_version},
            )
        if self.expected_operational_version is not None and int(self.expected_operational_version) < 1:
            raise ApplicationValidationError(
                "La version opérationnelle attendue doit être au moins 1.",
                code="operational_choice_version_invalid",
            )
        if self.overallocation_policy is not None:
            _overallocation_policy(self.overallocation_policy)


@dataclass(frozen=True, slots=True)
class AllocationDropEvaluateCommand:
    allocation_id: str
    resource_id: str
    day: date
    outside_standard_hours: bool = False
    include_planning_window_override_options: bool = False

    def __post_init__(self) -> None:
        required_text(
            self.allocation_id,
            field="allocation_id",
            message="Un identifiant d'allocation est requis.",
        )
        required_text(
            self.resource_id,
            field="allocation_resource",
            message="Une ressource cible est requise.",
        )


@dataclass(frozen=True, slots=True)
class AllocationExtendMoveCommand:
    allocation_id: str
    resource_id: str
    day: date
    expected_planning_version: int
    confirm_window_extension: bool
    outside_standard_hours: bool = False
    overallocation_policy: str | None = None
    expected_approval_revision_id: str | None = None
    expected_operational_version: int | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        required_text(
            self.allocation_id,
            field="allocation_id",
            message="Un identifiant d'allocation est requis.",
        )
        required_text(
            self.resource_id,
            field="allocation_resource",
            message="Une ressource cible est requise.",
        )
        if int(self.expected_planning_version) < 1:
            raise ApplicationValidationError(
                "La version attendue du planning doit être au moins 1.",
                code="planning_version_invalid",
                context={"expected_planning_version": self.expected_planning_version},
            )
        if (
            self.expected_operational_version is not None
            and int(self.expected_operational_version) < 1
        ):
            raise ApplicationValidationError(
                "La version opérationnelle attendue doit être au moins 1.",
                code="operational_choice_version_invalid",
            )
        if self.overallocation_policy is not None:
            _overallocation_policy(self.overallocation_policy)


@dataclass(frozen=True, slots=True)
class PlanningWindowOverrideExtendCommand:
    segment_id: str
    start_date: date
    end_date: date
    reason: str
    expected_planning_version: int
    expected_approval_revision_id: str
    idempotency_key: str
    expected_operational_version: int | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        required_text(
            self.segment_id,
            field="segment_id",
            message="Un segment REQUEST est requis pour la dérogation de fenêtre.",
        )
        required_text(
            self.reason,
            field="planning_window_override_reason",
            message="Un motif est requis pour la dérogation de fenêtre Planning.",
        )
        required_text(
            self.expected_approval_revision_id,
            field="allocation_approval_revision",
            message="La révision approuvée attendue est requise.",
        )
        normalized_key = normalize_idempotency_key(self.idempotency_key)
        if normalized_key is None:
            raise ApplicationValidationError(
                "Une clé d'idempotence est requise.",
                code="idempotency_key_invalid",
            )
        object.__setattr__(self, "idempotency_key", normalized_key)
        if self.end_date < self.start_date:
            raise ApplicationValidationError(
                "La fin de la fenêtre effective doit être postérieure ou égale au début.",
                code="planning_window_override_invalid",
            )
        if int(self.expected_planning_version) < 1:
            raise ApplicationValidationError(
                "La version attendue du planning doit être au moins 1.",
                code="planning_version_invalid",
            )
        if (
            self.expected_operational_version is not None
            and int(self.expected_operational_version) < 1
        ):
            raise ApplicationValidationError(
                "La version opérationnelle attendue doit être au moins 1.",
                code="operational_choice_version_invalid",
            )


@dataclass(frozen=True, slots=True)
class AllocationWindowOverrideMoveCommand:
    allocation_id: str
    resource_id: str
    day: date
    reason: str
    expected_planning_version: int
    expected_approval_revision_id: str
    idempotency_key: str
    outside_standard_hours: bool = False
    overallocation_policy: str | None = None
    expected_operational_version: int | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        required_text(
            self.allocation_id,
            field="allocation_id",
            message="Un identifiant d'allocation est requis.",
        )
        required_text(
            self.resource_id,
            field="allocation_resource",
            message="Une ressource cible est requise.",
        )
        required_text(
            self.reason,
            field="planning_window_override_reason",
            message="Un motif est requis pour la dérogation de fenêtre Planning.",
        )
        required_text(
            self.expected_approval_revision_id,
            field="allocation_approval_revision",
            message="La révision approuvée attendue est requise.",
        )
        normalized_key = normalize_idempotency_key(self.idempotency_key)
        if normalized_key is None:
            raise ApplicationValidationError(
                "Une clé d'idempotence est requise.",
                code="idempotency_key_invalid",
            )
        object.__setattr__(self, "idempotency_key", normalized_key)
        if int(self.expected_planning_version) < 1:
            raise ApplicationValidationError(
                "La version attendue du planning doit être au moins 1.",
                code="planning_version_invalid",
            )
        if (
            self.expected_operational_version is not None
            and int(self.expected_operational_version) < 1
        ):
            raise ApplicationValidationError(
                "La version opérationnelle attendue doit être au moins 1.",
                code="operational_choice_version_invalid",
            )
        if self.overallocation_policy is not None:
            _overallocation_policy(self.overallocation_policy)


@dataclass(frozen=True, slots=True)
class AllocationWindowExtensionProposalCommand:
    allocation_id: str
    resource_id: str
    day: date
    expected_request_version: int
    expected_approval_revision_id: str
    outside_standard_hours: bool = False
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        required_text(
            self.allocation_id,
            field="allocation_id",
            message="Un identifiant d'allocation est requis.",
        )
        required_text(
            self.resource_id,
            field="allocation_resource",
            message="Une ressource cible est requise.",
        )
        required_text(
            self.expected_approval_revision_id,
            field="allocation_approval_revision",
            message="La révision approuvée attendue est requise.",
        )
        if int(self.expected_request_version) < 1:
            raise ApplicationValidationError(
                "La version candidate attendue doit être au moins 1.",
                code="demand_version_invalid",
                context={"expected_request_version": self.expected_request_version},
            )
