from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
import json
import re
from typing import Any

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import Session

from ...application.read_models import SegmentReadModel
from ...application.repository_ports import SegmentRepositoryPort
from ...domain.confirmation import CONFIRMATION_CONFIRMED, normalize_confirmation
from ...domain.effective_need import resolve_effective_slot_need
from ...domain.planning_engine import MISSING_ALLOCATION_TYPE
from ...domain.load_profiles import normalize_load_profile
from ...domain.operational_contacts import (
    PROVENANCE_OPERATIONAL_CAPTURE,
    RESPONSIBILITY_CONTEXT_VERSION,
    resolve_operational_responsible,
)
from .demand_period_models import WorkforceRequestPeriod, WorkforceRequestPeriodRequirement
from .segment_asset_guard import assert_segment_asset_mutation_compatible
from .operational_contact_repository import SqlOperationalContactRepository
from .planning_intent_repository import SqlRequestRequirementPlanningIntentRepository
from .operational_choice_models import (
    PLANNING_INTENT_PRESERVE_EXISTING,
    RequestCompletionReduction,
    RequestRequirementPlanningIntent,
)
from .models import (
    ORIGIN_REQUEST,
    Project,
    RequestLine,
    RequestLineCompetency,
    Resource,
    ResourceRequirement,
    ResourceRequirementCompetency,
    Shift,
    WorkforceRequest,
    WorkPackage,
)


_SEGMENT_ID_RE = re.compile(r"^SEG-(\d{4})-(\d+)$", re.IGNORECASE)


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    value_text = _text(value)
    return value_text or None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value).replace(",", "."))


def _optional_positive_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    numeric = float(value)
    if numeric <= 0 or not numeric.is_integer():
        raise ValueError("Les jours actifs cibles doivent être un entier positif.")
    return int(numeric)


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "oui", "on"}


