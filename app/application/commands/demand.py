from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from ...domain.request_lines import (
    RequestLinePolicyError,
    default_legacy_hours,
    normalize_request_line,
)
from ..errors import ApplicationValidationError
from .common import (
    UNSET,
    UnsetType,
    date_value,
    float_value,
    int_value,
    optional_text,
    required_text,
    text,
    validate_date_window,
)


@dataclass(frozen=True, slots=True)
class DemandLineInput:
    line_id: str | None = None
    position: int = 0
    kind: str = "WORKFORCE"
    required_resource_class: str | None = None
    required_competency_ids: tuple[str, ...] = ()
    required_competencies: str | None = None
    desired_start: date | None = None
    desired_end: date | None = None
    desired_active_days: int | None = None
    estimated_hours: float | None = None
    work_package_ref: str | None = None
    task_code: str | None = None
    proposed_resource_id: str | None = None
    confirmation: str = "Confirmée"
    description: str | None = None
    asset_type_id: str | None = None
    proposed_asset_id: str | None = None

    def to_repository_values(self, *, require_complete: bool) -> dict[str, object]:
        try:
            normalized = normalize_request_line(
                line_id=self.line_id,
                position=self.position,
                kind=self.kind,
                required_resource_class=self.required_resource_class,
                required_competency_ids=self.required_competency_ids,
                required_competencies=self.required_competencies,
                desired_start=self.desired_start,
                desired_end=self.desired_end,
                desired_active_days=self.desired_active_days,
                estimated_hours=self.estimated_hours,
                work_package_ref=self.work_package_ref,
                task_code=self.task_code,
                proposed_resource_id=self.proposed_resource_id,
                confirmation=self.confirmation,
                description=self.description,
                require_complete=require_complete,
                asset_type_id=self.asset_type_id,
                proposed_asset_id=self.proposed_asset_id,
            )
        except RequestLinePolicyError as exc:
            raise ApplicationValidationError(
                str(exc),
                code=exc.code,
                context=exc.context,
            ) from exc
        return normalized.to_repository_values()


