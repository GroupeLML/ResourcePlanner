from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from ...application.errors import ApplicationConflictError
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
from ...domain.planning_engine import MISSING_ALLOCATION_TYPE, PreservedAllocationInput, build_allocation_plan
from ...domain.planning_projection import project_planning_snapshot
from ...domain.value_coercion import date_from_value
from .asset_service import SqlAssetService
from .base import new_id, utc_now
from .models import (
    ORIGIN_AD_HOC,
    ORIGIN_QUICK_SHIFT,
    ORIGIN_REQUEST,
    Project,
    Resource,
    QuickShiftPlanningExclusion,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    WorkforceRequestHistory,
)
from .planning_repository import SqlPlanningReadRepository
from .planning_intent_repository import SqlRequestRequirementPlanningIntentRepository
from .operational_choice_models import (
    PLANNING_INTENT_AUTO,
    PLANNING_INTENT_PRESERVE_EXISTING,
    PLANNING_INTENT_QUEUED,
    PLANNING_TARGET_EXPLICIT_NONE,
    PLANNING_TARGET_EXPLICIT_RESOURCE,
    PLANNING_TARGET_INHERIT,
    PlanningWorkQueueRelease,
    PlanningWorkQueueRestoration,
    RequestRequirementPlanningIntent,
)
from .segment_asset_guard import assert_segment_asset_mutation_compatible
from .planning_version import SqlPlanningMutationVersionRepository
from .segment_repository import SqlSegmentRepository
from .same_resource_periods import SqlSameResourcePeriodCoordinator


INACTIVE_REQUIREMENT_STATUSES = {"Annulé", "Terminé"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value).replace(",", "."))


_AUTO_EXTEND_REQUIREMENT_ORIGINS = frozenset({ORIGIN_AD_HOC, ORIGIN_QUICK_SHIFT})


