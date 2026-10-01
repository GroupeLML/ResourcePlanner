from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from ...application.errors import ApplicationValidationError
from ...application.read_models import SegmentMobilizedResourceReadModel, SegmentReadModel
from ...application.repository_ports import (
    PlanningAuthorizationPort,
    PlanningMutationVersionPort,
    SegmentRepositoryPort,
)
from ...domain.availability_rules import (
    OutsideStandardHoursDecision,
    outside_standard_hours_decision_for_day,
)
from ...domain.planning_engine import MISSING_ALLOCATION_TYPE
from ...domain.manual_overallocation import (
    INCREASE_PLANNED,
    KEEP_EXCEPTION,
    ManualOverallocationImpact,
    TOLERANCE_HOURS,
    manual_overallocation_impact,
    normalize_overallocation_policy,
)
from ...domain.value_coercion import date_from_value
from .command_adapters import SqlAllocationCommandAdapter
from .emergency_query_repository import SqlPlannerQueryRepositoryWithEmergencyOverride
from .models import Resource, ResourceRequirement, Shift
from .planning_audit import (
    ENTITY_SEGMENT,
    ENTITY_SHIFT,
    AuditedAllocationCommandAdapter,
    SqlPlanningAuditJournal,
)
from .planning_repository import SqlPlanningReadRepository
from .segment_repository import SqlSegmentRepository


def _text(value: object) -> str:
    return str(value or "").strip()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value).replace(",", "."))


def _segment_metrics(session: Session, identifier: str) -> dict[str, float] | None:
    wanted = _text(identifier)
    requirement = session.scalar(
        select(ResourceRequirement).where(
            (ResourceRequirement.id == wanted)
            | (ResourceRequirement.legacy_segment_id == wanted)
        )
    )
    if requirement is None:
        return None
    locked = session.scalar(
        select(func.coalesce(func.sum(Shift.hours), 0)).where(
            Shift.resource_requirement_id == requirement.id,
            Shift.locked == true(),
        )
    )
    planned = float(requirement.planned_hours)
    locked_hours = float(locked or 0)
    return {
        "planned_hours": round(planned, 2),
        "locked_hours": round(locked_hours, 2),
        "overallocated_hours": round(max(locked_hours - planned, 0.0), 2),
    }


def _allocation_projection_metrics(
    session: Session,
    requirements: Sequence[ResourceRequirement],
) -> dict[str, dict[str, object]]:
    """Build additive #331A coverage metrics from counted persisted shifts."""

    rows = tuple(requirements)
    if not rows:
        return {}

    requirement_ids = tuple(row.id for row in rows)
    shift_rows = session.execute(
        select(
            Shift.resource_requirement_id,
            Shift.resource_id,
            Resource.name,
            Shift.locked,
            func.coalesce(func.sum(Shift.hours), 0),
        )
        .join(Resource, Shift.resource_id == Resource.id)
        .where(
            Shift.resource_requirement_id.in_(requirement_ids),
            (Shift.allocation_type.is_(None))
            | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
        )
        .group_by(
            Shift.resource_requirement_id,
            Shift.resource_id,
            Resource.name,
            Shift.locked,
        )
        .order_by(
            Shift.resource_requirement_id,
            Resource.name,
            Shift.resource_id,
            Shift.locked,
        )
    ).all()

    by_requirement: dict[str, dict[str, object]] = {
        requirement.id: {
            "locked_hours": 0.0,
            "replaceable_hours": 0.0,
            "resources": {},
        }
        for requirement in rows
    }

    for requirement_id, resource_id, resource_name, locked, hours in shift_rows:
        amount = float(hours or 0)
        metrics = by_requirement[requirement_id]
        key = "locked_hours" if locked else "replaceable_hours"
        metrics[key] = float(metrics[key]) + amount

        resources = metrics["resources"]
        if not isinstance(resources, dict):
            continue
        resource_metrics = resources.setdefault(
            resource_id,
            {
                "resource_name": _text(resource_name),
                "allocated_hours": 0.0,
                "locked_hours": 0.0,
                "replaceable_hours": 0.0,
            },
        )
        resource_metrics["allocated_hours"] += amount
        resource_metrics[key] += amount

    result: dict[str, dict[str, object]] = {}
    for requirement in rows:
        reference = _text(requirement.legacy_segment_id) or requirement.id
        planned = float(requirement.planned_hours)
        metrics = by_requirement[requirement.id]
        locked = float(metrics["locked_hours"])
        replaceable = float(metrics["replaceable_hours"])
        covered = locked + replaceable
        resource_values = metrics["resources"]
        mobilized = ()
        if isinstance(resource_values, dict):
            mobilized = tuple(
                SegmentMobilizedResourceReadModel(
                    resource_id=resource_id,
                    resource_name=_text(values["resource_name"]),
                    allocated_hours=round(float(values["allocated_hours"]), 2),
                    locked_hours=round(float(values["locked_hours"]), 2),
                    replaceable_hours=round(float(values["replaceable_hours"]), 2),
                )
                for resource_id, values in sorted(
                    resource_values.items(),
                    key=lambda item: (
                        _text(item[1]["resource_name"]).casefold(),
                        item[0],
                    ),
                )
            )
        result[reference] = {
            "planned_hours": round(planned, 2),
            "locked_hours": round(locked, 2),
            "replaceable_hours": round(replaceable, 2),
            "covered_hours": round(covered, 2),
            "automatic_rebuild_hours": round(max(planned - locked, 0.0), 2),
            "remaining_hours": round(max(planned - covered, 0.0), 2),
            "excess_hours": round(max(covered - planned, 0.0), 2),
            # Compatibility: #38 historically defines overallocation from locked decisions.
            "overallocated_hours": round(max(locked - planned, 0.0), 2),
            "mobilized_resources": mobilized,
        }
    return result


