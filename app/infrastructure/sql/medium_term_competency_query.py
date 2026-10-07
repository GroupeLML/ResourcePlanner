from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application.medium_term_budget import (
    COMPETENCY_DIAGNOSTIC_ALTERNATIVE_UNRESOLVED,
    COMPETENCY_DIAGNOSTIC_CAPACITY_EXCEEDED,
    COMPETENCY_DIAGNOSTIC_CAPACITY_ZERO,
    COMPETENCY_DIAGNOSTIC_COMMON_QUALIFICATION_EXCEEDED,
    COMPETENCY_DIAGNOSTIC_COMMON_QUALIFICATION_ZERO,
    COMPETENCY_DIAGNOSTIC_GROUP_CLASS_INACTIVE,
    COMPETENCY_DIAGNOSTIC_REFERENCE_UNRESOLVED,
    COMPETENCY_DIAGNOSTIC_REQUESTED_HOURS_UNAVAILABLE,
    MediumTermCompetencyCombinationWeekReadModel,
    MediumTermCompetencyWeekReadModel,
)
from ...application.read_models import DemandReadModel
from ...domain.demand_periods import projected_hours_in_window
from .demand_period_models import (
    WorkforceRequestPeriod,
    WorkforceRequestPeriodSelection,
)
from .medium_term_capacity_query import (
    build_workforce_weekly_capacity_by_resource,
    medium_term_capacity_state,
)
from .models import (
    Competency,
    RequestLine,
    Resource,
    ResourceCompetency,
    WorkforceRequest,
)
from .resource_class_models import ResourceClassConfig


