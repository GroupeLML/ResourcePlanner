from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from ...application.command_ports import (
    AllocationCommandPort,
    ApprovedDemandSyncPort,
    PlanningCommandPort,
)
from ...application.repository_ports import PlanningMutationVersionPort
from ...domain.availability_rules import (
    OutsideStandardHoursDecision,
    outside_standard_hours_decision_for_day,
)
from ...domain.confirmation import CONFIRMATION_CONFIRMED, normalize_confirmation
from ...domain.planning_engine import build_allocation_plan
from ...domain.planning_projection import project_planning_snapshot
from ...domain.value_coercion import date_from_value
from .asset_service import SqlAssetService
from .base import new_id, utc_now
from .models import (
    ORIGIN_REQUEST,
    Project,
    Resource,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    WorkforceRequestHistory,
)
from .planning_repository import SqlPlanningReadRepository
from .planning_version import SqlPlanningMutationVersionRepository
from .segment_repository import SqlSegmentRepository


INACTIVE_REQUIREMENT_STATUSES = {"Annulé", "Terminé"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value).replace(",", "."))


class SqlPlanningCommandAdapter(PlanningCommandPort):
    """Authoritative pure-engine rebuild persisted atomically in SQL.

    The caller owns the Session transaction. Existing active locked shifts are never
    deleted by an automatic rebuild; all other shifts are regenerated from the pure
    engine result. Any failure therefore rolls the whole unit of work back naturally.
    """

    def __init__(
        self,
        session: Session,
        *,
        versioning: PlanningMutationVersionPort | None = None,
    ) -> None:
        self._session = session
        self._versioning = versioning or SqlPlanningMutationVersionRepository(session)

    def _active_locked_shifts(self) -> list[Shift]:
        return list(
            self._session.scalars(
                select(Shift)
                .join(
                    ResourceRequirement,
                    Shift.resource_requirement_id == ResourceRequirement.id,
                )
                .where(
                    Shift.locked == true(),
                    ResourceRequirement.status.not_in(INACTIVE_REQUIREMENT_STATUSES),
                )
                .order_by(Shift.id)
            ).all()
        )

    def rebuild(self) -> Mapping[str, Any]:
        self._versioning.acquire()
        snapshot = SqlPlanningReadRepository(self._session).capture()
        calculation = project_planning_snapshot(snapshot)
        if calculation.unsupported_segment_ids:
            raise RuntimeError(
                "Le moteur pur ne peut pas recalculer des segments actifs non supportés: "
                + ", ".join(calculation.unsupported_segment_ids)
            )

        result = build_allocation_plan(
            calculation.segments,
            calculation.locked_allocations,
            calculation.capacity_by_resource_day,
            preserve_locked_segment_ids=calculation.preserved_segment_ids,
            outside_schedule_eligible_by_resource_day=(
                calculation.outside_schedule_eligible_by_resource_day
            ),
        )

        protected = self._active_locked_shifts()
        protected_ids = {shift.id for shift in protected}

        # Keep only active locked user decisions. Unlocked rows are disposable engine
        # output; locked rows belonging to cancelled/completed requirements are removed
        # as part of that explicit workflow transition.
        existing = self._session.scalars(select(Shift)).all()
        for shift in existing:
            if shift.id not in protected_ids:
                self._session.delete(shift)
        self._session.flush()

        requirements = self._session.scalars(select(ResourceRequirement)).all()
        requirement_by_identifier = {
            _text(requirement.legacy_segment_id) or requirement.id: requirement
            for requirement in requirements
        }
        resources = self._session.scalars(select(Resource)).all()
        resource_by_name = {resource.name: resource for resource in resources}

        for allocation in result.allocations:
            if allocation.locked:
                continue
            requirement = requirement_by_identifier.get(allocation.segment_id)
            if requirement is None:
                raise RuntimeError(
                    f"Segment {allocation.segment_id} introuvable pendant la persistance du plan."
                )
            resource = resource_by_name.get(allocation.resource_id)
            if resource is None:
                raise RuntimeError(
                    f"Ressource {allocation.resource_id} introuvable pendant la persistance du plan."
                )

            missing = not allocation.counts_as_allocated
            self._session.add(
                Shift(
                    resource_requirement_id=requirement.id,
                    resource_id=resource.id,
                    work_date=allocation.day,
                    hours=_decimal(allocation.hours),
                    allocation_type=allocation.allocation_type,
                    source="AUTO",
                    locked=False,
                    outside_standard_hours=bool(allocation.outside_schedule),
                    # NULL is intentional: automatic shifts inherit their requirement.
                    confirmation=None,
                    note=(
                        "Capacité standard insuffisante — quart hors horaire à confirmer"
                        if missing
                        else (
                            "Hors horaire autorisé au niveau du segment"
                            if allocation.outside_schedule
                            else None
                        )
                    ),
                )
            )
        self._session.flush()

        persisted_protected = (
            set(
                self._session.scalars(
                    select(Shift.id).where(Shift.id.in_(protected_ids))
                ).all()
            )
            if protected_ids
            else set()
        )
        missing_locked = sorted(protected_ids - persisted_protected)
        if missing_locked:
            raise RuntimeError(
                "Le recalcul SQL aurait supprimé des quarts verrouillés actifs: "
                + ", ".join(missing_locked)
            )

        return {
            "segments": result.segment_count,
            "allocations": len(
                [row for row in result.allocations if row.counts_as_allocated]
            ),
            "locked_allocations": len(protected_ids),
            "requested_hours": round(result.requested_hours, 2),
            "allocated_hours": round(result.allocated_hours, 2),
            "overtime_hours": round(result.overtime_hours, 2),
            "unallocated_hours": round(result.unallocated_hours, 2),
            "planning_engine": "pure",
        }