def _segment_projection_metrics(
    session: Session,
    identifier: str,
) -> dict[str, object] | None:
    wanted = _text(identifier)
    requirement = session.scalar(
        select(ResourceRequirement).where(
            (ResourceRequirement.id == wanted)
            | (ResourceRequirement.legacy_segment_id == wanted)
        )
    )
    if requirement is None:
        return None
    reference = _text(requirement.legacy_segment_id) or requirement.id
    return _allocation_projection_metrics(session, (requirement,)).get(reference)


def _snapshot_with_metrics(
    snapshot: Mapping[str, object] | None,
    metrics: Mapping[str, float] | None,
) -> dict[str, object] | None:
    if snapshot is None:
        return None
    result = dict(snapshot)
    if metrics:
        result.update(metrics)
    return result


class SqlSegmentRepositoryWithAllocationMetrics(SqlSegmentRepository):
    """Canonical segment repository enriched with derived locked-hour diagnostics."""

    def __init__(self, session: Session, *, actor_name: str = "") -> None:
        super().__init__(session, actor_name=actor_name)
        self._overallocation_session = session

    def _metric_map(self) -> dict[str, dict[str, object]]:
        requirements = self._overallocation_session.scalars(
            select(ResourceRequirement)
        ).all()
        return _allocation_projection_metrics(
            self._overallocation_session,
            requirements,
        )

    @staticmethod
    def _enrich(
        row: SegmentReadModel,
        metrics: Mapping[str, object] | None,
    ) -> SegmentReadModel:
        values = metrics or {}
        locked = float(values.get("locked_hours", 0.0))
        replaceable = float(values.get("replaceable_hours", 0.0))
        covered = float(values.get("covered_hours", 0.0))
        rebuild = float(values.get("automatic_rebuild_hours", 0.0))
        remaining = float(values.get("remaining_hours", 0.0))
        coverage_excess = float(values.get("excess_hours", 0.0))
        locked_excess = float(values.get("overallocated_hours", 0.0))
        mobilized = values.get("mobilized_resources", ())
        if not isinstance(mobilized, tuple):
            mobilized = ()
        return replace(
            row,
            mobilized_resources=mobilized,
            locked_hours=round(locked, 2),
            replaceable_hours=round(replaceable, 2),
            covered_hours=round(covered, 2),
            automatic_rebuild_hours=round(rebuild, 2),
            remaining_hours=round(remaining, 2),
            excess_hours=round(coverage_excess, 2),
            overallocated_hours=round(locked_excess, 2),
            overallocated=locked_excess > TOLERANCE_HOURS,
        )

    def list(self, *, include_cancelled: bool = True) -> Sequence[SegmentReadModel]:
        rows = super().list(include_cancelled=include_cancelled)
        metrics = self._metric_map()
        return tuple(self._enrich(row, metrics.get(row.segment_id)) for row in rows)

    def get(self, segment_id: str) -> SegmentReadModel | None:
        row = super().get(segment_id)
        if row is None:
            return None
        return self._enrich(row, _segment_projection_metrics(self._overallocation_session, row.segment_id))