@dataclass(frozen=True, slots=True)
class DemandCreateCommand:
    project_number: str
    desired_start: date | None = None
    submit: bool = False
    project_name: str = ""
    client: str = ""
    project_manager: str = ""
    requester_user_id: str | None = None
    requester: str | None = None
    work_package_ref: str | None = None
    task_code: str | None = None
    request_type: str = "Projet"
    priority: str = "Normale"
    confirmation: str = "Confirmée"
    desired_end: date | None = None
    description: str = ""
    site_client: str = ""
    location: str = ""
    resource_count: int = 1
    required_competencies: str | None = None
    required_resource_class: str | None = None
    estimated_hours: float | None = None
    estimated_days: float | None = None
    proposed_technician: str | None = None
    lines: tuple[DemandLineInput, ...] | None = None

    def __post_init__(self) -> None:
        required_text(
            self.project_number,
            field="demand_project",
            message="Le projet est requis.",
        )
        if self.lines is None:
            if self.desired_start is None:
                raise ApplicationValidationError(
                    "La date de début est requise pour une demande sans lignes.",
                    code="demand_start_required",
                    context={"field": "desired_start"},
                )
            validate_date_window(self.desired_start, self.desired_end, prefix="demand")
        elif not self.lines:
            raise ApplicationValidationError(
                "Une demande multi-lignes doit contenir au moins une ligne.",
                code="demand_lines_required",
            )
        else:
            mixed = []
            if self.desired_start is not None:
                mixed.append("desired_start")
            if self.desired_end is not None:
                mixed.append("desired_end")
            if self.work_package_ref is not None:
                mixed.append("work_package_ref")
            if self.task_code is not None:
                mixed.append("task_code")
            if self.confirmation != "Confirmée":
                mixed.append("confirmation")
            if self.resource_count != 1:
                mixed.append("resource_count")
            if self.required_competencies is not None:
                mixed.append("required_competencies")
            if self.required_resource_class is not None:
                mixed.append("required_resource_class")
            if self.estimated_hours is not None:
                mixed.append("estimated_hours")
            if self.estimated_days is not None:
                mixed.append("estimated_days")
            if self.proposed_technician is not None:
                mixed.append("proposed_technician")
            if mixed:
                raise ApplicationValidationError(
                    "Les champs de besoin plats ne peuvent pas être combinés avec lines.",
                    code="demand_lines_mixed_contract",
                    context={"fields": tuple(sorted(mixed))},
                )
        if self.resource_count < 1:
            raise ApplicationValidationError(
                "Le nombre de ressources doit être au moins 1.",
                code="demand_resource_count_invalid",
                context={"field": "resource_count", "value": self.resource_count},
            )
        for field, value in (
            ("estimated_hours", self.estimated_hours),
            ("estimated_days", self.estimated_days),
        ):
            if value is not None and value <= 0:
                raise ApplicationValidationError(
                    f"{field} doit être supérieur à zéro lorsqu'il est renseigné.",
                    code=f"demand_{field}_invalid",
                    context={"field": field, "value": value},
                )
        if self.lines is None:
            if (
                self.estimated_hours is not None
                and float(self.estimated_hours) < int(self.resource_count) * 0.01
            ):
                raise ApplicationValidationError(
                    "Les heures totales sont insuffisantes pour produire un besoin positif par ressource.",
                    code="demand_hours_split_invalid",
                    context={
                        "estimated_hours": float(self.estimated_hours),
                        "resource_count": int(self.resource_count),
                    },
                )
            if (
                self.submit
                and self.estimated_hours is None
                and self.estimated_days is None
            ):
                raise ApplicationValidationError(
                    "Une demande soumise doit préciser des heures ou un nombre de jours.",
                    code="demand_effort_required",
                )

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
        *,
        submit: bool = False,
    ) -> "DemandCreateCommand":
        start = date_value(
            values.get("DateDebutSouhaitee"), field="demand_start", required=True
        )
        return cls(
            project_number=required_text(
                values.get("NumeroProjet"),
                field="demand_project",
                message="Le projet est requis.",
            ),
            desired_start=start,  # type: ignore[arg-type]
            submit=bool(submit),
            project_name=text(values.get("NomProjet")),
            client=text(values.get("Client")),
            project_manager=text(values.get("ChargeProjet")),
            requester=optional_text(values.get("Demandeur")),
            work_package_ref=optional_text(values.get("SourceEffortID")),
            task_code=optional_text(values.get("TaskCode")),
            request_type=text(values.get("TypeDemande")) or "Projet",
            priority=text(values.get("Priorite")) or "Normale",
            confirmation=text(values.get("Confirmation")) or "Confirmée",
            desired_end=date_value(
                values.get("DateFinSouhaitee"), field="demand_end", required=False
            ),
            description=text(values.get("Description")),
            site_client=text(values.get("SiteClient")),
            location=text(values.get("Lieu")),
            resource_count=int_value(
                values.get("NombreRessources"),
                field="demand_resource_count",
                default=1,
                minimum=1,
            )
            or 1,
            required_competencies=optional_text(values.get("CompetencesRequises")),
            estimated_hours=float_value(
                values.get("TempsEstimeHeures"),
                field="demand_estimated_hours",
                minimum=0,
            ),
            estimated_days=float_value(
                values.get("TempsEstimeJours"),
                field="demand_estimated_days",
                minimum=0,
            ),
            proposed_technician=optional_text(values.get("TechnicienPropose")),
        )

    def to_repository_values(self) -> dict[str, Any]:
        resolved_hours = default_legacy_hours(
            estimated_hours=self.estimated_hours,
            estimated_days=self.estimated_days,
            resource_count=self.resource_count,
        )
        legacy_hours_source = (
            "EXPLICIT"
            if self.estimated_hours is not None
            else (
                "DEFAULT_8H"
                if resolved_hours is not None and self.estimated_days is not None
                else None
            )
        )
        values: dict[str, Any] = {
            "NumeroProjet": text(self.project_number),
            "NomProjet": text(self.project_name),
            "Client": text(self.client),
            "ChargeProjet": text(self.project_manager),
            "RequesterUserId": self.requester_user_id,
            "Demandeur": self.requester,
            "SourceEffortID": self.work_package_ref,
            "TaskCode": self.task_code,
            "TypeDemande": text(self.request_type) or "Projet",
            "Priorite": text(self.priority) or "Normale",
            "Confirmation": text(self.confirmation) or "Confirmée",
            "DateDebutSouhaitee": self.desired_start,
            "DateFinSouhaitee": self.desired_end,
            "Description": text(self.description),
            "SiteClient": text(self.site_client),
            "Lieu": text(self.location),
            "NombreRessources": int(self.resource_count),
            "CompetencesRequises": self.required_competencies,
            "RequiredResourceClass": optional_text(self.required_resource_class),
            "TempsEstimeHeures": resolved_hours,
            "TempsEstimeJours": self.estimated_days,
            "TechnicienPropose": self.proposed_technician,
            "RequestLineHoursSource": legacy_hours_source,
        }
        if self.lines is not None:
            values["RequestLines"] = tuple(
                line.to_repository_values(require_complete=bool(self.submit))
                for line in self.lines
            )
        return values