@dataclass(frozen=True, slots=True)
class MediumTermCompetencyProjection:
    window_start: date | None
    window_end: date | None
    skills_by_week: Mapping[date, tuple[MediumTermCompetencyWeekReadModel, ...]]
    combinations_by_week: Mapping[
        date, tuple[MediumTermCompetencyCombinationWeekReadModel, ...]
    ]
    diagnostics_by_week: Mapping[date, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _DemandUnit:
    competency_ids: tuple[str, ...]
    unresolved_reference: bool
    required_resource_class_code: str | None
    start_date: date | None
    end_date: date | None
    estimated_hours: Decimal | None
    periods: tuple[WorkforceRequestPeriod, ...]


@dataclass(slots=True)
class _HoursAccumulator:
    total: Decimal = Decimal("0.00")
    unavailable: bool = False
    diagnostics: list[str] | None = None
    line_count: int = 0

    def __post_init__(self) -> None:
        if self.diagnostics is None:
            self.diagnostics = []

    def add(
        self,
        hours: Decimal | None,
        diagnostics: Sequence[str],
        *,
        count_line: bool = False,
    ) -> None:
        if count_line:
            self.line_count += 1
        if hours is None:
            self.unavailable = True
        else:
            self.total += hours
        assert self.diagnostics is not None
        for diagnostic in diagnostics:
            if diagnostic not in self.diagnostics:
                self.diagnostics.append(diagnostic)

    def value(self) -> Decimal | None:
        return None if self.unavailable else self.total.quantize(Decimal("0.01"))


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    value_text = _text(value)
    return value_text or None


def _cancelled(demand: DemandReadModel) -> bool:
    status = _text(demand.effective_status or demand.status).casefold()
    return any(token in status for token in ("annul", "cancel"))


def _overlaps(
    start_date: date | None,
    end_date: date | None,
    window_start: date,
    window_end: date,
) -> bool:
    if start_date is None:
        return False
    effective_end = end_date or start_date
    return effective_end >= window_start and start_date <= window_end


def _period_week_hours(
    periods: Sequence[WorkforceRequestPeriod],
    selected_period_ids: frozenset[str],
    *,
    week_start: date,
    week_end: date,
) -> tuple[Decimal | None, tuple[str, ...]]:
    cumulative: list[WorkforceRequestPeriod] = []
    alternatives: defaultdict[str, list[WorkforceRequestPeriod]] = defaultdict(list)
    for period in periods:
        if _text(period.kind).upper() == "ALTERNATIVE":
            alternatives[_text(period.alternative_group)].append(period)
        else:
            cumulative.append(period)

    total = Decimal("0.00")
    diagnostics: list[str] = []
    unavailable = False

    def contribution(period: WorkforceRequestPeriod) -> Decimal | None:
        if not _overlaps(period.start_date, period.end_date, week_start, week_end):
            return Decimal("0.00")
        if period.hours is None:
            return None
        return Decimal(
            str(
                projected_hours_in_window(
                    float(period.hours),
                    period.start_date,
                    period.end_date,
                    week_start,
                    week_end,
                )
            )
        ).quantize(Decimal("0.01"))

    for period in cumulative:
        value = contribution(period)
        if value is None:
            unavailable = True
            if COMPETENCY_DIAGNOSTIC_REQUESTED_HOURS_UNAVAILABLE not in diagnostics:
                diagnostics.append(COMPETENCY_DIAGNOSTIC_REQUESTED_HOURS_UNAVAILABLE)
        else:
            total += value

    for options in alternatives.values():
        selected = [row for row in options if row.id in selected_period_ids]
        candidates = selected or list(options)
        overlaps_week = any(
            _overlaps(row.start_date, row.end_date, week_start, week_end)
            for row in candidates
        )
        if not selected and overlaps_week:
            diagnostics.append(COMPETENCY_DIAGNOSTIC_ALTERNATIVE_UNRESOLVED)
        values = [contribution(row) for row in candidates]
        if any(value is None for value in values):
            if overlaps_week:
                unavailable = True
                if COMPETENCY_DIAGNOSTIC_REQUESTED_HOURS_UNAVAILABLE not in diagnostics:
                    diagnostics.append(COMPETENCY_DIAGNOSTIC_REQUESTED_HOURS_UNAVAILABLE)
            continue
        if values:
            total += max(value for value in values if value is not None)

    return (
        None if unavailable else total.quantize(Decimal("0.01")),
        tuple(dict.fromkeys(diagnostics)),
    )


def _unit_week_hours(
    unit: _DemandUnit,
    selected_period_ids: frozenset[str],
    *,
    week_start: date,
    week_end: date,
) -> tuple[Decimal | None, tuple[str, ...], bool]:
    if unit.periods:
        value, diagnostics = _period_week_hours(
            unit.periods,
            selected_period_ids,
            week_start=week_start,
            week_end=week_end,
        )
        relevant = any(
            _overlaps(row.start_date, row.end_date, week_start, week_end)
            for row in unit.periods
        )
        return value, diagnostics, relevant

    relevant = _overlaps(
        unit.start_date,
        unit.end_date,
        week_start,
        week_end,
    )
    if not relevant:
        return Decimal("0.00"), (), False
    if unit.estimated_hours is None or unit.start_date is None:
        return (
            None,
            (COMPETENCY_DIAGNOSTIC_REQUESTED_HOURS_UNAVAILABLE,),
            True,
        )
    effective_end = unit.end_date or unit.start_date
    value = projected_hours_in_window(
        float(unit.estimated_hours),
        unit.start_date,
        effective_end,
        week_start,
        week_end,
    )
    return Decimal(str(value)).quantize(Decimal("0.01")), (), True


def _matches_task_filter(
    *,
    project_number: str | None,
    task_catalog_item_id: str | None,
    task_code: str | None,
    task_filter_active: bool,
    allowed_task_ids: frozenset[str],
    allowed_task_codes_by_project: Mapping[str, frozenset[str]],
) -> bool:
    if not task_filter_active:
        return True
    if task_catalog_item_id and task_catalog_item_id in allowed_task_ids:
        return True
    if project_number and task_code:
        return task_code in allowed_task_codes_by_project.get(
            project_number,
            frozenset(),
        )
    return False


def _load_demand_units(
    session: Session,
    *,
    demands: Sequence[DemandReadModel],
    project_ids: tuple[str, ...],
    business_resource_class_code: str | None,
    task_filter_active: bool,
    allowed_task_ids: frozenset[str],
    allowed_task_codes_by_project: Mapping[str, frozenset[str]],
) -> tuple[tuple[_DemandUnit, ...], frozenset[str]]:
    if not project_ids:
        return (), frozenset()

    requests = tuple(
        session.scalars(
            select(WorkforceRequest).where(
                WorkforceRequest.project_id.in_(project_ids)
            )
        ).all()
    )
    request_by_number = {
        _optional_text(row.legacy_demand_number) or row.id: row for row in requests
    }
    request_ids = tuple(row.id for row in requests)
    if not request_ids:
        return (), frozenset()

    request_lines = tuple(
        session.scalars(
            select(RequestLine).where(
                RequestLine.workforce_request_id.in_(request_ids)
            )
        ).all()
    )
    line_by_id = {row.id: row for row in request_lines}
    periods = tuple(
        session.scalars(
            select(WorkforceRequestPeriod).where(
                WorkforceRequestPeriod.workforce_request_id.in_(request_ids),
                WorkforceRequestPeriod.active == true(),
            )
        ).all()
    )
    periods_by_request: defaultdict[str, list[WorkforceRequestPeriod]] = defaultdict(list)
    periods_by_line: defaultdict[str, list[WorkforceRequestPeriod]] = defaultdict(list)
    for row in periods:
        periods_by_request[row.workforce_request_id].append(row)
        if row.request_line_id:
            periods_by_line[row.request_line_id].append(row)

    selections = tuple(
        session.scalars(
            select(WorkforceRequestPeriodSelection).where(
                WorkforceRequestPeriodSelection.workforce_request_id.in_(request_ids)
            )
        ).all()
    )
    selected_period_ids = frozenset(row.period_id for row in selections)

    units: list[_DemandUnit] = []
    wanted_business_class = _optional_text(business_resource_class_code)

    for demand in demands:
        if _cancelled(demand):
            continue
        request = request_by_number.get(demand.number)
        if request is None:
            continue

        if demand.line_mode:
            active_lines = tuple(line for line in demand.lines if line.active)
            for line in active_lines:
                if _text(line.kind).upper() != "WORKFORCE":
                    continue
                raw_line = line_by_id.get(line.line_id)
                if not _matches_task_filter(
                    project_number=demand.project_number,
                    task_catalog_item_id=(
                        _optional_text(raw_line.task_catalog_item_id)
                        if raw_line is not None
                        else None
                    ),
                    task_code=_optional_text(line.task_code),
                    task_filter_active=task_filter_active,
                    allowed_task_ids=allowed_task_ids,
                    allowed_task_codes_by_project=allowed_task_codes_by_project,
                ):
                    continue
                required_class = _optional_text(line.required_resource_class)
                if (
                    wanted_business_class is not None
                    and required_class != wanted_business_class
                ):
                    continue
                competency_ids = tuple(
                    dict.fromkeys(
                        value
                        for value in (_text(item) for item in line.required_competency_ids)
                        if value
                    )
                )
                units.append(
                    _DemandUnit(
                        competency_ids=competency_ids,
                        unresolved_reference=bool(
                            _optional_text(line.required_competencies)
                            and not competency_ids
                        ),
                        required_resource_class_code=required_class,
                        start_date=line.desired_start,
                        end_date=line.desired_end or line.desired_start,
                        estimated_hours=(
                            Decimal(str(line.estimated_hours))
                            if line.estimated_hours is not None
                            else None
                        ),
                        periods=(
                            tuple(periods_by_line.get(line.line_id, ()))
                            or (
                                tuple(
                                    row
                                    for row in periods_by_request.get(request.id, ())
                                    if row.request_line_id in (None, line.line_id)
                                )
                                if len(active_lines) == 1
                                else ()
                            )
                        ),
                    )
                )
            continue

        if not _matches_task_filter(
            project_number=demand.project_number,
            task_catalog_item_id=None,
            task_code=_optional_text(request.erp_task_code),
            task_filter_active=task_filter_active,
            allowed_task_ids=allowed_task_ids,
            allowed_task_codes_by_project=allowed_task_codes_by_project,
        ):
            continue
        if wanted_business_class is not None:
            # Legacy simple requests do not carry a canonical business-class field.
            # Never infer one from the competency grouping class.
            continue
        competency_ids = tuple(
            dict.fromkeys(
                value
                for value in (
                    _text(item) for item in demand.required_competency_ids
                )
                if value
            )
        )
        units.append(
            _DemandUnit(
                competency_ids=competency_ids,
                unresolved_reference=bool(
                    _optional_text(demand.required_competencies)
                    and not competency_ids
                ),
                required_resource_class_code=None,
                start_date=demand.desired_start,
                end_date=demand.desired_end or demand.desired_start,
                estimated_hours=(
                    Decimal(str(demand.estimated_hours))
                    if demand.estimated_hours is not None
                    else None
                ),
                periods=tuple(periods_by_request.get(request.id, ())),
            )
        )

    return tuple(units), selected_period_ids


def _projection_window(
    units: Sequence[_DemandUnit],
) -> tuple[date | None, date | None]:
    starts: list[date] = []
    ends: list[date] = []
    for unit in units:
        if unit.periods:
            starts.extend(row.start_date for row in unit.periods)
            ends.extend(row.end_date for row in unit.periods)
        elif unit.start_date is not None:
            starts.append(unit.start_date)
            ends.append(unit.end_date or unit.start_date)
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def build_medium_term_competency_projection(
    queries: object,
    session: Session,
    *,
    demands: Sequence[DemandReadModel],
    project_ids: tuple[str, ...],
    start: date | None,
    end: date | None,
    business_resource_class_code: str | None = None,
    competency_resource_class_code: str | None = None,
    task_filter_active: bool = False,
    allowed_task_ids: frozenset[str] = frozenset(),
    allowed_task_codes_by_project: Mapping[str, frozenset[str]] | None = None,
) -> MediumTermCompetencyProjection:
    """Build the ADR-028 non-additive weekly skill analytics."""

    if not project_ids:
        return MediumTermCompetencyProjection(
            window_start=start,
            window_end=end,
            skills_by_week={},
            combinations_by_week={},
            diagnostics_by_week={},
        )

    units, selected_period_ids = _load_demand_units(
        session,
        demands=demands,
        project_ids=project_ids,
        business_resource_class_code=business_resource_class_code,
        task_filter_active=task_filter_active,
        allowed_task_ids=allowed_task_ids,
        allowed_task_codes_by_project=allowed_task_codes_by_project or {},
    )

    effective_start = start
    effective_end = end
    if effective_start is None and effective_end is None:
        effective_start, effective_end = _projection_window(units)
    if effective_start is None or effective_end is None:
        return MediumTermCompetencyProjection(
            window_start=effective_start,
            window_end=effective_end,
            skills_by_week={},
            combinations_by_week={},
            diagnostics_by_week={},
        )
    if effective_end < effective_start:
        effective_start, effective_end = effective_end, effective_start

    competency_rows = tuple(
        session.execute(
            select(Competency, ResourceClassConfig)
            .outerjoin(
                ResourceClassConfig,
                Competency.resource_class_code == ResourceClassConfig.code,
            )
            .order_by(Competency.sort_order, Competency.name, Competency.id)
        ).all()
    )
    wanted_group_class = _optional_text(competency_resource_class_code)
    displayed_competencies = tuple(
        (competency, resource_class)
        for competency, resource_class in competency_rows
        if (
            wanted_group_class is None
            or _optional_text(competency.resource_class_code) == wanted_group_class
        )
    )
    displayed_competency_ids = {row.id for row, _class in displayed_competencies}
    competency_name_by_id = {
        row.id: row.name for row, _class in competency_rows
    }

    relations = tuple(
        session.execute(
            select(
                ResourceCompetency.resource_id,
                ResourceCompetency.competency_id,
            )
        ).all()
    )
    resource_ids_by_competency: defaultdict[str, set[str]] = defaultdict(set)
    competencies_by_resource: defaultdict[str, set[str]] = defaultdict(set)
    for resource_id, competency_id in relations:
        resource_ids_by_competency[competency_id].add(resource_id)
        competencies_by_resource[resource_id].add(competency_id)

    resources = {
        row.id: row
        for row in session.scalars(select(Resource)).all()
    }
    capacity_by_week_resource = build_workforce_weekly_capacity_by_resource(
        queries,
        session,
        start=effective_start,
        end=effective_end,
    )

    first_week = effective_start - timedelta(days=effective_start.weekday())
    last_week = effective_end - timedelta(days=effective_end.weekday())
    skills_by_week: dict[date, tuple[MediumTermCompetencyWeekReadModel, ...]] = {}
    combinations_by_week: dict[
        date, tuple[MediumTermCompetencyCombinationWeekReadModel, ...]
    ] = {}
    diagnostics_by_week: dict[date, tuple[str, ...]] = {}

    cursor = first_week
    while cursor <= last_week:
        week_end = cursor + timedelta(days=6)
        requested_by_skill: defaultdict[str, _HoursAccumulator] = defaultdict(
            _HoursAccumulator
        )
        requested_by_combination: defaultdict[
            tuple[tuple[str, ...], str | None], _HoursAccumulator
        ] = defaultdict(_HoursAccumulator)
        week_diagnostics: list[str] = []

        for unit in units:
            hours, diagnostics, relevant = _unit_week_hours(
                unit,
                selected_period_ids,
                week_start=cursor,
                week_end=week_end,
            )
            if not relevant:
                continue
            if unit.unresolved_reference:
                if COMPETENCY_DIAGNOSTIC_REFERENCE_UNRESOLVED not in week_diagnostics:
                    week_diagnostics.append(
                        COMPETENCY_DIAGNOSTIC_REFERENCE_UNRESOLVED
                    )
                continue
            if not unit.competency_ids:
                continue
            for competency_id in unit.competency_ids:
                requested_by_skill[competency_id].add(hours, diagnostics)
            if len(unit.competency_ids) > 1:
                combo_ids = tuple(sorted(set(unit.competency_ids)))
                requested_by_combination[
                    (combo_ids, unit.required_resource_class_code)
                ].add(hours, diagnostics, count_line=True)

        resource_capacity = capacity_by_week_resource.get(cursor, {})
        skill_rows: list[MediumTermCompetencyWeekReadModel] = []
        for competency, resource_class in displayed_competencies:
            accumulator = requested_by_skill.get(
                competency.id,
                _HoursAccumulator(),
            )
            requested = accumulator.value()
            capacity_resource_ids = resource_ids_by_competency.get(
                competency.id,
                set(),
            )
            capacity = sum(
                (
                    resource_capacity.get(resource_id, Decimal("0.00"))
                    for resource_id in capacity_resource_ids
                ),
                Decimal("0.00"),
            ).quantize(Decimal("0.01"))
            diagnostics = list(accumulator.diagnostics or ())
            if resource_class is not None and not bool(resource_class.active):
                diagnostics.append(COMPETENCY_DIAGNOSTIC_GROUP_CLASS_INACTIVE)
            if capacity <= 0:
                diagnostics.append(COMPETENCY_DIAGNOSTIC_CAPACITY_ZERO)
            if requested is not None and requested > capacity:
                diagnostics.append(COMPETENCY_DIAGNOSTIC_CAPACITY_EXCEEDED)
            utilization = (
                (requested / capacity * Decimal("100")).quantize(Decimal("0.01"))
                if requested is not None and capacity > 0
                else None
            )
            state = (
                "unavailable"
                if requested is None
                else medium_term_capacity_state(capacity, requested)
            )
            skill_rows.append(
                MediumTermCompetencyWeekReadModel(
                    competency_id=competency.id,
                    competency_name=competency.name,
                    competency_active=bool(competency.active),
                    resource_class_code=_optional_text(
                        competency.resource_class_code
                    ),
                    resource_class_label=(
                        _optional_text(resource_class.label)
                        if resource_class is not None
                        else None
                    ),
                    resource_class_active=(
                        bool(resource_class.active)
                        if resource_class is not None
                        else None
                    ),
                    requested_hours=requested,
                    capacity_hours=capacity,
                    utilization=utilization,
                    state=state,
                    diagnostics=tuple(dict.fromkeys(diagnostics)),
                    qualifying_resource_count=sum(
                        1
                        for resource_id in capacity_resource_ids
                        if resource_id in resource_capacity
                    ),
                )
            )

        combination_rows: list[MediumTermCompetencyCombinationWeekReadModel] = []
        for (
            competency_ids,
            required_resource_class,
        ), accumulator in sorted(
            requested_by_combination.items(),
            key=lambda item: (item[0][0], item[0][1] or ""),
        ):
            if (
                wanted_group_class is not None
                and not any(
                    competency_id in displayed_competency_ids
                    for competency_id in competency_ids
                )
            ):
                continue
            candidate_ids = set(resource_capacity)
            for competency_id in competency_ids:
                candidate_ids &= resource_ids_by_competency.get(
                    competency_id,
                    set(),
                )
            if required_resource_class is not None:
                candidate_ids = {
                    resource_id
                    for resource_id in candidate_ids
                    if (
                        resource_id in resources
                        and _optional_text(resources[resource_id].resource_class)
                        == required_resource_class
                    )
                }
            common_capacity = sum(
                (
                    resource_capacity.get(resource_id, Decimal("0.00"))
                    for resource_id in candidate_ids
                ),
                Decimal("0.00"),
            ).quantize(Decimal("0.01"))
            requested = accumulator.value()
            diagnostics = list(accumulator.diagnostics or ())
            if requested is not None and requested > 0 and common_capacity <= 0:
                diagnostics.append(
                    COMPETENCY_DIAGNOSTIC_COMMON_QUALIFICATION_ZERO
                )
            elif (
                requested is not None
                and requested > common_capacity
            ):
                diagnostics.append(
                    COMPETENCY_DIAGNOSTIC_COMMON_QUALIFICATION_EXCEEDED
                )
            utilization = (
                (
                    requested
                    / common_capacity
                    * Decimal("100")
                ).quantize(Decimal("0.01"))
                if requested is not None and common_capacity > 0
                else None
            )
            state = (
                "unavailable"
                if requested is None
                else medium_term_capacity_state(common_capacity, requested)
            )
            combination_rows.append(
                MediumTermCompetencyCombinationWeekReadModel(
                    competency_ids=competency_ids,
                    competency_names=tuple(
                        competency_name_by_id.get(
                            competency_id,
                            competency_id,
                        )
                        for competency_id in competency_ids
                    ),
                    required_resource_class_code=required_resource_class,
                    requested_hours=requested,
                    common_capacity_hours=common_capacity,
                    utilization=utilization,
                    state=state,
                    diagnostics=tuple(dict.fromkeys(diagnostics)),
                    demand_line_count=accumulator.line_count,
                )
            )

        skills_by_week[cursor] = tuple(skill_rows)
        combinations_by_week[cursor] = tuple(combination_rows)
        diagnostics_by_week[cursor] = tuple(dict.fromkeys(week_diagnostics))
        cursor += timedelta(days=7)

    return MediumTermCompetencyProjection(
        window_start=effective_start,
        window_end=effective_end,
        skills_by_week=skills_by_week,
        combinations_by_week=combinations_by_week,
        diagnostics_by_week=diagnostics_by_week,
    )