class SqlSegmentRepository(SegmentRepositoryPort):
    """SQLAlchemy implementation of the ResourceRequirement/segment port."""

    def __init__(self, session: Session, *, actor_name: str = "") -> None:
        self._session = session
        self._actor_name = _text(actor_name)

    def _row_query(self):
        zero_effective = (
            select(func.count())
            .select_from(RequestRequirementPlanningIntent)
            .join(
                RequestCompletionReduction,
                RequestCompletionReduction.planning_intent_id
                == RequestRequirementPlanningIntent.id,
            )
            .where(
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == ResourceRequirement.id,
                RequestCompletionReduction.reconciliation_state == "ACTIVE",
                RequestCompletionReduction.effective_need_after <= 0,
                RequestCompletionReduction.effective_need_before
                == ResourceRequirement.planned_hours,
            )
            .correlate(ResourceRequirement)
            .scalar_subquery()
        )
        effective_need = case(
            (zero_effective > 0, Decimal("0.00")),
            else_=ResourceRequirement.planned_hours,
        )
        return (
            select(
                ResourceRequirement,
                Project,
                WorkforceRequest,
                Resource,
                effective_need.label("effective_need"),
            )
            .join(Project, ResourceRequirement.project_id == Project.id)
            .outerjoin(
                WorkforceRequest,
                ResourceRequirement.workforce_request_id == WorkforceRequest.id,
            )
            .outerjoin(Resource, ResourceRequirement.assigned_resource_id == Resource.id)
        )

    def _active_reduction_hours(
        self,
        requirement: ResourceRequirement,
    ) -> tuple[Decimal, ...]:
        intent = self._session.scalar(
            select(RequestRequirementPlanningIntent).where(
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == requirement.id
            )
        )
        if intent is None:
            return ()
        return tuple(
            Decimal(str(value)).quantize(Decimal("0.01"))
            for value in self._session.scalars(
                select(RequestCompletionReduction.reduction_hours).where(
                    RequestCompletionReduction.planning_intent_id == intent.id,
                    RequestCompletionReduction.reconciliation_state == "ACTIVE",
                )
            ).all()
        )

    def _operational_base_hours(
        self,
        requirement: ResourceRequirement,
        *,
        effective_need: Decimal,
    ) -> Decimal:
        reductions = self._active_reduction_hours(requirement)
        if not reductions:
            return effective_need
        return (
            effective_need + sum(reductions, Decimal("0.00"))
        ).quantize(Decimal("0.01"))

    def _read_model(
        self,
        requirement: ResourceRequirement,
        project: Project,
        request: WorkforceRequest | None,
        resource: Resource | None,
        *,
        effective_need: Decimal,
    ) -> SegmentReadModel:
        operational_base_hours = self._operational_base_hours(
            requirement,
            effective_need=effective_need,
        )
        return SegmentReadModel(
            segment_id=_text(requirement.legacy_segment_id) or requirement.id,
            demand_number=(
                _text(request.legacy_demand_number) or request.id
                if request is not None
                else None
            ),
            project_number=_optional_text(project.number),
            project_name=_optional_text(project.name),
            resource_name=_optional_text(resource.name) if resource else None,
            requirement_id=requirement.id,
            automatic_target_resource_id=resource.id if resource else None,
            automatic_target_resource_name=(
                _optional_text(resource.name) if resource else None
            ),
            start_date=requirement.start_date,
            end_date=requirement.end_date,
            planned_hours=float(effective_need),
            operational_base_hours=float(operational_base_hours),
            status=_text(requirement.status),
            description=_optional_text(requirement.description),
            origin=_optional_text(requirement.origin),
            required_competency=_optional_text(requirement.required_competency),
            required_competency_id=_optional_text(requirement.required_competency_id),
            required_resource_class=_optional_text(requirement.required_resource_class),
            planning_type=_optional_text(requirement.planning_type),
            priority=_optional_text(requirement.priority),
            outside_standard_hours=bool(requirement.outside_standard_hours_allowed),
            confirmation=_optional_text(requirement.confirmation),
            confirmation_overridden=bool(requirement.confirmation_overridden),
            project_manager=_optional_text(project.project_manager_name),
            requester=(
                _optional_text(request.requester_name)
                if request is not None
                else _optional_text(requirement.created_by_name)
            ),
            desired_active_days=requirement.desired_active_days,
            load_profile=normalize_load_profile(requirement.load_profile),
        )

    def list(self, *, include_cancelled: bool = True) -> Sequence[SegmentReadModel]:
        statement = self._row_query()
        if not include_cancelled:
            statement = statement.where(ResourceRequirement.status != "Annulé")
        rows = self._session.execute(
            statement.order_by(
                ResourceRequirement.start_date,
                ResourceRequirement.legacy_segment_id,
                ResourceRequirement.id,
            )
        ).all()
        return tuple(
            self._read_model(
                requirement,
                project,
                request,
                resource,
                effective_need=Decimal(str(effective_need)).quantize(Decimal("0.01")),
            )
            for requirement, project, request, resource, effective_need in rows
        )

    def get(self, segment_id: str) -> SegmentReadModel | None:
        wanted = _text(segment_id)
        if not wanted:
            return None
        row = self._session.execute(
            self._row_query().where(
                (ResourceRequirement.legacy_segment_id == wanted)
                | (ResourceRequirement.id == wanted)
            )
        ).one_or_none()
        if row is None:
            return None
        requirement, project, request, resource, effective_need = row
        return self._read_model(
            requirement,
            project,
            request,
            resource,
            effective_need=Decimal(str(effective_need)).quantize(Decimal("0.01")),
        )

    def _requirement(self, segment_id: str) -> ResourceRequirement:
        wanted = _text(segment_id)
        requirement = self._session.scalar(
            select(ResourceRequirement).where(
                (ResourceRequirement.legacy_segment_id == wanted)
                | (ResourceRequirement.id == wanted)
            )
        )
        if requirement is None:
            raise KeyError(f"Segment {wanted} introuvable")
        return requirement

    def _project(self, number: object) -> Project:
        project_number = _text(number)
        project = self._session.scalar(
            select(Project).where(Project.number == project_number)
        )
        if project is None:
            raise KeyError(f"Projet {project_number} introuvable")
        return project

    def _request(self, number: object) -> WorkforceRequest | None:
        request_number = _text(number)
        if not request_number:
            return None
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == request_number)
                | (WorkforceRequest.id == request_number)
            )
        )
        if request is None:
            raise KeyError(f"Demande {request_number} introuvable")
        return request

    def _resource(self, name: object) -> Resource | None:
        resource_name = _text(name)
        if not resource_name:
            return None
        resource = self._session.scalar(select(Resource).where(Resource.name == resource_name))
        if resource is None:
            raise KeyError(f"Ressource {resource_name} introuvable")
        return resource

    def _inherited_confirmation(
        self,
        requirement: ResourceRequirement,
        request: WorkforceRequest | None,
    ) -> str:
        link = self._session.get(WorkforceRequestPeriodRequirement, requirement.id)
        if link is not None:
            period = self._session.get(WorkforceRequestPeriod, link.period_id)
            if period is not None:
                return normalize_confirmation(
                    period.confirmation,
                    default=CONFIRMATION_CONFIRMED,
                )
        if request is not None:
            return normalize_confirmation(
                request.confirmation,
                default=CONFIRMATION_CONFIRMED,
            )
        return CONFIRMATION_CONFIRMED

    def _next_segment_id(self) -> str:
        year = date.today().year
        prefix = f"SEG-{year}-"
        identifiers = self._session.scalars(
            select(ResourceRequirement.legacy_segment_id).where(
                ResourceRequirement.legacy_segment_id.like(f"{prefix}%")
            )
        ).all()
        max_sequence = 0
        for identifier in identifiers:
            match = _SEGMENT_ID_RE.match(_text(identifier))
            if match and int(match.group(1)) == year:
                max_sequence = max(max_sequence, int(match.group(2)))
        return f"SEG-{year}-{max_sequence + 1:04d}"

    def _resolve_project_and_request(
        self,
        *,
        project_number: object,
        demand_number: object,
    ) -> tuple[Project, WorkforceRequest | None]:
        request = self._request(demand_number)
        supplied_project = _text(project_number)
        if supplied_project:
            project = self._project(supplied_project)
        elif request is not None:
            project = self._session.get(Project, request.project_id)
            if project is None:
                raise KeyError(f"Projet {request.project_id} introuvable")
        else:
            raise KeyError("Un projet est requis pour le segment")

        if request is not None and request.project_id != project.id:
            raise ValueError(
                "Le projet du segment ne correspond pas au projet de la demande."
            )
        return project, request

    def _replace_requirement_competencies(
        self,
        requirement: ResourceRequirement,
        competency_ids: Sequence[str],
    ) -> None:
        self._session.execute(
            delete(ResourceRequirementCompetency).where(
                ResourceRequirementCompetency.resource_requirement_id == requirement.id
            )
        )
        for competency_id in dict.fromkeys(
            _text(value) for value in competency_ids if _text(value)
        ):
            self._session.add(
                ResourceRequirementCompetency(
                    resource_requirement_id=requirement.id,
                    competency_id=competency_id,
                )
            )

    def _request_line(
        self,
        request: WorkforceRequest | None,
        line_id: object,
    ) -> RequestLine | None:
        wanted = _optional_text(line_id)
        if wanted is None:
            return None
        if request is None:
            raise ValueError("Une provenance RequestLine exige une demande.")
        line = self._session.get(RequestLine, wanted)
        if line is None or line.workforce_request_id != request.id:
            raise KeyError(f"Ligne {wanted} introuvable pour la demande.")
        return line

    def _line_competency_ids(self, line: RequestLine) -> tuple[str, ...]:
        return tuple(
            self._session.scalars(
                select(RequestLineCompetency.competency_id).where(
                    RequestLineCompetency.request_line_id == line.id
                )
            ).all()
        )

    def _attach_legacy_request_line(
        self,
        requirement: ResourceRequirement,
        request: WorkforceRequest | None,
    ) -> None:
        """Attach transitional line provenance without changing segment semantics."""

        if request is None:
            requirement.source_request_line_id = None
            self._replace_requirement_competencies(requirement, ())
            return
        line = self._session.get(RequestLine, request.id)
        if line is None:
            requirement.source_request_line_id = None
            self._replace_requirement_competencies(requirement, ())
            return
        requirement.source_request_line_id = line.id
        self._replace_requirement_competencies(
            requirement,
            self._line_competency_ids(line),
        )

    def _guard_work_package_dependency(
        self,
        reference: object,
        *,
        project_id: str,
    ) -> WorkPackage | None:
        wanted = _optional_text(reference)
        if wanted is None:
            return None
        work_package = self._session.scalar(
            select(WorkPackage).where(
                (WorkPackage.id == wanted)
                | (WorkPackage.legacy_effort_id == wanted)
            )
        )
        if work_package is None:
            return None
        result = self._session.execute(
            update(WorkPackage)
            .where(WorkPackage.id == work_package.id)
            .values(
                version=WorkPackage.version,
                updated_at=WorkPackage.updated_at,
            )
        )
        if int(result.rowcount or 0) != 1:
            raise KeyError(f"WorkPackage {wanted} introuvable")
        self._session.flush()
        self._session.refresh(work_package)
        if work_package.project_id != project_id:
            raise ValueError(
                "Le WorkPackage du segment n'appartient pas au projet sélectionné."
            )
        return work_package

    def create(self, values: Mapping[str, Any]) -> str:
        project, request = self._resolve_project_and_request(
            project_number=values.get("NumeroProjet"),
            demand_number=values.get("NoDemande"),
        )
        resource = self._resource(values.get("Technicien"))
        source_effort_ref = _optional_text(
            values.get("SourceEffortID") or values.get("SourceEffortRow")
        )
        work_package = self._guard_work_package_dependency(
            source_effort_ref,
            project_id=project.id,
        )
        identifier = self._next_segment_id()
        origin = _text(values.get("OrigineSegment")) or ORIGIN_REQUEST
        supplied_confirmation = _optional_text(values.get("Confirmation"))
        if supplied_confirmation is not None:
            confirmation = normalize_confirmation(supplied_confirmation)
            overridden = (
                _bool(values.get("ConfirmationOverride"))
                if "ConfirmationOverride" in values
                else request is not None
            )
        else:
            confirmation = normalize_confirmation(
                request.confirmation if request is not None else None,
                default=CONFIRMATION_CONFIRMED,
            )
            overridden = False

        requirement = ResourceRequirement(
            legacy_segment_id=identifier,
            project_id=project.id,
            workforce_request_id=request.id if request else None,
            assigned_resource_id=resource.id if resource else None,
            start_date=values.get("DateDebut"),
            end_date=values.get("DateFin") or values.get("DateDebut"),
            planned_hours=_decimal(values.get("HeuresPrevues")),
            desired_active_days=_optional_positive_int(values.get("JoursActifsCibles")),
            load_profile=normalize_load_profile(values.get("ProfilCharge")),
            status=_text(values.get("Statut")) or "Planifié",
            description=_optional_text(values.get("Description")),
            source_effort_id=source_effort_ref,
            required_resource_class=_optional_text(values.get("ClasseRessourceRequise")),
            required_competency=_optional_text(values.get("CompetenceRequise")),
            planning_type=_text(values.get("TypePlanification")) or "Flexible",
            priority=_text(values.get("Priorite")) or "Normale",
            outside_standard_hours_allowed=_bool(values.get("HorsHoraireAutorise")),
            confirmation=confirmation,
            confirmation_overridden=overridden,
            origin=origin,
            approved_contact_context_status=(
                "NOT_APPLICABLE" if request is None else "LEGACY_UNKNOWN"
            ),
            created_by_name=_optional_text(values.get("CreePar")) or self._actor_name or None,
        )
        self._session.add(requirement)
        self._session.flush()

        source_line = self._request_line(request, values.get("SourceRequestLineID"))
        if source_line is not None:
            requirement.source_request_line_id = source_line.id
            supplied_competencies = values.get("RequiredCompetencyIDs")
            competency_ids = (
                tuple(supplied_competencies)
                if supplied_competencies is not None
                else self._line_competency_ids(source_line)
            )
            self._replace_requirement_competencies(requirement, competency_ids)
        else:
            self._attach_legacy_request_line(requirement, request)

        if request is None:
            task_id = (
                work_package.task_catalog_item_id
                if work_package is not None
                else None
            )
            task_candidate, project_candidate, manager_candidate = (
                SqlOperationalContactRepository(
                    self._session
                ).get_project_operational_candidates(
                    project.id,
                    task_id=task_id,
                )
            )
            resolution = resolve_operational_responsible(
                task_responsible=task_candidate,
                project_override=project_candidate,
                project_manager=manager_candidate,
            )
            requirement.captured_operational_responsible_contact_id = (
                resolution.contact_id
            )
            requirement.captured_operational_responsible_source_type = (
                resolution.source_type
            )
            requirement.captured_operational_responsible_source_entity_id = (
                resolution.source_entity_id
            )
            requirement.captured_operational_responsible_status = resolution.status
            requirement.captured_operational_responsible_diagnostics = json.dumps(
                list(resolution.diagnostics),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            requirement.operational_responsibility_context_provenance = (
                PROVENANCE_OPERATIONAL_CAPTURE
            )
            requirement.operational_responsibility_context_version = (
                RESPONSIBILITY_CONTEXT_VERSION
            )

        self._session.flush()
        return identifier

    def planned_hours_overallocation_context(
        self,
        segment_id: str,
        proposed_base_hours: float,
    ) -> Mapping[str, Any] | None:
        requirement = self._requirement(segment_id)
        if requirement.origin != ORIGIN_REQUEST:
            return None
        intent = self._session.scalar(
            select(RequestRequirementPlanningIntent).where(
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == requirement.id
            )
        )
        if intent is None or intent.intent_mode != PLANNING_INTENT_PRESERVE_EXISTING:
            return None

        active_reductions = self._active_reduction_hours(requirement)
        current_effective_need = SqlRequestRequirementPlanningIntentRepository(
            self._session
        ).current_effective_need_for_requirement(
            requirement.id,
            fallback_hours=requirement.planned_hours,
        )
        projected_effective_need = resolve_effective_slot_need(
            Decimal(str(proposed_base_hours)).quantize(Decimal("0.01")),
            active_reductions,
        )
        retained_coverage = Decimal(
            str(
                self._session.scalar(
                    select(func.coalesce(func.sum(Shift.hours), 0)).where(
                        Shift.resource_requirement_id == requirement.id,
                        (Shift.allocation_type.is_(None))
                        | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
                    )
                )
                or 0
            )
        ).quantize(Decimal("0.01"))
        current_excess = max(
            retained_coverage - current_effective_need,
            Decimal("0.00"),
        )
        projected_excess = max(
            retained_coverage - projected_effective_need,
            Decimal("0.00"),
        )
        return {
            "segment_id": _text(requirement.legacy_segment_id) or requirement.id,
            "planned_hours": float(projected_effective_need),
            "current_planned_hours": float(current_effective_need),
            "locked_hours": float(retained_coverage),
            "current_locked_hours": float(retained_coverage),
            "projected_locked_hours": float(retained_coverage),
            "current_excess_hours": float(current_excess),
            "excess_hours": float(projected_excess),
            "base_planned_hours": float(proposed_base_hours),
            "operational_base_hours": float(
                self._operational_base_hours(
                    requirement,
                    effective_need=current_effective_need,
                )
            ),
            "safe_operational_base_hours": float(
                (
                    retained_coverage
                    + sum(active_reductions, Decimal("0.00"))
                ).quantize(Decimal("0.01"))
            ),
            "active_reduction_hours": float(
                sum(active_reductions, Decimal("0.00"))
            ),
        }

    def update(self, segment_id: str, updates: Mapping[str, Any]) -> None:
        requirement = self._requirement(segment_id)

        request = (
            self._request(updates.get("NoDemande"))
            if "NoDemande" in updates
            else (
                self._session.get(WorkforceRequest, requirement.workforce_request_id)
                if requirement.workforce_request_id
                else None
            )
        )
        target_project_id = requirement.project_id
        if "NumeroProjet" in updates:
            target_project_id = self._project(updates.get("NumeroProjet")).id
        elif request is not None and "NoDemande" in updates:
            target_project_id = request.project_id

        if request is not None and request.project_id != target_project_id:
            raise ValueError(
                "Le projet du segment ne correspond pas au projet de la demande."
            )

        assert_segment_asset_mutation_compatible(
            self._session,
            requirement,
            target_project_id=target_project_id,
            target_start_date=(
                updates.get("DateDebut")
                if "DateDebut" in updates
                else requirement.start_date
            ),
            target_end_date=(
                updates.get("DateFin")
                if "DateFin" in updates
                else requirement.end_date
            ),
            removing=(
                "Statut" in updates
                and _text(updates.get("Statut")) == "Annulé"
            ),
        )

        if "NoDemande" in updates:
            requirement.workforce_request_id = request.id if request else None
            line = self._session.get(RequestLine, request.id) if request is not None else None
            requirement.source_request_line_id = line.id if line is not None else None
        requirement.project_id = target_project_id

        effective_source_effort = requirement.source_effort_id
        if "SourceEffortID" in updates or "SourceEffortRow" in updates:
            effective_source_effort = _optional_text(
                updates.get("SourceEffortID") or updates.get("SourceEffortRow")
            )
        self._guard_work_package_dependency(
            effective_source_effort,
            project_id=requirement.project_id,
        )

        if "Technicien" in updates:
            resource = self._resource(updates.get("Technicien"))
            requirement.assigned_resource_id = resource.id if resource else None
        if "DateDebut" in updates:
            requirement.start_date = updates.get("DateDebut")
        if "DateFin" in updates:
            requirement.end_date = updates.get("DateFin")
        if "HeuresPrevues" in updates:
            requirement._pre_mutation_effective_hours = Decimal(
                str(requirement.planned_hours)
            ).quantize(Decimal("0.01"))
            requirement.planned_hours = _decimal(updates.get("HeuresPrevues"))
        if "JoursActifsCibles" in updates:
            requirement.desired_active_days = _optional_positive_int(
                updates.get("JoursActifsCibles")
            )
        if "ProfilCharge" in updates:
            requirement.load_profile = normalize_load_profile(updates.get("ProfilCharge"))
        if "Statut" in updates:
            requirement.status = _text(updates.get("Statut"))
        if "Description" in updates:
            requirement.description = _optional_text(updates.get("Description"))
        if "SourceEffortID" in updates or "SourceEffortRow" in updates:
            requirement.source_effort_id = _optional_text(
                updates.get("SourceEffortID") or updates.get("SourceEffortRow")
            )
        if "ClasseRessourceRequise" in updates:
            requirement.required_resource_class = _optional_text(
                updates.get("ClasseRessourceRequise")
            )
        if "CompetenceRequise" in updates:
            requirement.required_competency = _optional_text(
                updates.get("CompetenceRequise")
            )
        if "TypePlanification" in updates:
            requirement.planning_type = _text(updates.get("TypePlanification")) or "Flexible"
        if "Priorite" in updates:
            requirement.priority = _text(updates.get("Priorite")) or "Normale"
        if "HorsHoraireAutorise" in updates:
            requirement.outside_standard_hours_allowed = _bool(
                updates.get("HorsHoraireAutorise")
            )
        if "OrigineSegment" in updates:
            requirement.origin = _text(updates.get("OrigineSegment")) or ORIGIN_REQUEST

        if "Confirmation" in updates:
            supplied = _optional_text(updates.get("Confirmation"))
            if supplied is None:
                requirement.confirmation_overridden = False
                requirement.confirmation = self._inherited_confirmation(requirement, request)
            else:
                requirement.confirmation = normalize_confirmation(supplied)
                requirement.confirmation_overridden = True
        elif "NoDemande" in updates and not requirement.confirmation_overridden:
            requirement.confirmation = self._inherited_confirmation(requirement, request)

        self._session.flush()