def evaluate_projected_manual_state(
    session: Session,
    requirement: ResourceRequirement,
    resource: Resource,
    day_value: Any,
    outside_standard_hours: bool,
    *,
    current_locked_hours: Decimal,
    projected_locked_hours: Decimal,
    window_start: date | None = None,
    window_end: date | None = None,
) -> tuple[date, ManualOverallocationImpact, OutsideStandardHoursDecision]:
    """Pure #333 projected validation used by DnD previews and atomic execution.

    It validates the supplied final window and availability but never changes the
    requirement, operational choices, approval state, or planning version.
    """

    day = date_from_value(day_value)
    if day is None:
        raise ValueError("La date du quart est requise.")
    lower = window_start or requirement.start_date
    upper = window_end or requirement.end_date
    if day < lower or day > upper:
        raise ValueError("Le quart manuel doit demeurer dans la fenêtre projetée du segment.")

    impact = manual_overallocation_impact(
        planned_hours=float(requirement.planned_hours),
        current_locked_hours=float(current_locked_hours),
        projected_locked_hours=float(projected_locked_hours),
    )
    snapshot = SqlPlanningReadRepository(session).capture()
    availability = outside_standard_hours_decision_for_day(
        snapshot.availability,
        resource.name,
        day,
        outside_standard_hours=outside_standard_hours,
    )
    if not availability.allowed and not availability.override_required:
        raise ValueError(
            "La ressource n'est pas disponible cette journée"
            f" ({availability.state.reason or 'indisponibilité non contournable'})."
        )
    return day, impact, availability


