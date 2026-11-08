from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import exists, or_, select, true
from sqlalchemy.orm import Session

from ...domain.planning_engine import MISSING_ALLOCATION_TYPE
from .models import Shift
from .operational_choice_models import (
    PLANNING_INTENT_PRESERVE_EXISTING,
    RequestRequirementPlanningIntent,
    RequestShiftSurvivorProtection,
)


def retained_counted_shifts(
    session: Session,
    requirement_ids: Iterable[str],
    *,
    completed_requirement_ids: Iterable[str] = (),
) -> tuple[Shift, ...]:
    """Return each physical counted Shift retained by current planning authority once.

    A retained Shift is counted if it is locked, belongs to a PRESERVE_EXISTING
    requirement, belongs to a completed requirement, or is referenced by an active
    #793 durable survivor protection. Survivor membership is expressed with EXISTS
    so overlapping authorities cannot multiply one physical Shift row.
    """

    ids = tuple(dict.fromkeys(str(value) for value in requirement_ids if str(value)))
    if not ids:
        return ()

    completed = tuple(
        dict.fromkeys(str(value) for value in completed_requirement_ids if str(value))
    )
    preserved_requirement_ids = select(
        RequestRequirementPlanningIntent.current_resource_requirement_id
    ).where(
        RequestRequirementPlanningIntent.current_resource_requirement_id.in_(ids),
        RequestRequirementPlanningIntent.intent_mode
        == PLANNING_INTENT_PRESERVE_EXISTING,
    )
    protected = exists(
        select(1).where(
            RequestShiftSurvivorProtection.shift_id == Shift.id,
            RequestShiftSurvivorProtection.active == true(),
        )
    )

    retained_conditions = [
        Shift.locked == true(),
        Shift.resource_requirement_id.in_(preserved_requirement_ids),
        protected,
    ]
    if completed:
        retained_conditions.append(Shift.resource_requirement_id.in_(completed))

    return tuple(
        session.scalars(
            select(Shift)
            .where(
                Shift.resource_requirement_id.in_(ids),
                (Shift.allocation_type.is_(None))
                | (Shift.allocation_type != MISSING_ALLOCATION_TYPE),
                or_(*retained_conditions),
            )
            .order_by(Shift.resource_requirement_id, Shift.work_date, Shift.id)
        ).all()
    )


def retained_coverage_hours(
    session: Session,
    requirement_id: str,
    *,
    completed: bool = False,
) -> Decimal:
    shifts = retained_counted_shifts(
        session,
        (requirement_id,),
        completed_requirement_ids=((requirement_id,) if completed else ()),
    )
    return sum((Decimal(str(row.hours)) for row in shifts), Decimal("0.00")).quantize(
        Decimal("0.01")
    )