class SqlAllocationCommandAdapter(AllocationCommandPort):
    """Manual-shift and segment-assignment commands backed by one SQL transaction."""

    def __init__(
        self,
        session: Session,
        *,
        planning: PlanningCommandPort | None = None,
        versioning: PlanningMutationVersionPort | None = None,
    ) -> None:
        self._session = session
        self._versioning = versioning or SqlPlanningMutationVersionRepository(session)
        self._planning = planning or SqlPlanningCommandAdapter(
            session,
            versioning=self._versioning,
        )

    def _requirement(self, identifier: str) -> ResourceRequirement:
        wanted = _text(identifier)
        requirement = self._session.scalar(
            select(ResourceRequirement).where(
                (ResourceRequirement.legacy_segment_id == wanted)
                | (ResourceRequirement.id == wanted)
            )
        )
        if requirement is None:
            raise KeyError(f"Segment {wanted} introuvable")
        if requirement.status in INACTIVE_REQUIREMENT_STATUSES:
            raise ValueError(f"Le segment {wanted} n'est plus actif.")
        return requirement

    def _resource(self, reference: str) -> Resource:
        wanted = _text(reference)
        resource = self._session.get(Resource, wanted)
        if resource is None:
            resource = self._session.scalar(
                select(Resource).where(Resource.name == wanted)
            )
        if resource is None:
            raise KeyError(f"Ressource {wanted} introuvable")
        if not resource.active:
            raise ValueError(f"La ressource {wanted} est inactive.")
        return resource

    def _shift(self, identifier: str) -> Shift | None:
        wanted = _text(identifier)
        if not wanted:
            return None
        return self._session.scalar(
            select(Shift).where(
                (Shift.legacy_allocation_id == wanted) | (Shift.id == wanted)
            )
        )

    def _validate_manual(
        self,
        requirement: ResourceRequirement,
        resource: Resource,
        day_value: Any,
        hours_value: Any,
        outside_standard_hours: bool,
        *,
        exclude_shift_id: str | None = None,
    ) -> tuple[date, Decimal, OutsideStandardHoursDecision]:
        day = date_from_value(day_value)
        if day is None:
            raise ValueError("La date du quart est requise.")
        hours = _decimal(hours_value)
        if hours <= 0:
            raise ValueError("Les heures doivent être supérieures à zéro.")
        if day < requirement.start_date or day > requirement.end_date:
            raise ValueError("Le quart manuel doit demeurer dans la fenêtre du segment.")

        locked_statement = select(func.coalesce(func.sum(Shift.hours), 0)).where(
            Shift.resource_requirement_id == requirement.id,
            Shift.locked == true(),
        )
        if exclude_shift_id:
            locked_statement = locked_statement.where(Shift.id != exclude_shift_id)
        locked_hours = Decimal(str(self._session.scalar(locked_statement) or 0))
        if locked_hours + hours > requirement.planned_hours + Decimal("0.001"):
            raise ValueError(
                "Les heures verrouillées ne peuvent pas dépasser les heures prévues du segment."
            )

        snapshot = SqlPlanningReadRepository(self._session).capture()
        availability = outside_standard_hours_decision_for_day(
            snapshot.availability,
            resource.name,
            day,
            outside_standard_hours=outside_standard_hours,
            resource_class=resource.resource_class,
        )
        if not availability.allowed:
            if availability.override_required:
                raise ValueError(
                    "La ressource n'est pas disponible selon son horaire standard cette journée. "
                    "Autorise explicitement le quart hors horaire pour continuer."
                )
            raise ValueError(
                "La ressource n'est pas disponible cette journée"
                f" ({availability.state.reason or 'indisponibilité non contournable'})."
            )
        return day, hours, availability

    def create_manual(
        self,
        segment_id: str,
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        confirmation: str | None = None,
    ) -> str:
        self._versioning.acquire()
        requirement = self._requirement(segment_id)
        resource = self._resource(technician)
        day, hours, _availability = self._validate_manual(
            requirement,
            resource,
            day_value,
            hours_value,
            bool(hors_horaire),
        )


        identifier = f"MAN-{new_id()}"
        shift = Shift(
            legacy_allocation_id=identifier,
            resource_requirement_id=requirement.id,
            resource_id=resource.id,
            work_date=day,
            hours=hours,
            allocation_type=requirement.planning_type,
            source="MANUAL",
            locked=True,
            outside_standard_hours=_availability.override_applied,
            confirmation=(
                normalize_confirmation(confirmation) if _text(confirmation) else None
            ),
            note=_text(note) or None,
        )
        self._session.add(shift)
        self._session.flush()
        self._planning.rebuild()
        return identifier

    def update_manual(
        self,
        allocation_id: str,
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        confirmation: str | None = None,
    ) -> None:
        self._versioning.acquire()
        shift = self._shift(allocation_id)
        if shift is None:
            raise KeyError(f"Allocation {allocation_id} introuvable")
        requirement = self._requirement(shift.resource_requirement_id)
        resource = self._resource(technician)
        day, hours, _availability = self._validate_manual(
            requirement,
            resource,
            day_value,
            hours_value,
            bool(hors_horaire),
            exclude_shift_id=shift.id,
        )

        shift.resource_id = resource.id
        shift.work_date = day
        shift.hours = hours
        shift.allocation_type = requirement.planning_type
        shift.source = "MANUAL"
        shift.locked = True
        shift.outside_standard_hours = _availability.override_applied
        # Allocation update is exposed as PUT: null is the explicit persisted state
        # meaning "inherit the requirement confirmation", not "leave unchanged".
        shift.confirmation = (
            normalize_confirmation(confirmation) if _text(confirmation) else None
        )
        shift.note = _text(note) or None
        SqlAssetService(
            self._session,
            actor="planning",
        ).synchronize_shift_ad_hoc_assignment(shift=shift)
        self._session.flush()
        self._planning.rebuild()

    def move_manual(
        self,
        allocation_id: str,
        technician: str,
        day_value: Any,
        outside_standard_hours: bool = False,
    ) -> None:
        self._versioning.acquire()
        shift = self._shift(allocation_id)
        if shift is None:
            raise KeyError(f"Allocation {allocation_id} introuvable")
        requirement = self._requirement(shift.resource_requirement_id)
        resource = self._resource(technician)
        requested_day = date_from_value(day_value)
        if requested_day is None:
            raise ValueError("La date du quart est requise.")
        if requested_day < requirement.start_date or requested_day > requirement.end_date:
            raise ValueError("Le quart déplacé doit demeurer dans la fenêtre du segment.")
        day, _, availability = self._validate_manual(
            requirement,
            resource,
            requested_day,
            shift.hours,
            bool(outside_standard_hours),
            exclude_shift_id=shift.id,
        )

        shift.resource_id = resource.id
        shift.work_date = day
        shift.allocation_type = requirement.planning_type
        shift.source = "MANUAL"
        shift.locked = True
        shift.outside_standard_hours = availability.override_applied
        SqlAssetService(
            self._session,
            actor="planning",
        ).synchronize_shift_ad_hoc_assignment(shift=shift)
        self._session.flush()
        self._planning.rebuild()

    def release_manual(self, allocation_id: str) -> None:
        self._versioning.acquire()
        shift = self._shift(allocation_id)
        if shift is None:
            return
        SqlAssetService(
            self._session,
            actor="planning",
        ).assert_shift_can_return_auto(shift)
        shift.locked = False
        self._session.flush()
        self._planning.rebuild()

    def delete_manual(self, allocation_id: str) -> None:
        self._versioning.acquire()
        shift = self._shift(allocation_id)
        if shift is None:
            return
        SqlAssetService(
            self._session,
            actor="planning",
        ).delete_shift_ad_hoc_assignment(shift=shift)
        self._session.delete(shift)
        self._session.flush()
        self._planning.rebuild()

    def assign_segment(self, segment_id: str, technician: str) -> Mapping[str, Any]:
        self._versioning.acquire()
        requirement = self._requirement(segment_id)
        resource = self._resource(technician)
        requirement.assigned_resource_id = resource.id
        requirement.status = "Planifié"
        self._session.flush()
        return self._planning.rebuild()


