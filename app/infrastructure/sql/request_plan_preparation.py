from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
import json
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...domain.active_days import normalize_active_day_target, split_total_workforce_hours
from ...domain.approval_envelope import EnvelopeEntryIdentity
from ...domain.confirmation import CONFIRMATION_CONFIRMED, normalize_confirmation
from ...domain.demand_periods import PERIOD_KIND_CUMULATIVE, resolve_period_authority
from .demand_period_models import (
    WorkforceRequestPeriod,
    WorkforceRequestPeriodRequirement,
    WorkforceRequestPeriodSelection,
)
from .operational_choice_repository import SqlRequestOperationalChoiceRepository
from .segment_asset_guard import segment_asset_dependencies
from .approval_revision_models import RequestApprovalRevision
from .planning_window_override_repository import SqlPlanningWindowOverrideRepository
from .models import (
    Competency,
    RequestLine,
    RequestLineCompetency,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    WorkforceRequestCompetency,
    WorkPackage,
)


LOCKED_REQUIREMENT_REMOVAL = "LOCKED_REQUIREMENT_REMOVAL"
LOCKED_SHIFT_OUTSIDE_WINDOW = "LOCKED_SHIFT_OUTSIDE_WINDOW"
LOCKED_HOURS_EXCEED_BUDGET = "LOCKED_HOURS_EXCEED_BUDGET"
SEGMENT_ASSET_REQUIREMENT_REMOVAL = "SEGMENT_ASSET_REQUIREMENT_REMOVAL"
SEGMENT_ASSET_OUTSIDE_WINDOW = "SEGMENT_ASSET_OUTSIDE_WINDOW"
SEGMENT_ASSET_PROJECT_CHANGE = "SEGMENT_ASSET_PROJECT_CHANGE"


def _text(value: object) -> str:
    return str(value or "").strip()


@dataclass(frozen=True, slots=True)
class PreparedRequirementSpec:
    """One effective requirement candidate shared by materialization and preview."""

    key: tuple[str, ...]
    base_key: tuple[str, ...]
    approved_entry_key: str
    source_request_line_id: str | None
    source_period_id: str | None
    start_date: date
    end_date: date
    planned_hours: Decimal
    desired_active_days: int | None
    confirmation: str
    proposed_resource_id: str | None
    description: str
    source_effort_id: str | None
    required_resource_class: str | None
    required_competency: str | None
    competency_ids: tuple[str, ...]
    slot_index: int = 0


@dataclass(frozen=True, slots=True)
class PreparedRequestPlan:
    specs: tuple[PreparedRequirementSpec, ...]
    unresolved_groups: int = 0
    approval_revision_id: str | None = None
    operational_version: int | None = None
    project_id: str | None = None
    priority: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedRequirementMatch:
    spec: PreparedRequirementSpec
    requirement: ResourceRequirement | None


@dataclass(frozen=True, slots=True)
class LockedPlanningConflict:
    code: str
    requirement_id: str
    spec_key: tuple[str, ...] | None
    shift_ids: tuple[str, ...]
    message: str