@dataclass(frozen=True, slots=True)
class DemandUpdateCommand:
    number: str
    comment: str = "Demande modifiée dans l'application"
    project_number: str | None | UnsetType = UNSET
    project_name: str | None | UnsetType = UNSET
    client: str | None | UnsetType = UNSET
    project_manager: str | None | UnsetType = UNSET
    requester_user_id: str | None | UnsetType = UNSET
    requester: str | None | UnsetType = UNSET
    work_package_ref: str | None | UnsetType = UNSET
    task_code: str | None | UnsetType = UNSET
    request_type: str | None | UnsetType = UNSET
    priority: str | None | UnsetType = UNSET
    confirmation: str | None | UnsetType = UNSET
    desired_start: date | None | UnsetType = UNSET
    desired_end: date | None | UnsetType = UNSET
    description: str | None | UnsetType = UNSET
    site_client: str | None | UnsetType = UNSET
    location: str | None | UnsetType = UNSET
    resource_count: int | None | UnsetType = UNSET
    required_competencies: str | None | UnsetType = UNSET
    required_resource_class: str | None | UnsetType = UNSET
    estimated_hours: float | None | UnsetType = UNSET
    estimated_days: float | None | UnsetType = UNSET
    proposed_technician: str | None | UnsetType = UNSET
    lines: tuple[DemandLineInput, ...] | UnsetType = UNSET
    expected_version: int | UnsetType = UNSET

    def __post_init__(self) -> None:
        required_text(
            self.number,
            field="demand_number",
            message="Le numéro de demande est requis.",
        )
        if isinstance(self.desired_start, date) and isinstance(self.desired_end, date):
            validate_date_window(self.desired_start, self.desired_end, prefix="demand")
        if isinstance(self.expected_version, int) and self.expected_version < 1:
            raise ApplicationValidationError(
                "La version attendue doit être au moins 1.",
                code="demand_version_invalid",
                context={"field": "expected_version", "value": self.expected_version},
            )
        if isinstance(self.lines, tuple) and not self.lines:
            raise ApplicationValidationError(
                "Une demande multi-lignes doit contenir au moins une ligne.",
                code="demand_lines_required",
            )
        if isinstance(self.lines, tuple):
            flat_fields = {
                "work_package_ref": self.work_package_ref,
                "task_code": self.task_code,
                "confirmation": self.confirmation,
                "desired_start": self.desired_start,
                "desired_end": self.desired_end,
                "resource_count": self.resource_count,
                "required_competencies": self.required_competencies,
                "required_resource_class": self.required_resource_class,
                "estimated_hours": self.estimated_hours,
                "estimated_days": self.estimated_days,
                "proposed_technician": self.proposed_technician,
            }
            mixed = tuple(
                sorted(
                    field
                    for field, value in flat_fields.items()
                    if value is not UNSET
                )
            )
            if mixed:
                raise ApplicationValidationError(
                    "Les champs de besoin plats ne peuvent pas être combinés avec lines.",
                    code="demand_lines_mixed_contract",
                    context={"fields": mixed},
                )
        if isinstance(self.resource_count, int) and self.resource_count < 1:
            raise ApplicationValidationError(
                "Le nombre de ressources doit être au moins 1.",
                code="demand_resource_count_invalid",
                context={"field": "resource_count", "value": self.resource_count},
            )

    @classmethod
    def from_mapping(
        cls,
        number: str,
        updates: Mapping[str, Any],
        *,
        comment: str = "Demande modifiée dans l'application",
    ) -> "DemandUpdateCommand":
        known = {
            "NumeroProjet",
            "NomProjet",
            "Client",
            "ChargeProjet",
            "Demandeur",
            "SourceEffortID",
            "TaskCode",
            "TypeDemande",
            "Priorite",
            "Confirmation",
            "DateDebutSouhaitee",
            "DateFinSouhaitee",
            "Description",
            "SiteClient",
            "Lieu",
            "NombreRessources",
            "CompetencesRequises",
            "RequiredResourceClass",
            "TempsEstimeHeures",
            "TempsEstimeJours",
            "TechnicienPropose",
        }
        unknown = sorted(set(updates) - known)
        if unknown:
            raise ApplicationValidationError(
                "La modification contient des champs non autorisés.",
                code="demand_update_fields_invalid",
                context={"fields": unknown},
            )

        def present(key: str, converter: Any) -> Any:
            return converter(updates[key]) if key in updates else UNSET

        return cls(
            number=required_text(
                number,
                field="demand_number",
                message="Le numéro de demande est requis.",
            ),
            comment=str(comment or ""),
            project_number=present("NumeroProjet", optional_text),
            project_name=present("NomProjet", optional_text),
            client=present("Client", optional_text),
            project_manager=present("ChargeProjet", optional_text),
            requester=present("Demandeur", optional_text),
            work_package_ref=present("SourceEffortID", optional_text),
            task_code=present("TaskCode", optional_text),
            request_type=present("TypeDemande", optional_text),
            priority=present("Priorite", optional_text),
            confirmation=present("Confirmation", optional_text),
            desired_start=(
                date_value(updates["DateDebutSouhaitee"], field="demand_start", required=False)
                if "DateDebutSouhaitee" in updates
                else UNSET
            ),
            desired_end=(
                date_value(updates["DateFinSouhaitee"], field="demand_end", required=False)
                if "DateFinSouhaitee" in updates
                else UNSET
            ),
            description=present("Description", optional_text),
            site_client=present("SiteClient", optional_text),
            location=present("Lieu", optional_text),
            resource_count=(
                int_value(
                    updates["NombreRessources"],
                    field="demand_resource_count",
                    minimum=1,
                )
                if "NombreRessources" in updates
                else UNSET
            ),
            required_competencies=present("CompetencesRequises", optional_text),
            required_resource_class=present("RequiredResourceClass", optional_text),
            estimated_hours=(
                float_value(
                    updates["TempsEstimeHeures"],
                    field="demand_estimated_hours",
                    minimum=0,
                )
                if "TempsEstimeHeures" in updates
                else UNSET
            ),
            estimated_days=(
                float_value(
                    updates["TempsEstimeJours"],
                    field="demand_estimated_days",
                    minimum=0,
                )
                if "TempsEstimeJours" in updates
                else UNSET
            ),
            proposed_technician=present("TechnicienPropose", optional_text),
        )

    def to_repository_values(self) -> dict[str, Any]:
        values = {
            "NumeroProjet": self.project_number,
            "NomProjet": self.project_name,
            "Client": self.client,
            "ChargeProjet": self.project_manager,
            "RequesterUserId": self.requester_user_id,
            "Demandeur": self.requester,
            "SourceEffortID": self.work_package_ref,
            "TaskCode": self.task_code,
            "TypeDemande": self.request_type,
            "Priorite": self.priority,
            "Confirmation": self.confirmation,
            "DateDebutSouhaitee": self.desired_start,
            "DateFinSouhaitee": self.desired_end,
            "Description": self.description,
            "SiteClient": self.site_client,
            "Lieu": self.location,
            "NombreRessources": self.resource_count,
            "CompetencesRequises": self.required_competencies,
            "RequiredResourceClass": self.required_resource_class,
            "TempsEstimeHeures": self.estimated_hours,
            "TempsEstimeJours": self.estimated_days,
            "TechnicienPropose": self.proposed_technician,
        }
        if self.lines is not UNSET:
            values["RequestLines"] = tuple(
                line.to_repository_values(require_complete=False)
                for line in self.lines
            )
        if self.expected_version is not UNSET:
            values["ExpectedVersion"] = self.expected_version
        return {key: value for key, value in values.items() if value is not UNSET}