class SqlApprovedDemandSyncAdapter(ApprovedDemandSyncPort):
    """Synchronize an approved or explicitly emergency-materialized request."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._segments = SqlSegmentRepository(session)

    def _request(self, number: str) -> WorkforceRequest:
        wanted = _text(number)
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        )
        if request is None:
            raise KeyError(f"Demande {wanted} introuvable après approbation")
        return request

    @staticmethod
    def _emergency_materialization(request: WorkforceRequest) -> bool:
        return bool(request.emergency_override_active) and request.status == "Soumise"

    def _active_requirements(self, request: WorkforceRequest) -> list[ResourceRequirement]:
        return list(
            self._session.scalars(
                select(ResourceRequirement)
                .where(
                    ResourceRequirement.workforce_request_id == request.id,
                    ResourceRequirement.status != "Annulé",
                )
                .order_by(ResourceRequirement.created_at, ResourceRequirement.id)
            ).all()
        )

    def _proposed_resource(self, request: WorkforceRequest) -> Resource | None:
        if not request.proposed_resource_id:
            return None
        return self._session.get(Resource, request.proposed_resource_id)

    def _hours_per_resource(
        self,
        request: WorkforceRequest,
        desired: int,
        current: list[ResourceRequirement],
    ) -> Decimal:
        if request.estimated_hours is not None and request.estimated_hours > 0:
            return (request.estimated_hours / Decimal(desired)).quantize(Decimal("0.01"))

        proposed = self._proposed_resource(request)
        if (
            request.estimated_days is not None
            and request.estimated_days > 0
            and proposed is not None
            and request.desired_start is not None
        ):
            end = request.desired_end or request.desired_start
            snapshot = SqlPlanningReadRepository(self._session).capture()
            capacities: list[float] = []
            cursor = request.desired_start
            while cursor <= end:
                capacity = availability_hours_for_day(
                    snapshot.availability,
                    proposed.name,
                    cursor,
                )
                if capacity > 0:
                    capacities.append(capacity)
                cursor += timedelta(days=1)
            if capacities:
                average_day = sum(capacities) / len(capacities)
                return _decimal(float(request.estimated_days) * average_day).quantize(
                    Decimal("0.01")
                )

        existing_hours = [
            requirement.planned_hours
            for requirement in current
            if requirement.planned_hours > 0
        ]
        if existing_hours:
            return (
                sum(existing_hours, Decimal("0")) / Decimal(len(existing_hours))
            ).quantize(Decimal("0.01"))

        raise ValueError(
            "Impossible de déterminer les heures prévues par ressource pour la demande "
            f"{_text(request.legacy_demand_number) or request.id}."
        )

    def sync_approved(
        self,
        demand_number: str,
        *,
        approved_request_version: int | None = None,
    ) -> None:
        request = self._request(demand_number)
        project = self._session.get(Project, request.project_id)
        if project is None:
            raise KeyError(f"Projet {request.project_id} introuvable")
        if request.desired_start is None:
            raise ValueError("La date de début de la demande est requise pour synchroniser les besoins.")

        desired = max(int(request.resource_count or 1), 1)
        current = self._active_requirements(request)
        per_resource = self._hours_per_resource(request, desired, current)
        if per_resource <= 0:
            raise ValueError("Les heures prévues par ressource doivent être supérieures à zéro.")
        inherited_confirmation = normalize_confirmation(
            request.confirmation,
            default=CONFIRMATION_CONFIRMED,
        )

        while len(current) < desired:
            identifier = self._segments.create(
                {
                    "NoDemande": _text(request.legacy_demand_number) or request.id,
                    "NumeroProjet": project.number,
                    "Technicien": None,
                    "DateDebut": request.desired_start,
                    "DateFin": request.desired_end or request.desired_start,
                    "HeuresPrevues": per_resource,
                    "Statut": "À assigner",
                    "Description": (
                        request.erp_task_label
                        or request.description
                        or "Ressource additionnelle"
                    ),
                    "CompetenceRequise": request.required_competencies,
                    "TypePlanification": "Flexible",
                    "Priorite": request.priority or "Normale",
                    "HorsHoraireAutorise": False,
                    "OrigineSegment": ORIGIN_REQUEST,
                    "Confirmation": inherited_confirmation,
                    "ConfirmationOverride": False,
                }
            )
            created = self._session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.legacy_segment_id == identifier
                )
            )
            if created is None:
                raise RuntimeError("Le besoin créé à l'approbation est introuvable.")
            current.append(created)

        if len(current) > desired:
            ranked = sorted(
                current,
                key=lambda row: (
                    0 if row.assigned_resource_id else 1,
                    row.created_at,
                    row.id,
                ),
            )
            keep_ids = {row.id for row in ranked[:desired]}
            for requirement in current:
                if requirement.id not in keep_ids:
                    requirement.status = "Annulé"
            current = [row for row in ranked if row.id in keep_ids]

        proposed = self._proposed_resource(request)
        for index, requirement in enumerate(current[:desired]):
            if index == 0 and not requirement.assigned_resource_id and proposed is not None:
                requirement.assigned_resource_id = proposed.id

            if requirement.assigned_resource_id and requirement.status == "À assigner":
                requirement.status = "Planifié"
            elif (
                not requirement.assigned_resource_id
                and requirement.status not in {"Terminé", "Annulé"}
            ):
                requirement.status = "À assigner"

            requirement.project_id = request.project_id
            requirement.start_date = request.desired_start
            requirement.end_date = request.desired_end or request.desired_start
            requirement.planned_hours = per_resource
            requirement.description = (
                request.erp_task_label
                or request.description
                or requirement.description
                or ""
            )
            requirement.required_competency = request.required_competencies
            requirement.priority = request.priority or "Normale"
            requirement.origin = ORIGIN_REQUEST
            if not requirement.confirmation_overridden:
                requirement.confirmation = inherited_confirmation

        emergency = self._emergency_materialization(request)
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action=(
                    "Synchronisation segments urgente"
                    if emergency
                    else "Synchronisation segments"
                ),
                status=request.status,
                comment=(
                    f"Segments matérialisés par dérogation urgente ({desired} ressource(s))."
                    if emergency
                    else f"Segments synchronisés avec la version approuvée ({desired} ressource(s))."
                ),
                actor_name=(
                    request.emergency_override_by_name
                    if emergency
                    else request.approved_by_name
                ),
                occurred_at=utc_now(),
            )
        )
        self._session.flush()