class SqlRequestPlanPreparer:
    """Canonical SQL-to-planning preparation for request materialization and preview.

    This component deliberately does not mutate planning state. It translates the
    candidate request definition plus active operational selections into effective
    requirement specifications and evaluates locked-work compatibility.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _active_periods(self, request_id: str) -> list[WorkforceRequestPeriod]:
        return list(
            self._session.scalars(
                select(WorkforceRequestPeriod)
                .where(
                    WorkforceRequestPeriod.workforce_request_id == request_id,
                    WorkforceRequestPeriod.active == true(),
                )
                .order_by(
                    WorkforceRequestPeriod.request_line_id,
                    WorkforceRequestPeriod.sequence,
                    WorkforceRequestPeriod.created_at,
                    WorkforceRequestPeriod.id,
                )
            ).all()
        )

    def _selections(self, request_id: str) -> dict[tuple[str, str], str]:
        rows = self._session.scalars(
            select(WorkforceRequestPeriodSelection).where(
                WorkforceRequestPeriodSelection.workforce_request_id == request_id
            )
        ).all()
        return {
            (row.request_line_id, row.alternative_group): row.period_id
            for row in rows
        }

    def _active_lines(self, request_id: str) -> list[RequestLine]:
        return list(
            self._session.scalars(
                select(RequestLine)
                .where(
                    RequestLine.workforce_request_id == request_id,
                    RequestLine.active == true(),
                )
                .order_by(RequestLine.position, RequestLine.id)
            ).all()
        )

    def _request_competencies(
        self,
        request_id: str,
    ) -> tuple[str, ...]:
        return tuple(
            self._session.scalars(
                select(WorkforceRequestCompetency.competency_id)
                .where(
                    WorkforceRequestCompetency.workforce_request_id == request_id
                )
                .order_by(WorkforceRequestCompetency.competency_id)
            ).all()
        )

    def _line_competencies(
        self,
        line_ids: Sequence[str],
    ) -> dict[str, tuple[str, ...]]:
        identifiers = tuple(line_ids)
        if not identifiers:
            return {}
        rows = self._session.execute(
            select(
                RequestLineCompetency.request_line_id,
                RequestLineCompetency.competency_id,
            )
            .where(RequestLineCompetency.request_line_id.in_(identifiers))
            .order_by(
                RequestLineCompetency.request_line_id,
                RequestLineCompetency.competency_id,
            )
        ).all()
        grouped: dict[str, list[str]] = defaultdict(list)
        for line_id, competency_id in rows:
            grouped[line_id].append(competency_id)
        return {
            line_id: tuple(dict.fromkeys(values))
            for line_id, values in grouped.items()
        }

    def _line_work_package_refs(
        self,
        lines: Sequence[RequestLine],
    ) -> dict[str, str | None]:
        identifiers = {
            line.work_package_id
            for line in lines
            if line.work_package_id
        }
        packages = (
            self._session.scalars(
                select(WorkPackage).where(WorkPackage.id.in_(identifiers))
            ).all()
            if identifiers
            else []
        )
        refs = {
            package.id: _text(package.legacy_effort_id) or package.id
            for package in packages
        }
        return {
            line.id: refs.get(line.work_package_id)
            if line.work_package_id
            else None
            for line in lines
        }

    def _prepare_line_mode(
        self,
        request: WorkforceRequest,
        periods: list[WorkforceRequestPeriod],
        selections: dict[tuple[str, str], str],
    ) -> PreparedRequestPlan:
        lines = self._active_lines(request.id)
        if not lines:
            raise ValueError(
                "Une demande multi-lignes doit conserver au moins une ligne active."
            )

        periods_by_line: dict[str, list[WorkforceRequestPeriod]] = defaultdict(list)
        for period in periods:
            if period.request_line_id:
                periods_by_line[period.request_line_id].append(period)

        competency_ids = self._line_competencies(tuple(line.id for line in lines))
        work_package_refs = self._line_work_package_refs(lines)
        specs: list[PreparedRequirementSpec] = []
        request_competency_ids = self._request_competencies(request.id)
        unresolved = 0

        for line in lines:
            if _text(line.kind) == "ASSET":
                continue  # Asset requirements use exclusive days, never hourly shifts.
            if _text(line.kind) != "WORKFORCE":
                raise ValueError(
                    f"La ligne {line.id} de type {line.kind} ne peut pas être matérialisée."
                )
            line_periods = periods_by_line.get(line.id, [])
            required_text = _text(line.required_competencies_snapshot) or None
            required_resource_class = _text(line.required_resource_class) or None

            if line_periods:
                groups = {
                    _text(period.alternative_group)
                    for period in line_periods
                    if period.alternative_group
                }
                unresolved += sum(
                    1
                    for group in groups
                    if (line.id, group) not in selections
                )
                effective = [
                    period
                    for period in line_periods
                    if period.kind == PERIOD_KIND_CUMULATIVE
                    or (
                        period.alternative_group is not None
                        and selections.get(
                            (line.id, _text(period.alternative_group))
                        )
                        == period.id
                    )
                ]
                for period in effective:
                    resolved = resolve_period_authority(
                        contract_version=period.inheritance_contract_version,
                        stored_resource_count=period.resource_count,
                        stored_confirmation=period.confirmation,
                        stored_proposed_resource=period.proposed_resource_id,
                        confirmation_mode=period.confirmation_mode,
                        proposed_resource_mode=period.proposed_resource_mode,
                        same_as_period_id=period.same_as_period_key,
                        master_resource_count=line.slot_count,
                        master_confirmation=line.confirmation,
                        master_proposed_resource=line.proposed_resource_id,
                    )
                    desired = resolved.resource_count
                    split_hours = split_total_workforce_hours(period.hours, desired)
                    base_key = ("PERIOD", line.id, period.period_key)
                    entry_key = EnvelopeEntryIdentity(
                        line_id=line.id,
                        period_key=period.period_key,
                    ).stable_key
                    for index, hours in enumerate(split_hours):
                        specs.append(
                            PreparedRequirementSpec(
                                key=(base_key if desired == 1 else (*base_key, str(index))),
                                base_key=base_key,
                                approved_entry_key=entry_key,
                                source_request_line_id=line.id,
                                source_period_id=period.id,
                                start_date=period.start_date,
                                end_date=period.end_date,
                                planned_hours=Decimal(str(hours)).quantize(
                                    Decimal("0.01")
                                ),
                                desired_active_days=period.desired_active_days,
                                confirmation=normalize_confirmation(
                                    resolved.confirmation,
                                    default=CONFIRMATION_CONFIRMED,
                                ),
                                proposed_resource_id=(
                                    resolved.proposed_resource if index == 0 else None
                                ),
                                description=(
                                    _text(period.note)
                                    or _text(line.description)
                                    or _text(line.erp_task_label)
                                    or _text(request.description)
                                    or "Période approuvée"
                                ),
                                source_effort_id=work_package_refs.get(line.id),
                                required_resource_class=required_resource_class,
                                required_competency=required_text,
                                competency_ids=competency_ids.get(line.id, ()),
                                slot_index=index,
                            )
                        )
                continue

            if line.desired_start is None:
                raise ValueError(
                    f"La date de début de la ligne {line.id} est requise pour la matérialisation."
                )
            if line.estimated_hours is None or line.estimated_hours <= 0:
                raise ValueError(
                    f"Les heures de la ligne {line.id} sont requises pour la matérialisation."
                )
            base_key = ("LINE", line.id)
            specs.append(
                PreparedRequirementSpec(
                    key=base_key,
                    base_key=base_key,
                    approved_entry_key=EnvelopeEntryIdentity(
                        line_id=line.id
                    ).stable_key,
                    source_request_line_id=line.id,
                    source_period_id=None,
                    start_date=line.desired_start,
                    end_date=line.desired_end or line.desired_start,
                    planned_hours=Decimal(str(line.estimated_hours)).quantize(
                        Decimal("0.01")
                    ),
                    desired_active_days=(
                        int(line.desired_active_days)
                        if line.desired_active_days is not None
                        else None
                    ),
                    confirmation=normalize_confirmation(
                        line.confirmation,
                        default=CONFIRMATION_CONFIRMED,
                    ),
                    proposed_resource_id=line.proposed_resource_id,
                    description=(
                        _text(line.description)
                        or _text(line.erp_task_label)
                        or _text(request.description)
                        or "Besoin approuvé"
                    ),
                    source_effort_id=work_package_refs.get(line.id),
                    required_resource_class=required_resource_class,
                    required_competency=required_text,
                    competency_ids=competency_ids.get(line.id, ()),
                )
            )

        keys = [spec.key for spec in specs]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "Deux besoins effectifs d'une même demande partagent la même identité de ligne/période."
            )
        return PreparedRequestPlan(tuple(specs), unresolved_groups=unresolved)

    def _prepare_legacy_periods(
        self,
        request: WorkforceRequest,
        periods: list[WorkforceRequestPeriod],
        selections: dict[tuple[str, str], str],
    ) -> PreparedRequestPlan:
        line_id = request.id
        effective = [
            period
            for period in periods
            if period.kind == PERIOD_KIND_CUMULATIVE
            or (
                period.alternative_group is not None
                and selections.get(
                    (line_id, _text(period.alternative_group))
                )
                == period.id
            )
        ]
        groups = {
            _text(period.alternative_group)
            for period in periods
            if period.alternative_group
        }
        unresolved = sum(
            1 for group in groups if (line_id, group) not in selections
        )

        request_competency_ids = self._request_competencies(request.id)
        specs: list[PreparedRequirementSpec] = []
        for period in effective:
            resolved = resolve_period_authority(
                contract_version=period.inheritance_contract_version,
                stored_resource_count=period.resource_count,
                stored_confirmation=period.confirmation,
                stored_proposed_resource=period.proposed_resource_id,
                confirmation_mode=period.confirmation_mode,
                proposed_resource_mode=period.proposed_resource_mode,
                same_as_period_id=period.same_as_period_key,
                master_resource_count=request.resource_count,
                master_confirmation=request.confirmation,
                master_proposed_resource=request.proposed_resource_id,
            )
            desired = resolved.resource_count
            split_hours = split_total_workforce_hours(period.hours, desired)
            base_key = ("PERIOD", line_id, period.period_key)
            entry_key = EnvelopeEntryIdentity(
                line_id=line_id,
                period_key=period.period_key,
            ).stable_key
            for index, hours in enumerate(split_hours):
                specs.append(
                    PreparedRequirementSpec(
                        key=(*base_key, str(index)),
                        base_key=base_key,
                        approved_entry_key=entry_key,
                        source_request_line_id=line_id,
                        source_period_id=period.id,
                        start_date=period.start_date,
                        end_date=period.end_date,
                        planned_hours=Decimal(str(hours)).quantize(
                            Decimal("0.01")
                        ),
                        desired_active_days=period.desired_active_days,
                        confirmation=normalize_confirmation(
                            resolved.confirmation,
                            default=CONFIRMATION_CONFIRMED,
                        ),
                        proposed_resource_id=(
                            resolved.proposed_resource
                            if index == 0
                            else None
                        ),
                        description=(
                            _text(period.note)
                            or _text(request.erp_task_label)
                            or _text(request.description)
                            or "Période approuvée"
                        ),
                        source_effort_id=None,
                        required_resource_class=None,
                        required_competency=(
                            _text(request.required_competencies) or None
                        ),
                        competency_ids=request_competency_ids,
                        slot_index=index,
                    )
                )
        return PreparedRequestPlan(tuple(specs), unresolved_groups=unresolved)

    def _prepare_legacy_simple(
        self,
        request: WorkforceRequest,
        current: Sequence[ResourceRequirement],
    ) -> PreparedRequestPlan:
        if request.desired_start is None:
            raise ValueError(
                "La date de début de la demande est requise pour synchroniser les besoins."
            )
        desired = max(int(request.resource_count or 1), 1)
        if request.estimated_hours is not None and request.estimated_hours > 0:
            total_hours = Decimal(request.estimated_hours)
        else:
            total_hours = sum(
                (
                    row.planned_hours
                    for row in current
                    if row.planned_hours > 0
                ),
                Decimal("0"),
            )
            if total_hours <= 0:
                if request.estimated_days is not None and request.estimated_days > 0:
                    raise ValueError(
                        "Les jours estimés guident la répartition mais ne définissent pas les heures. "
                        "Renseigne les heures estimées avant d'approuver cette demande."
                    )
                raise ValueError(
                    "Les heures estimées sont requises pour matérialiser une nouvelle demande."
                )

        request_competency_ids = self._request_competencies(request.id)
        target_days = normalize_active_day_target(
            request.estimated_days,
            start=request.desired_start,
            end=request.desired_end or request.desired_start,
            field="Les jours estimés de la demande",
        )
        split_hours = split_total_workforce_hours(total_hours, desired)
        base_key = ("LINE", request.id)
        entry_key = EnvelopeEntryIdentity(line_id=request.id).stable_key
        specs = tuple(
            PreparedRequirementSpec(
                key=(*base_key, str(index)),
                base_key=base_key,
                approved_entry_key=entry_key,
                source_request_line_id=request.id,
                source_period_id=None,
                start_date=request.desired_start,
                end_date=request.desired_end or request.desired_start,
                planned_hours=Decimal(str(hours)).quantize(Decimal("0.01")),
                desired_active_days=target_days,
                confirmation=normalize_confirmation(
                    request.confirmation,
                    default=CONFIRMATION_CONFIRMED,
                ),
                proposed_resource_id=(
                    request.proposed_resource_id if index == 0 else None
                ),
                description=(
                    _text(request.erp_task_label)
                    or _text(request.description)
                    or "Ressource additionnelle"
                ),
                source_effort_id=None,
                required_resource_class=None,
                required_competency=(
                    _text(request.required_competencies) or None
                ),
                competency_ids=request_competency_ids,
                slot_index=index,
            )
            for index, hours in enumerate(split_hours)
        )
        return PreparedRequestPlan(specs)

    def prepare(
        self,
        request: WorkforceRequest,
        *,
        current: Sequence[ResourceRequirement] = (),
    ) -> PreparedRequestPlan:
        periods = self._active_periods(request.id)
        selections = self._selections(request.id)
        if bool(request.line_mode):
            return self._prepare_line_mode(request, periods, selections)
        if periods:
            return self._prepare_legacy_periods(
                request,
                periods,
                selections,
            )
        return self._prepare_legacy_simple(request, current)

    def prepare_active(
        self,
        request: WorkforceRequest,
        *,
        current: Sequence[ResourceRequirement] = (),
    ) -> PreparedRequestPlan:
        """Prepare the active plan from immutable approval plus operational choices."""

        choices = SqlRequestOperationalChoiceRepository(
            self._session
        ).state_for_request_id(request.id)
        if choices is None:
            raise ValueError(
                "Aucune révision approuvée active ne permet de préparer le plan opérationnel."
            )
        revision = self._session.get(
            RequestApprovalRevision,
            choices.approval_revision_id,
        )
        if revision is None:
            raise ValueError("La révision approuvée active est introuvable.")
        payload = json.loads(revision.payload_text)
        authorization = payload.get("authorization", {})
        entries = authorization.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("Le snapshot approuvé est invalide.")
        request_snapshot = payload.get("request", {})
        line_mode = bool(request_snapshot.get("line_mode"))

        competency_ids = {
            _text(value)
            for row in entries
            for value in (row.get("competency_ids") or [])
            if _text(value)
        }
        competencies = (
            self._session.scalars(
                select(Competency).where(Competency.id.in_(competency_ids))
            ).all()
            if competency_ids
            else []
        )
        competency_names = {row.id: row.name for row in competencies}

        current_by_entry: dict[str, list[ResourceRequirement]] = defaultdict(list)
        for requirement in current:
            if _text(requirement.approved_entry_key):
                current_by_entry[_text(requirement.approved_entry_key)].append(requirement)
        for rows in current_by_entry.values():
            rows.sort(key=lambda row: (row.created_at, row.id))

        specs: list[PreparedRequirementSpec] = []
        unresolved_groups = 0
        groups_seen: set[str] = set()
        groups_selected: set[str] = set()

        for row in entries:
            if _text(row.get("line_kind")).upper() == "ASSET":
                continue
            identity_key = _text(row.get("identity"))
            if not identity_key:
                continue
            identity_parts = json.loads(identity_key)
            if not isinstance(identity_parts, list) or len(identity_parts) != 3:
                raise ValueError("Une identité approuvée est invalide.")
            identity_kind, line_id, period_key = identity_parts
            line_id = _text(line_id)
            period_key = _text(period_key) or None
            kind = _text(row.get("kind")).upper()
            group = _text(row.get("group")) or None
            if group:
                groups_seen.add(group)
            if kind == "ALTERNATIVE":
                selected = choices.selections.get(group or "")
                if selected != identity_key:
                    continue
                if group:
                    groups_selected.add(group)

            source_period_id = _text(row.get("source_period_id")) or None
            source_period = (
                self._session.get(WorkforceRequestPeriod, source_period_id)
                if source_period_id
                else None
            )
            source_line = (
                self._session.get(RequestLine, line_id)
                if line_id
                else None
            )
            previous_rows = current_by_entry.get(identity_key, [])
            previous_description = next(
                (
                    _text(item.description)
                    for item in previous_rows
                    if _text(item.description)
                ),
                "",
            )
            description = (
                _text(source_period.note if source_period is not None else None)
                or _text(source_line.description if source_line is not None else None)
                or previous_description
                or _text(request.description)
                or "Besoin approuvé"
            )
            ids = tuple(
                sorted(
                    {
                        _text(value)
                        for value in (row.get("competency_ids") or [])
                        if _text(value)
                    }
                )
            )
            required_text = ", ".join(
                competency_names[value]
                for value in ids
                if value in competency_names
            ) or None
            start_date = date.fromisoformat(_text(row.get("start_date")))
            end_date = date.fromisoformat(_text(row.get("end_date")))
            total_hours = Decimal(
                str(
                    choices.budget_overrides.get(
                        identity_key,
                        _text(row.get("hours")),
                    )
                )
            ).quantize(Decimal("0.01"))
            slot_count = (
                max(int(row.get("slot_count") or 1), 1)
                if period_key is not None
                else (1 if line_mode else max(int(row.get("slot_count") or 1), 1))
            )
            split_hours = split_total_workforce_hours(total_hours, slot_count)
            base_key = (
                ("PERIOD", line_id, period_key)
                if period_key is not None
                else ("LINE", line_id)
            )
            confirmation = normalize_confirmation(
                choices.confirmations.get(identity_key, row.get("confirmation")),
                default=CONFIRMATION_CONFIRMED,
            )
            for index, hours in enumerate(split_hours):
                spec_key = (
                    base_key
                    if line_mode and slot_count == 1
                    else (*base_key, str(index))
                )
                specs.append(
                    PreparedRequirementSpec(
                        key=spec_key,
                        base_key=base_key,
                        approved_entry_key=identity_key,
                        source_request_line_id=line_id,
                        source_period_id=source_period_id,
                        start_date=start_date,
                        end_date=end_date,
                        planned_hours=Decimal(str(hours)).quantize(
                            Decimal("0.01")
                        ),
                        desired_active_days=(
                            int(row["desired_active_days"])
                            if row.get("desired_active_days") is not None
                            else None
                        ),
                        confirmation=confirmation,
                        proposed_resource_id=(
                            _text(row.get("proposed_resource_id"))
                            if index == 0
                            else None
                        )
                        or None,
                        description=description,
                        source_effort_id=(
                (
                    _text(work_package.legacy_effort_id) or work_package.id
                )
                if (
                    (work_package_ref := _text(row.get("work_package_ref")))
                    and (
                        work_package := self._session.get(
                            WorkPackage,
                            work_package_ref,
                        )
                    )
                    is not None
                )
                else (_text(row.get("work_package_ref")) or None)
            ),
                        required_resource_class=(
                            _text(row.get("required_resource_class")) or None
                        ),
                        required_competency=required_text,
                        competency_ids=ids,
                        slot_index=index,
                    )
                )

        unresolved_groups = len(groups_seen - groups_selected)
        effective_specs = tuple(specs)
        if current and effective_specs:
            matches, _obsolete = self.match_current(request, current, effective_specs)
            applicable_matches = tuple(
                match
                for match in matches
                if match.requirement is not None
                and _text(match.requirement.approved_entry_key)
                == match.spec.approved_entry_key
            )
            overrides = SqlPlanningWindowOverrideRepository(
                self._session
            ).active_by_requirement_ids(
                (match.requirement.id for match in applicable_matches),
                approval_revision_id=revision.id,
            )
            effective_windows = {}
            resolver = SqlPlanningWindowOverrideRepository(self._session)
            for match in applicable_matches:
                requirement = match.requirement
                if requirement is None:
                    continue
                override = overrides.get(requirement.id)
                if override is None:
                    continue
                effective_windows[match.spec.key] = resolver.effective_window_for_requirement(
                    requirement,
                    override,
                    approval_revision_id=revision.id,
                    approved_entry_key=match.spec.approved_entry_key,
                    approved_start_date=match.spec.start_date,
                    approved_end_date=match.spec.end_date,
                )
            if effective_windows:
                effective_specs = tuple(
                    replace(
                        spec,
                        start_date=effective_windows[spec.key].start_date,
                        end_date=effective_windows[spec.key].end_date,
                    )
                    if spec.key in effective_windows
                    else spec
                    for spec in effective_specs
                )

        return PreparedRequestPlan(
            specs=effective_specs,
            unresolved_groups=unresolved_groups,
            approval_revision_id=revision.id,
            operational_version=choices.version,
            project_id=_text(request_snapshot.get("project_id")) or None,
            priority=(
                _text(request_snapshot.get("priority"))
                or next(
                    (
                        _text(requirement.priority)
                        for requirement in current
                        if _text(requirement.priority)
                    ),
                    "",
                )
                or None
            ),
        )

    def _period_identity_by_requirement(
        self,
        requirement_ids: set[str],
    ) -> dict[str, tuple[str, str]]:
        if not requirement_ids:
            return {}
        rows = self._session.execute(
            select(
                WorkforceRequestPeriodRequirement.resource_requirement_id,
                WorkforceRequestPeriod.request_line_id,
                WorkforceRequestPeriod.period_key,
            )
            .join(
                WorkforceRequestPeriod,
                WorkforceRequestPeriodRequirement.period_id
                == WorkforceRequestPeriod.id,
            )
            .where(
                WorkforceRequestPeriodRequirement.resource_requirement_id.in_(
                    requirement_ids
                )
            )
        ).all()
        return {
            requirement_id: (_text(line_id), period_key)
            for requirement_id, line_id, period_key in rows
        }

    def _current_by_base_key(
        self,
        request: WorkforceRequest,
        current: Sequence[ResourceRequirement],
    ) -> dict[tuple[str, ...], list[ResourceRequirement]]:
        period_identity = self._period_identity_by_requirement(
            {row.id for row in current}
        )
        grouped: dict[tuple[str, ...], list[ResourceRequirement]] = defaultdict(list)
        for requirement in current:
            identity = period_identity.get(requirement.id)
            if identity is not None:
                line_id = identity[0] or _text(
                    requirement.source_request_line_id
                ) or request.id
                key = ("PERIOD", line_id, identity[1])
            else:
                line_id = (
                    _text(requirement.source_request_line_id) or request.id
                )
                key = ("LINE", line_id)
            grouped[key].append(requirement)
        for rows in grouped.values():
            rows.sort(
                key=lambda row: (
                    0 if row.assigned_resource_id else 1,
                    row.created_at,
                    row.id,
                )
            )
        return grouped

    def match_current(
        self,
        request: WorkforceRequest,
        current: Sequence[ResourceRequirement],
        specs: Sequence[PreparedRequirementSpec],
    ) -> tuple[tuple[PreparedRequirementMatch, ...], tuple[ResourceRequirement, ...]]:
        current_by_key = self._current_by_base_key(request, current)
        specs_by_key: dict[tuple[str, ...], list[PreparedRequirementSpec]] = defaultdict(list)
        for spec in specs:
            specs_by_key[spec.base_key].append(spec)
        for rows in specs_by_key.values():
            rows.sort(key=lambda row: (row.slot_index, row.key))

        matches: list[PreparedRequirementMatch] = []
        obsolete: list[ResourceRequirement] = []
        all_keys = set(current_by_key) | set(specs_by_key)
        for base_key in sorted(all_keys):
            current_rows = current_by_key.get(base_key, [])
            desired_rows = specs_by_key.get(base_key, [])
            for index, spec in enumerate(desired_rows):
                matches.append(
                    PreparedRequirementMatch(
                        spec=spec,
                        requirement=(
                            current_rows[index]
                            if index < len(current_rows)
                            else None
                        ),
                    )
                )
            obsolete.extend(current_rows[len(desired_rows):])
        return tuple(matches), tuple(obsolete)

    def locked_conflicts(
        self,
        request: WorkforceRequest,
        current: Sequence[ResourceRequirement],
        specs: Sequence[PreparedRequirementSpec],
        *,
        target_project_id: str | None = None,
    ) -> tuple[LockedPlanningConflict, ...]:
        matches, obsolete = self.match_current(request, current, specs)
        requirement_ids = {row.id for row in current}
        locked_rows = (
            self._session.scalars(
                select(Shift)
                .where(
                    Shift.resource_requirement_id.in_(requirement_ids),
                    Shift.locked == true(),
                )
                .order_by(Shift.resource_requirement_id, Shift.work_date, Shift.id)
            ).all()
            if requirement_ids
            else []
        )
        locked_by_requirement: dict[str, list[Shift]] = defaultdict(list)
        for shift in locked_rows:
            locked_by_requirement[shift.resource_requirement_id].append(shift)

        conflicts: list[LockedPlanningConflict] = []
        for requirement in obsolete:
            locked = locked_by_requirement.get(requirement.id, [])
            if not locked:
                continue
            conflicts.append(
                LockedPlanningConflict(
                    code=LOCKED_REQUIREMENT_REMOVAL,
                    requirement_id=requirement.id,
                    spec_key=None,
                    shift_ids=tuple(row.id for row in locked),
                    message=(
                        "La réapprobation supprimerait un besoin contenant des quarts verrouillés. "
                        "Libère ou déplace ces quarts manuels avant d'approuver."
                    ),
                )
            )

        for match in matches:
            requirement = match.requirement
            if requirement is None:
                continue
            locked = locked_by_requirement.get(requirement.id, [])
            if not locked:
                continue
            outside = [
                shift
                for shift in locked
                if shift.work_date < match.spec.start_date
                or shift.work_date > match.spec.end_date
            ]
            if outside:
                conflicts.append(
                    LockedPlanningConflict(
                        code=LOCKED_SHIFT_OUTSIDE_WINDOW,
                        requirement_id=requirement.id,
                        spec_key=match.spec.key,
                        shift_ids=tuple(row.id for row in outside),
                        message=(
                            "La nouvelle fenêtre d'un besoin exclut un quart verrouillé existant. "
                            "Libère ou déplace ce quart avant d'approuver."
                        ),
                    )
                )
                continue
            locked_hours = sum(
                (shift.hours for shift in locked),
                Decimal("0"),
            )
            if locked_hours > match.spec.planned_hours + Decimal("0.001"):
                conflicts.append(
                    LockedPlanningConflict(
                        code=LOCKED_HOURS_EXCEED_BUDGET,
                        requirement_id=requirement.id,
                        spec_key=match.spec.key,
                        shift_ids=tuple(row.id for row in locked),
                        message=(
                            "Les heures verrouillées dépassent les heures prévues par le nouveau besoin. "
                            "Réduis ou libère les quarts manuels avant d'approuver."
                        ),
                    )
                )
        dependencies_by_requirement: dict[str, list] = defaultdict(list)
        for dependency in segment_asset_dependencies(
            self._session,
            tuple(requirement_ids),
        ):
            dependencies_by_requirement[
                dependency.resource_requirement_id
            ].append(dependency)

        for requirement in obsolete:
            dependencies = dependencies_by_requirement.get(requirement.id, [])
            if dependencies:
                conflicts.append(
                    LockedPlanningConflict(
                        code=SEGMENT_ASSET_REQUIREMENT_REMOVAL,
                        requirement_id=requirement.id,
                        spec_key=None,
                        shift_ids=tuple(
                            row.allocation_id for row in dependencies
                        ),
                        message=(
                            "La réapprobation supprimerait un segment portant une "
                            "réservation d'actif. Libère ou adapte explicitement cette "
                            "réservation avant d'approuver."
                        ),
                    )
                )

        for match in matches:
            requirement = match.requirement
            if requirement is None:
                continue
            dependencies = dependencies_by_requirement.get(requirement.id, [])
            if not dependencies:
                continue
            if (
                target_project_id
                and any(
                    row.project_id != target_project_id
                    for row in dependencies
                )
            ):
                conflicts.append(
                    LockedPlanningConflict(
                        code=SEGMENT_ASSET_PROJECT_CHANGE,
                        requirement_id=requirement.id,
                        spec_key=match.spec.key,
                        shift_ids=tuple(
                            row.allocation_id for row in dependencies
                        ),
                        message=(
                            "La réapprobation changerait le projet d'un segment portant "
                            "une réservation d'actif. Libère cette réservation avant "
                            "d'approuver."
                        ),
                    )
                )
                continue
            outside = [
                row
                for row in dependencies
                if row.start_date < match.spec.start_date
                or row.end_date > match.spec.end_date
            ]
            if outside:
                conflicts.append(
                    LockedPlanningConflict(
                        code=SEGMENT_ASSET_OUTSIDE_WINDOW,
                        requirement_id=requirement.id,
                        spec_key=match.spec.key,
                        shift_ids=tuple(
                            row.allocation_id for row in outside
                        ),
                        message=(
                            "La nouvelle fenêtre du segment exclut une réservation "
                            "d'actif existante. Libère ou adapte explicitement cette "
                            "réservation avant d'approuver."
                        ),
                    )
                )

        return tuple(conflicts)

    def assert_locked_compatible(
        self,
        request: WorkforceRequest,
        current: Sequence[ResourceRequirement],
        specs: Sequence[PreparedRequirementSpec],
        *,
        target_project_id: str | None = None,
    ) -> None:
        conflicts = self.locked_conflicts(
            request,
            current,
            specs,
            target_project_id=target_project_id,
        )
        if conflicts:
            raise ValueError(conflicts[0].message)