def validate_projected_manual_state(
    session: Session,
    requirement: ResourceRequirement,
    resource: Resource,
    day_value: Any,
    outside_standard_hours: bool,
    *,
    current_locked_hours: Decimal,
    projected_locked_hours: Decimal,
    overallocation_policy: str | None,
    authorization: PlanningAuthorizationPort | None,
    expected_operational_version: int | None = None,
) -> tuple[date, ManualOverallocationImpact, OutsideStandardHoursDecision]:
    """Validate one final projected locked state and apply the shared #13/#38 policy."""

    day = date_from_value(day_value)
    if day is None:
        raise ValueError("La date du quart est requise.")
    if day < requirement.start_date or day > requirement.end_date:
        raise ValueError("Le quart manuel doit demeurer dans la fenêtre du segment.")

    try:
        policy = normalize_overallocation_policy(overallocation_policy)
    except ValueError as exc:
        raise ApplicationValidationError(
            str(exc),
            code="allocation_overallocation_policy_invalid",
            context={"value": overallocation_policy},
        ) from exc

    impact = manual_overallocation_impact(
        planned_hours=float(requirement.planned_hours),
        current_locked_hours=float(current_locked_hours),
        projected_locked_hours=float(projected_locked_hours),
    )

    if impact.increases_exception and policy is None:
        reference = _text(requirement.legacy_segment_id) or requirement.id
        raise ApplicationValidationError(
            "Ce quart ferait dépasser les heures prévues du segment. Choisis explicitement d'augmenter les heures prévues ou de conserver la surallocation comme dérogation.",
            code="allocation_overallocation_choice_required",
            context={
                "segment_id": reference,
                "planned_hours": impact.planned_hours,
                "current_locked_hours": impact.current_locked_hours,
                "projected_locked_hours": impact.projected_locked_hours,
                "current_excess_hours": impact.current_excess_hours,
                "excess_hours": impact.projected_excess_hours,
            },
        )

    if policy == INCREASE_PLANNED and impact.projected_excess_hours > TOLERANCE_HOURS:
        if authorization is not None:
            authorization.authorize_planned_hours(
                _text(requirement.legacy_segment_id) or requirement.id,
                impact.projected_locked_hours,
                explicit_increase=True,
                expected_operational_version=expected_operational_version,
            )
        requirement.planned_hours = _decimal(impact.projected_locked_hours).quantize(
            Decimal("0.01")
        )
        session.flush()
    elif impact.increases_exception and policy != KEEP_EXCEPTION:
        raise ApplicationValidationError(
            "Une décision explicite est requise pour la surallocation manuelle.",
            code="allocation_overallocation_choice_required",
            context={
                "segment_id": _text(requirement.legacy_segment_id) or requirement.id,
                "planned_hours": impact.planned_hours,
                "current_locked_hours": impact.current_locked_hours,
                "projected_locked_hours": impact.projected_locked_hours,
                "current_excess_hours": impact.current_excess_hours,
                "excess_hours": impact.projected_excess_hours,
            },
        )

    snapshot = SqlPlanningReadRepository(session).capture()
    availability = outside_standard_hours_decision_for_day(
        snapshot.availability,
        resource.name,
        day,
        outside_standard_hours=outside_standard_hours,
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
    return day, impact, availability


class SqlOverallocationAllocationCommandAdapter(SqlAllocationCommandAdapter):
    """Manual allocation adapter that requires an explicit decision to increase excess."""

    def __init__(
        self,
        session: Session,
        *,
        planning=None,
        authorization: PlanningAuthorizationPort | None = None,
        versioning: PlanningMutationVersionPort | None = None,
    ) -> None:
        super().__init__(session, planning=planning, versioning=versioning)
        self._overallocation_session = session
        self._authorization = authorization
        self._active_policy: str | None = None

    @staticmethod
    def _policy(value: object) -> str | None:
        try:
            return normalize_overallocation_policy(value)
        except ValueError as exc:
            raise ApplicationValidationError(
                str(exc),
                code="allocation_overallocation_policy_invalid",
                context={"value": value},
            ) from exc

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
        hours = _decimal(hours_value)
        if hours <= 0:
            raise ValueError("Les heures doivent être supérieures à zéro.")

        current_locked = Decimal(
            str(
                self._overallocation_session.scalar(
                    select(func.coalesce(func.sum(Shift.hours), 0)).where(
                        Shift.resource_requirement_id == requirement.id,
                        Shift.locked == true(),
                    )
                )
                or 0
            )
        )
        other_statement = select(func.coalesce(func.sum(Shift.hours), 0)).where(
            Shift.resource_requirement_id == requirement.id,
            Shift.locked == true(),
        )
        if exclude_shift_id:
            other_statement = other_statement.where(Shift.id != exclude_shift_id)
        other_locked = Decimal(
            str(self._overallocation_session.scalar(other_statement) or 0)
        )
        projected_locked = other_locked + hours
        day, _impact, availability = validate_projected_manual_state(
            self._overallocation_session,
            requirement,
            resource,
            day_value,
            outside_standard_hours,
            current_locked_hours=current_locked,
            projected_locked_hours=projected_locked,
            overallocation_policy=self._active_policy,
            authorization=self._authorization,
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
        overallocation_policy: str | None = None,
    ) -> str:
        self._active_policy = self._policy(overallocation_policy)
        try:
            return super().create_manual(
                segment_id,
                technician,
                day_value,
                hours_value,
                hors_horaire,
                note,
                confirmation,
            )
        finally:
            self._active_policy = None

    def update_manual(
        self,
        allocation_id: str,
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        confirmation: str | None = None,
        overallocation_policy: str | None = None,
    ) -> None:
        self._active_policy = self._policy(overallocation_policy)
        try:
            super().update_manual(
                allocation_id,
                technician,
                day_value,
                hours_value,
                hors_horaire,
                note,
                confirmation,
            )
        finally:
            self._active_policy = None


def append_overallocation_audit(
    session: Session,
    journal: SqlPlanningAuditJournal,
    reference: str | None,
    before_snapshot: tuple[str, str, dict[str, object]] | None,
    before_metrics: Mapping[str, float] | None,
    *,
    policy: str | None = None,
) -> None:
    """Append the canonical #38 segment audit for a completed mutation."""

    if not reference or before_snapshot is None:
        return
    after_snapshot = journal.requirement_snapshot(reference)
    after_metrics = _segment_metrics(session, reference)
    if after_snapshot is None or after_metrics is None or before_metrics is None:
        return

    before_excess = float(before_metrics.get("overallocated_hours", 0.0))
    after_excess = float(after_metrics.get("overallocated_hours", 0.0))
    before_planned = float(before_metrics.get("planned_hours", 0.0))
    after_planned = float(after_metrics.get("planned_hours", 0.0))
    action: str | None = None
    if policy == INCREASE_PLANNED and after_planned > before_planned + TOLERANCE_HOURS:
        action = "Augmentation heures prévues depuis quart manuel"
    elif after_excess > before_excess + TOLERANCE_HOURS:
        action = "Dérogation surallocation manuelle"
    elif before_excess > after_excess + TOLERANCE_HOURS:
        action = (
            "Régularisation surallocation manuelle"
            if after_excess <= TOLERANCE_HOURS
            else "Réduction surallocation manuelle"
        )
    if action is None:
        return

    entity_id, entity_reference, before_values = before_snapshot
    _, _, after_values = after_snapshot
    journal.append(
        entity_type=ENTITY_SEGMENT,
        entity_id=entity_id,
        entity_reference=entity_reference,
        action=action,
        before=_snapshot_with_metrics(before_values, before_metrics),
        after=_snapshot_with_metrics(after_values, after_metrics),
    )


class OverallocationAuditedAllocationCommandAdapter(AuditedAllocationCommandAdapter):
    """Planning audit extended with explicit overallocation/regularization events."""

    def __init__(
        self,
        delegate: SqlOverallocationAllocationCommandAdapter,
        journal: SqlPlanningAuditJournal,
        session: Session,
    ) -> None:
        super().__init__(delegate, journal)
        self._overallocation_session = session

    def _segment_audit_state(
        self,
        reference: str | None,
    ) -> tuple[tuple[str, str, dict[str, object]] | None, dict[str, float] | None]:
        if not reference:
            return None, None
        return self._journal.requirement_snapshot(reference), _segment_metrics(
            self._overallocation_session,
            reference,
        )

    def _append_segment_change(
        self,
        reference: str | None,
        before_snapshot: tuple[str, str, dict[str, object]] | None,
        before_metrics: Mapping[str, float] | None,
        *,
        policy: str | None = None,
    ) -> None:
        append_overallocation_audit(
            self._overallocation_session,
            self._journal,
            reference,
            before_snapshot,
            before_metrics,
            policy=policy,
        )

    def create_manual(
        self,
        segment_id: str,
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        confirmation: str | None = None,
        overallocation_policy: str | None = None,
    ) -> str:
        before_snapshot, before_metrics = self._segment_audit_state(segment_id)
        reference = self._delegate.create_manual(
            segment_id,
            technician,
            day_value,
            hours_value,
            hors_horaire,
            note,
            confirmation,
            overallocation_policy,
        )
        current = self._journal.shift_snapshot(reference)
        if current is not None:
            entity_id, entity_reference, parent, after = current
            self._journal.append(
                entity_type=ENTITY_SHIFT,
                entity_id=entity_id,
                entity_reference=entity_reference,
                parent_reference=parent,
                action="Création quart manuel",
                after=after,
            )
        self._append_segment_change(
            segment_id,
            before_snapshot,
            before_metrics,
            policy=normalize_overallocation_policy(overallocation_policy),
        )
        return reference

    def update_manual(
        self,
        allocation_id: str,
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        confirmation: str | None = None,
        overallocation_policy: str | None = None,
    ) -> None:
        previous = self._journal.shift_snapshot(allocation_id)
        parent = previous[2] if previous is not None else None
        before_snapshot, before_metrics = self._segment_audit_state(parent)
        self._delegate.update_manual(
            allocation_id,
            technician,
            day_value,
            hours_value,
            hors_horaire,
            note,
            confirmation,
            overallocation_policy,
        )
        current = self._journal.shift_snapshot(allocation_id)
        if previous is not None and current is not None:
            entity_id, entity_reference, parent_reference, before = previous
            _, _, _, after = current
            self._journal.append(
                entity_type=ENTITY_SHIFT,
                entity_id=entity_id,
                entity_reference=entity_reference,
                parent_reference=parent_reference,
                action="Modification quart manuel",
                before=before,
                after=after,
            )
        self._append_segment_change(
            parent,
            before_snapshot,
            before_metrics,
            policy=normalize_overallocation_policy(overallocation_policy),
        )

    def move_manual(
        self,
        allocation_id: str,
        technician: str,
        day_value: Any,
        outside_standard_hours: bool = False,
    ) -> None:
        previous = self._journal.shift_snapshot(allocation_id)
        self._delegate.move_manual(
            allocation_id,
            technician,
            day_value,
            outside_standard_hours,
        )
        current = self._journal.shift_snapshot(allocation_id)
        if previous is None or current is None:
            return
        entity_id, entity_reference, parent, before = previous
        _, _, _, after = current
        self._journal.append(
            entity_type=ENTITY_SHIFT,
            entity_id=entity_id,
            entity_reference=entity_reference,
            parent_reference=parent,
            action="Déplacement quart",
            before=before,
            after=after,
        )

    def release_manual(self, allocation_id: str) -> None:
        previous = self._journal.shift_snapshot(allocation_id)
        parent = previous[2] if previous is not None else None
        before_snapshot, before_metrics = self._segment_audit_state(parent)
        super().release_manual(allocation_id)
        self._append_segment_change(parent, before_snapshot, before_metrics)

    def delete_manual(self, allocation_id: str) -> None:
        previous = self._journal.shift_snapshot(allocation_id)
        parent = previous[2] if previous is not None else None
        before_snapshot, before_metrics = self._segment_audit_state(parent)
        super().delete_manual(allocation_id)
        self._append_segment_change(parent, before_snapshot, before_metrics)


class OverallocationAuditedSegmentRepository(SegmentRepositoryPort):
    """Adds explicit segment audit events when planned-hour edits cross the budget."""

    def __init__(
        self,
        delegate: SegmentRepositoryPort,
        journal: SqlPlanningAuditJournal,
    ) -> None:
        self._delegate = delegate
        self._journal = journal

    def list(self, *, include_cancelled: bool = True) -> Sequence[SegmentReadModel]:
        return self._delegate.list(include_cancelled=include_cancelled)

    def get(self, segment_id: str) -> SegmentReadModel | None:
        return self._delegate.get(segment_id)

    def create(self, values: Mapping[str, Any]) -> str:
        return self._delegate.create(values)

    def update(self, segment_id: str, updates: Mapping[str, Any]) -> None:
        before_row = self._delegate.get(segment_id)
        before_snapshot = self._journal.requirement_snapshot(segment_id)
        self._delegate.update(segment_id, updates)
        after_row = self._delegate.get(segment_id)
        after_snapshot = self._journal.requirement_snapshot(segment_id)
        if (
            before_row is None
            or after_row is None
            or before_snapshot is None
            or after_snapshot is None
        ):
            return
        before_excess = float(before_row.overallocated_hours)
        after_excess = float(after_row.overallocated_hours)
        if abs(after_excess - before_excess) <= TOLERANCE_HOURS:
            return
        action = (
            "Dérogation surallocation manuelle"
            if after_excess > before_excess
            else "Régularisation surallocation manuelle"
            if after_excess <= TOLERANCE_HOURS
            else "Réduction surallocation manuelle"
        )
        entity_id, entity_reference, before_values = before_snapshot
        _, _, after_values = after_snapshot
        self._journal.append(
            entity_type=ENTITY_SEGMENT,
            entity_id=entity_id,
            entity_reference=entity_reference,
            action=action,
            before={
                **before_values,
                "locked_hours": before_row.locked_hours,
                "overallocated_hours": before_row.overallocated_hours,
            },
            after={
                **after_values,
                "locked_hours": after_row.locked_hours,
                "overallocated_hours": after_row.overallocated_hours,
            },
        )


class SqlPlannerQueryRepositoryWithOverallocation(
    SqlPlannerQueryRepositoryWithEmergencyOverride
):
    """Canonical reads enriched with manual-overallocation diagnostics."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self._overallocation_query_session = session
        self._segments = SqlSegmentRepositoryWithAllocationMetrics(session)

    def list_shifts(self, **kwargs):
        rows = super().list_shifts(**kwargs)
        if not rows:
            return rows
        segments = {
            row.segment_id: row
            for row in self._segments.list(include_cancelled=True)
        }
        return tuple(
            replace(
                row,
                segment_planned_hours=(
                    float(segments[row.segment_id].planned_hours)
                    if row.segment_id in segments
                    else 0.0
                ),
                segment_locked_hours=(
                    float(segments[row.segment_id].locked_hours)
                    if row.segment_id in segments
                    else 0.0
                ),
                segment_overallocated_hours=(
                    float(segments[row.segment_id].overallocated_hours)
                    if row.segment_id in segments
                    else 0.0
                ),
            )
            for row in rows
        )