def _extend_ad_hoc_requirement_window(
    requirement: ResourceRequirement,
    day: date,
) -> bool:
    if requirement.origin not in _AUTO_EXTEND_REQUIREMENT_ORIGINS:
        return False
    requirement.start_date = min(requirement.start_date, day)
    requirement.end_date = max(requirement.end_date, day)
    return True


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

    def rebuild(
        self,
        *,
        preserve_shift_ids: Sequence[str] = (),
        temporary_excluded_dates_by_segment: Mapping[str, Sequence[date]] | None = None,
    ) -> Mapping[str, Any]:
        self._versioning.acquire()
        same_resource = SqlSameResourcePeriodCoordinator(self._session)
        same_resource_summary = same_resource.enforce_targets()
        snapshot = SqlPlanningReadRepository(self._session).capture()
        calculation = project_planning_snapshot(snapshot)
        intent_rows = self._session.execute(
            select(RequestRequirementPlanningIntent, ResourceRequirement)
            .join(
                ResourceRequirement,
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == ResourceRequirement.id,
            )
            .where(
                RequestRequirementPlanningIntent.intent_mode.in_(
                    (PLANNING_INTENT_PRESERVE_EXISTING, PLANNING_INTENT_QUEUED)
                )
            )
        ).all()
        suppressed_segment_ids = {
            _text(requirement.legacy_segment_id) or requirement.id
            for _intent, requirement in intent_rows
        }
        preserve_requirement_ids = {
            requirement.id
            for intent, requirement in intent_rows
            if intent.intent_mode == PLANNING_INTENT_PRESERVE_EXISTING
        }
        if calculation.unsupported_segment_ids:
            raise RuntimeError(
                "Le moteur pur ne peut pas recalculer des segments actifs non supportés: "
                + ", ".join(calculation.unsupported_segment_ids)
            )

        preserve_ids = {str(value) for value in preserve_shift_ids if str(value)}
        terminal_survivor_ids = set(
            self._session.scalars(
                select(Shift.id)
                .join(
                    ResourceRequirement,
                    Shift.resource_requirement_id == ResourceRequirement.id,
                )
                .where(ResourceRequirement.status == "Terminé")
            ).all()
        )
        if preserve_requirement_ids:
            preserve_ids.update(
                self._session.scalars(
                    select(Shift.id).where(
                        Shift.resource_requirement_id.in_(preserve_requirement_ids)
                    )
                ).all()
            )
        preserved_rows = []
        if preserve_ids:
            preserved_rows = list(
                self._session.execute(
                    select(Shift, ResourceRequirement, Resource)
                    .join(
                        ResourceRequirement,
                        Shift.resource_requirement_id == ResourceRequirement.id,
                    )
                    .join(Resource, Shift.resource_id == Resource.id)
                    .where(Shift.id.in_(preserve_ids))
                ).all()
            )
        temporary_inputs = tuple(
            PreservedAllocationInput(
                segment_id=_text(requirement.legacy_segment_id) or requirement.id,
                resource_id=resource.name,
                day=shift.work_date,
                hours=float(shift.hours),
                locked=bool(shift.locked),
                outside_schedule=bool(shift.outside_standard_hours),
                counts_as_allocated=shift.allocation_type != MISSING_ALLOCATION_TYPE,
            )
            for shift, requirement, resource in preserved_rows
        )

        temporary_exclusions = {
            str(segment_id): frozenset(days)
            for segment_id, days in (temporary_excluded_dates_by_segment or {}).items()
            if str(segment_id)
        }
        engine_segments = tuple(
            replace(
                segment,
                excluded_dates=(
                    segment.excluded_dates
                    | temporary_exclusions.get(segment.segment_id, frozenset())
                ),
            )
            for segment in calculation.segments
            if segment.segment_id not in suppressed_segment_ids
        )
        preserved_locked_counts: dict[tuple[str, str, date, float], int] = {}
        for item in temporary_inputs:
            if not item.locked:
                continue
            key = (
                item.segment_id,
                item.resource_id,
                item.day,
                round(float(item.hours), 4),
            )
            preserved_locked_counts[key] = preserved_locked_counts.get(key, 0) + 1

        engine_locked_rows = []
        for allocation in calculation.locked_allocations:
            if allocation.segment_id in suppressed_segment_ids:
                continue
            key = (
                allocation.segment_id,
                allocation.resource_id,
                allocation.day,
                round(float(allocation.hours), 4),
            )
            duplicate_count = preserved_locked_counts.get(key, 0)
            if duplicate_count:
                preserved_locked_counts[key] = duplicate_count - 1
                continue
            engine_locked_rows.append(allocation)
        engine_locked = tuple(engine_locked_rows)
        preserved_capacity_segment_ids = {
            item.segment_id for item in temporary_inputs
        }
        result = build_allocation_plan(
            engine_segments,
            engine_locked,
            calculation.capacity_by_resource_day,
            # ADR-034: suppressed PRESERVE_EXISTING segments must not regenerate
            # residual work, but their surviving Shifts still consume capacity.
            preserved_allocations=temporary_inputs,
            preserve_locked_segment_ids=(
                set(calculation.preserved_segment_ids)
                | preserved_capacity_segment_ids
            ),
            outside_schedule_eligible_by_resource_day=(
                calculation.outside_schedule_eligible_by_resource_day
            ),
        )

        protected = self._active_locked_shifts()
        protected_ids = (
            {shift.id for shift in protected}
            | {
                shift.id for shift, _requirement, _resource in preserved_rows
            }
            | terminal_survivor_ids
        )

        # Keep active locked user decisions plus the narrowly scoped survivor rows
        # explicitly preserved for this one mutation. Other automatic rows remain
        # disposable engine output.
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
            if allocation.preserved:
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
        same_resource.assert_shift_invariant()

        persisted_protected = (
            set(
                self._session.scalars(
                    select(Shift.id).where(Shift.id.in_(protected_ids))
                ).all()
            )
            if protected_ids
            else set()
        )
        missing_protected = sorted(protected_ids - persisted_protected)
        if missing_protected:
            raise RuntimeError(
                "Le recalcul SQL aurait supprimé des quarts protégés pour cette mutation: "
                + ", ".join(missing_protected)
            )

        return {
            "segments": result.segment_count,
            "allocations": len(
                [row for row in result.allocations if row.counts_as_allocated]
            ),
            "locked_allocations": len(protected),
            "requested_hours": round(result.requested_hours, 2),
            "allocated_hours": round(result.allocated_hours, 2),
            "overtime_hours": round(result.overtime_hours, 2),
            "unallocated_hours": round(result.unallocated_hours, 2),
            "planning_engine": "pure",
            "same_resource_groups": same_resource_summary.group_count,
            "same_resource_unresolved_groups": same_resource_summary.unresolved_group_count,
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

    def _request_intent(
        self,
        requirement: ResourceRequirement,
    ) -> RequestRequirementPlanningIntent | None:
        if requirement.origin != ORIGIN_REQUEST:
            return None
        rows = self._session.scalars(
            select(RequestRequirementPlanningIntent).where(
                RequestRequirementPlanningIntent.current_resource_requirement_id
                == requirement.id
            )
        ).all()
        if len(rows) > 1:
            raise ApplicationConflictError(
                "Le segment possède plusieurs intentions Planning durables.",
                code="planning_slot_binding_ambiguous",
                context={"requirement_id": requirement.id},
            )
        return rows[0] if rows else None

    def _selected_release_outstanding(
        self,
        intent: RequestRequirementPlanningIntent | None,
        *,
        resource_id: str,
        work_date: date,
    ) -> tuple[PlanningWorkQueueRelease | None, Decimal]:
        if intent is None:
            return None, Decimal("0.00")
        release = self._session.scalar(
            select(PlanningWorkQueueRelease)
            .where(
                PlanningWorkQueueRelease.planning_intent_id == intent.id,
                PlanningWorkQueueRelease.active == true(),
                PlanningWorkQueueRelease.resource_id == resource_id,
                PlanningWorkQueueRelease.work_date == work_date,
            )
            .order_by(
                PlanningWorkQueueRelease.created_at,
                PlanningWorkQueueRelease.id,
            )
        )
        if release is None:
            return None, Decimal("0.00")
        restored = Decimal(
            str(
                self._session.scalar(
                    select(
                        func.coalesce(
                            func.sum(PlanningWorkQueueRestoration.restored_hours),
                            0,
                        )
                    ).where(
                        PlanningWorkQueueRestoration.release_id == release.id
                    )
                )
                or 0
            )
        )
        outstanding = max(
            Decimal(str(release.hours)) - restored,
            Decimal("0.00"),
        )
        return release, outstanding

    def _resolve_covered_releases(
        self,
        intent: RequestRequirementPlanningIntent | None,
        requirement: ResourceRequirement,
        *,
        restored_shifts: Sequence[Shift],
    ) -> None:
        if intent is None or not restored_shifts:
            return
        tolerance = Decimal("0.001")
        for shift in restored_shifts:
            if shift.allocation_type == MISSING_ALLOCATION_TYPE:
                continue
            already_attributed = self._session.scalar(
                select(PlanningWorkQueueRestoration.id).where(
                    PlanningWorkQueueRestoration.shift_id == shift.id
                )
            )
            if already_attributed is not None:
                continue

            release, remaining = self._selected_release_outstanding(
                intent,
                resource_id=shift.resource_id,
                work_date=shift.work_date,
            )
            if release is None:
                continue

            release_hours = Decimal(str(release.hours))
            restored_before = release_hours - remaining
            if remaining <= tolerance:
                release.active = False
                release.resolved_at = release.resolved_at or utc_now()
                continue

            shift_hours = Decimal(str(shift.hours))
            if shift_hours > remaining + tolerance:
                raise ApplicationConflictError(
                    "Le placement dépasse les heures encore attribuables au retour sélectionné.",
                    code="request_work_queue_restoration_exceeds_release_outstanding",
                    context={
                        "release_id": release.id,
                        "release_hours": float(release_hours),
                        "restored_hours": float(restored_before),
                        "outstanding_hours": float(remaining),
                        "requested_hours": float(shift_hours),
                    },
                )
            attributed = shift_hours
            if attributed <= tolerance:
                continue
            self._session.add(
                PlanningWorkQueueRestoration(
                    release_id=release.id,
                    shift_id=shift.id,
                    restored_hours=attributed,
                )
            )
            restored_after = restored_before + attributed
            if restored_after + tolerance >= release_hours:
                release.active = False
                release.resolved_at = utc_now()

    def _residual_overallocation_authorized(self) -> bool:
        """Hook for the canonical over-allocation adapter after it validates policy."""

        return False

    def _controlled_work_queue_placement(
        self,
        intent: RequestRequirementPlanningIntent | None,
    ) -> bool:
        if intent is None:
            return False
        release_history = self._session.scalar(
            select(PlanningWorkQueueRelease.id).where(
                PlanningWorkQueueRelease.planning_intent_id == intent.id
            )
        )
        if release_history is not None:
            return True
        return (
            intent.intent_mode
            in (PLANNING_INTENT_PRESERVE_EXISTING, PLANNING_INTENT_QUEUED)
            and intent.target_mode != PLANNING_TARGET_INHERIT
        )

    @staticmethod
    def _assert_new_placement_cutoff(
        intent: RequestRequirementPlanningIntent | None,
        day: date,
    ) -> None:
        cutoff = intent.placement_not_before_date if intent is not None else None
        if cutoff is not None and day < cutoff:
            raise ApplicationConflictError(
                "La nouvelle mise en place précède le cutoff opérationnel du segment.",
                code="segment_placement_before_cutoff",
                context={
                    "placement_not_before_date": cutoff.isoformat(),
                    "requested_date": day.isoformat(),
                },
            )

    @staticmethod
    def _assert_projected_placement_cutoff(
        intent: RequestRequirementPlanningIntent | None,
        shift: Shift,
        *,
        resource_id: str,
        day: date,
        hours: Decimal,
    ) -> None:
        """Reject only the newly introduced portion of placement before ADR-035 cutoff."""

        cutoff = intent.placement_not_before_date if intent is not None else None
        if cutoff is None or day >= cutoff:
            return
        old_counted = _text(shift.allocation_type) != MISSING_ALLOCATION_TYPE
        old_hours = _decimal(shift.hours) if old_counted else Decimal("0")
        same_placement_identity = (
            old_counted
            and shift.work_date == day
            and _text(shift.resource_id) == _text(resource_id)
        )
        if same_placement_identity and hours <= old_hours + Decimal("0.001"):
            return
        raise ApplicationConflictError(
            "La mutation ajouterait du nouveau placement avant le cutoff opérationnel du segment.",
            code="segment_placement_before_cutoff",
            context={
                "placement_not_before_date": cutoff.isoformat(),
                "requested_date": day.isoformat(),
                "existing_date": shift.work_date.isoformat(),
                "existing_resource_id": shift.resource_id,
                "requested_resource_id": resource_id,
                "existing_counted_hours": float(old_hours),
                "requested_counted_hours": float(hours),
            },
        )

    def _current_effective_need(
        self,
        requirement: ResourceRequirement,
    ) -> Decimal:
        return SqlRequestRequirementPlanningIntentRepository(
            self._session
        ).current_effective_need_for_requirement(
            requirement.id,
            fallback_hours=requirement.planned_hours,
        )

    def _counted_coverage(self, requirement_id: str) -> Decimal:
        covered = self._session.scalar(
            select(func.coalesce(func.sum(Shift.hours), 0)).where(
                Shift.resource_requirement_id == requirement_id,
                (Shift.allocation_type.is_(None))
                | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
            )
        )
        return _decimal(covered or 0)

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

        intent = self._request_intent(requirement)
        self._assert_new_placement_cutoff(intent, day)
        if (
            self._controlled_work_queue_placement(intent)
            and not self._residual_overallocation_authorized()
        ):
            effective_need = self._current_effective_need(requirement)
            covered = self._counted_coverage(requirement.id)
            residual = max(effective_need - covered, Decimal("0.00"))
            if hours > residual + Decimal("0.001"):
                raise ApplicationConflictError(
                    "Le placement explicite dépasse le besoin courant non couvert.",
                    code="request_work_queue_placement_exceeds_residual",
                    context={
                        "requirement_id": requirement.id,
                        "effective_need": float(effective_need),
                        "covered_hours": float(covered),
                        "residual_hours": float(residual),
                        "requested_hours": float(hours),
                    },
                )
        selected_release, selected_outstanding = self._selected_release_outstanding(
            intent,
            resource_id=resource.id,
            work_date=day,
        )
        if (
            selected_release is not None
            and hours > selected_outstanding + Decimal("0.001")
        ):
            raise ApplicationConflictError(
                "Le placement dépasse les heures encore attribuables au retour sélectionné.",
                code="request_work_queue_restoration_exceeds_release_outstanding",
                context={
                    "release_id": selected_release.id,
                    "outstanding_hours": float(selected_outstanding),
                    "requested_hours": float(hours),
                },
            )
        if intent is not None and intent.intent_mode == PLANNING_INTENT_QUEUED:
            intent.intent_mode = PLANNING_INTENT_PRESERVE_EXISTING
            intent.target_mode = PLANNING_TARGET_EXPLICIT_NONE
            intent.target_resource_id = None

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
        self._resolve_covered_releases(
            intent,
            requirement,
            restored_shifts=(shift,),
        )
        self._session.flush()
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
        requested_day = date_from_value(day_value)
        if requested_day is None:
            raise ValueError("La date du quart est requise.")
        if requested_day < requirement.start_date or requested_day > requirement.end_date:
            _extend_ad_hoc_requirement_window(requirement, requested_day)
        day, hours, _availability = self._validate_manual(
            requirement,
            resource,
            requested_day,
            hours_value,
            bool(hors_horaire),
            exclude_shift_id=shift.id,
        )
        intent = self._request_intent(requirement)
        self._assert_projected_placement_cutoff(
            intent,
            shift,
            resource_id=resource.id,
            day=day,
            hours=hours,
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
            if not _extend_ad_hoc_requirement_window(requirement, requested_day):
                raise ValueError("Le quart déplacé doit demeurer dans la fenêtre du segment.")
        day, _, availability = self._validate_manual(
            requirement,
            resource,
            requested_day,
            shift.hours,
            bool(outside_standard_hours),
            exclude_shift_id=shift.id,
        )
        intent = self._request_intent(requirement)
        self._assert_projected_placement_cutoff(
            intent,
            shift,
            resource_id=resource.id,
            day=day,
            hours=_decimal(shift.hours),
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
        requirement = self._requirement(shift.resource_requirement_id)
        if requirement.origin == ORIGIN_REQUEST:
            raise ApplicationConflictError(
                "Un quart issu d'une demande approuvée doit utiliser la commande explicite de retour à la File de Travail.",
                code="request_shift_action_requires_explicit_command",
                context={"allocation_id": allocation_id, "requirement_id": requirement.id},
            )
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
        requirement = self._requirement(shift.resource_requirement_id)
        if requirement.origin == ORIGIN_REQUEST:
            raise ApplicationConflictError(
                "Un quart issu d'une demande approuvée doit utiliser une commande #778 explicite.",
                code="request_shift_action_requires_explicit_command",
                context={"allocation_id": allocation_id, "requirement_id": requirement.id},
            )
        if requirement.origin != ORIGIN_QUICK_SHIFT:
            SqlAssetService(
                self._session,
                actor="planning",
            ).delete_shift_ad_hoc_assignment(shift=shift)
            self._session.delete(shift)
            self._session.flush()
            self._planning.rebuild()
            return

        deleted_hours = Decimal(shift.hours)
        remaining_hours = Decimal(requirement.planned_hours) - deleted_hours
        if remaining_hours < 0:
            raise ValueError(
                "Le quart Quick Shift dépasse les heures prévues du segment; "
                "la suppression est refusée pour éviter un budget négatif."
            )

        survivor_ids = tuple(
            self._session.scalars(
                select(Shift.id).where(
                    Shift.resource_requirement_id == requirement.id,
                    Shift.id != shift.id,
                )
            ).all()
        )
        if remaining_hours == 0:
            if survivor_ids:
                raise ValueError(
                    "Le Quick Shift contient encore des quarts alors que la suppression "
                    "épuiserait ses heures prévues."
                )
            assert_segment_asset_mutation_compatible(
                self._session,
                requirement,
                removing=True,
            )
            SqlAssetService(
                self._session,
                actor="planning",
            ).delete_shift_ad_hoc_assignment(shift=shift)
            self._session.delete(shift)
            requirement.status = "Annulé"
            self._session.flush()
            self._planning.rebuild()
            return

        SqlAssetService(
            self._session,
            actor="planning",
        ).delete_shift_ad_hoc_assignment(shift=shift)
        requirement.planned_hours = remaining_hours
        if self._session.get(
            QuickShiftPlanningExclusion,
            {
                "resource_requirement_id": requirement.id,
                "excluded_date": shift.work_date,
            },
        ) is None:
            self._session.add(
                QuickShiftPlanningExclusion(
                    resource_requirement_id=requirement.id,
                    excluded_date=shift.work_date,
                )
            )
        self._session.delete(shift)
        self._session.flush()
        self._planning.rebuild(preserve_shift_ids=survivor_ids)

    def assign_segment(self, segment_id: str, technician: str) -> Mapping[str, Any]:
        self._versioning.acquire()
        requirement = self._requirement(segment_id)
        resource = self._resource(technician)
        intent = self._request_intent(requirement)
        controlled_restore = (
            intent is not None
            and intent.intent_mode
            in (PLANNING_INTENT_QUEUED, PLANNING_INTENT_PRESERVE_EXISTING)
        )
        survivor_ids = (
            tuple(
                self._session.scalars(
                    select(Shift.id).where(
                        Shift.resource_requirement_id == requirement.id
                    )
                ).all()
            )
            if controlled_restore
            else ()
        )
        if controlled_restore and intent is not None:
            intent.intent_mode = PLANNING_INTENT_AUTO
            intent.target_mode = PLANNING_TARGET_EXPLICIT_RESOURCE
            intent.target_resource_id = resource.id

        grouped = SqlSameResourcePeriodCoordinator(self._session).retarget_component(
            requirement.id,
            resource.id,
        )
        if not grouped:
            requirement.assigned_resource_id = resource.id
            requirement.status = "Planifié"
        self._session.flush()
        before_shift_ids = set(
            self._session.scalars(
                select(Shift.id).where(
                    Shift.resource_requirement_id == requirement.id
                )
            ).all()
        )
        result = self._planning.rebuild(preserve_shift_ids=survivor_ids)

        if controlled_restore and intent is not None:
            intent.intent_mode = PLANNING_INTENT_PRESERVE_EXISTING
            intent.target_mode = PLANNING_TARGET_EXPLICIT_RESOURCE
            intent.target_resource_id = resource.id
            restored_shifts = tuple(
                self._session.scalars(
                    select(Shift).where(
                        Shift.resource_requirement_id == requirement.id,
                        Shift.id.not_in(before_shift_ids) if before_shift_ids else true(),
                    )
                ).all()
            )
            self._resolve_covered_releases(
                intent,
                requirement,
                restored_shifts=restored_shifts,
            )
            self._session.flush()
        return result


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