@dataclass(frozen=True, slots=True)
class DemandSubmitCommand:
    number: str
    expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class DemandApproveCommand:
    number: str
    comment: str = ""
    expected_version: int | None = None
    expected_planning_version: int | None = None

    def __post_init__(self) -> None:
        if self.expected_planning_version is not None and int(self.expected_planning_version) < 1:
            raise ApplicationValidationError(
                "La version attendue du planning doit être au moins 1.",
                code="planning_version_invalid",
                context={"expected_planning_version": self.expected_planning_version},
            )


@dataclass(frozen=True, slots=True)
class DemandCorrectionCommand:
    number: str
    comment: str
    expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class DemandCancelCommand:
    number: str
    expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class DemandCancellationRequestCommand:
    number: str
    reason: str
    expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class DemandCancellationRejectCommand:
    number: str
    cancellation_request_id: str
    comment: str
    expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class DemandCancellationAcceptCommand:
    number: str
    cancellation_request_id: str
    comment: str
    expected_version: int
    expected_planning_version: int
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if int(self.expected_version) < 1:
            raise ApplicationValidationError(
                "La version attendue de la demande doit être au moins 1.",
                code="demand_version_invalid",
                context={"expected_version": self.expected_version},
            )
        if int(self.expected_planning_version) < 1:
            raise ApplicationValidationError(
                "La version attendue du planning doit être au moins 1.",
                code="planning_version_invalid",
                context={"expected_planning_version": self.expected_planning_version},
            )
