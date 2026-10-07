from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Sequence

from .active_days import normalize_active_day_target
from .confirmation import normalize_confirmation


PERIOD_KIND_CUMULATIVE = "CUMULATIVE"
PERIOD_KIND_ALTERNATIVE = "ALTERNATIVE"
VALID_PERIOD_KINDS = {PERIOD_KIND_CUMULATIVE, PERIOD_KIND_ALTERNATIVE}

PERIOD_INHERITANCE_CONTRACT_VERSION = 1
CONFIRMATION_MODE_INHERIT_MASTER = "INHERIT_MASTER"
CONFIRMATION_MODE_EXPLICIT = "EXPLICIT"
VALID_CONFIRMATION_MODES = {CONFIRMATION_MODE_INHERIT_MASTER, CONFIRMATION_MODE_EXPLICIT}
PROPOSED_RESOURCE_MODE_INHERIT_MASTER = "INHERIT_MASTER"
PROPOSED_RESOURCE_MODE_EXPLICIT = "EXPLICIT"
PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD = "SAME_AS_PERIOD"
VALID_PROPOSED_RESOURCE_MODES = {
    PROPOSED_RESOURCE_MODE_INHERIT_MASTER,
    PROPOSED_RESOURCE_MODE_EXPLICIT,
    PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
}
PERIOD_PROVENANCE_MASTER = "MASTER"
PERIOD_PROVENANCE_EXPLICIT = "EXPLICIT"
PERIOD_PROVENANCE_LEGACY = "LEGACY_PERIOD"
PERIOD_PROVENANCE_SAME_AS = "SAME_AS_PERIOD"



@dataclass(frozen=True, slots=True)
class DemandPeriodDefinition:
    """One requested work period independent of persistence and UI.

    ``hours`` is the total workforce effort for the period. ``resource_count`` is the
    desired parallelism and never multiplies that effort. ``desired_active_days`` is a
    distribution target for flexible planning, not an alternate hours formula.
    """

    period_id: str
    start_date: date
    end_date: date
    hours: float | None
    kind: str = PERIOD_KIND_CUMULATIVE
    alternative_group: str | None = None
    confirmation: str = "Tentative"
    confirmation_mode: str | None = None
    proposed_resource: str | None = None
    proposed_resource_mode: str | None = None
    same_as_period_id: str | None = None
    resource_count: int = 1
    desired_active_days: int | None = None
    note: str | None = None


def _text(value: object) -> str:
    return str(value or "").strip()


def normalized_confirmation_mode(value: object) -> str:
    mode = _text(value).upper() or CONFIRMATION_MODE_EXPLICIT
    if mode not in VALID_CONFIRMATION_MODES:
        raise ValueError(f"Mode de confirmation de période non supporté: {value}")
    return mode


def normalized_proposed_resource_mode(value: object) -> str:
    mode = _text(value).upper() or PROPOSED_RESOURCE_MODE_EXPLICIT
    if mode not in VALID_PROPOSED_RESOURCE_MODES:
        raise ValueError(f"Mode de ressource proposée non supporté: {value}")
    return mode


@dataclass(frozen=True, slots=True)
class PeriodAuthorityResolution:
    resource_count: int
    resource_count_provenance: str
    confirmation: str
    confirmation_mode: str | None
    confirmation_provenance: str
    proposed_resource: str | None
    proposed_resource_mode: str | None
    proposed_resource_provenance: str
    same_as_period_id: str | None = None
    same_as_root_period_id: str | None = None
    same_as_state: str | None = None


def resolve_period_authority(
    *,
    contract_version: int | None,
    stored_resource_count: int,
    stored_confirmation: str,
    stored_proposed_resource: str | None,
    confirmation_mode: str | None,
    proposed_resource_mode: str | None,
    same_as_period_id: str | None,
    master_resource_count: int | None,
    master_confirmation: str | None,
    master_proposed_resource: str | None,
) -> PeriodAuthorityResolution:
    """Resolve one period without inventing inheritance for historical rows.

    Rows without a contract version predate ADR-029 and keep their own values.
    Modern rows derive quantity from the applicable master. SAME_AS_PERIOD is
    only projected here; 655B owns graph resolution and Shift enforcement.
    """
    if contract_version is None:
        return PeriodAuthorityResolution(
            resource_count=max(int(stored_resource_count or 1), 1),
            resource_count_provenance=PERIOD_PROVENANCE_LEGACY,
            confirmation=normalize_confirmation(stored_confirmation),
            confirmation_mode=None,
            confirmation_provenance=PERIOD_PROVENANCE_LEGACY,
            proposed_resource=stored_proposed_resource,
            proposed_resource_mode=None,
            proposed_resource_provenance=PERIOD_PROVENANCE_LEGACY,
        )
    if int(contract_version) != PERIOD_INHERITANCE_CONTRACT_VERSION:
        raise ValueError(f"Version de contrat de période non supportée: {contract_version}")
    if master_resource_count is None:
        raise ValueError("Le maître de période est requis pour résoudre la quantité moderne.")

    effective_count = max(int(master_resource_count or 1), 1)
    c_mode = normalized_confirmation_mode(confirmation_mode)
    if c_mode == CONFIRMATION_MODE_INHERIT_MASTER:
        if master_confirmation is None:
            raise ValueError("La confirmation maître est requise pour une période héritée.")
        effective_confirmation = normalize_confirmation(master_confirmation)
        confirmation_provenance = PERIOD_PROVENANCE_MASTER
    else:
        effective_confirmation = normalize_confirmation(stored_confirmation)
        confirmation_provenance = PERIOD_PROVENANCE_EXPLICIT

    r_mode = normalized_proposed_resource_mode(proposed_resource_mode)
    target = _text(same_as_period_id) or None
    if r_mode == PROPOSED_RESOURCE_MODE_INHERIT_MASTER:
        if target is not None:
            raise ValueError("Une ressource héritée ne peut pas référencer une autre période.")
        effective_resource = master_proposed_resource
        resource_provenance = PERIOD_PROVENANCE_MASTER
        same_as_state = None
    elif r_mode == PROPOSED_RESOURCE_MODE_EXPLICIT:
        if target is not None:
            raise ValueError("Une ressource explicite ne peut pas référencer une autre période.")
        effective_resource = stored_proposed_resource
        resource_provenance = PERIOD_PROVENANCE_EXPLICIT
        same_as_state = None
    else:
        if not target:
            raise ValueError("SAME_AS_PERIOD requiert un identifiant logique de période cible.")
        effective_resource = None
        resource_provenance = PERIOD_PROVENANCE_SAME_AS
        same_as_state = "UNRESOLVED"

    return PeriodAuthorityResolution(
        resource_count=effective_count,
        resource_count_provenance=PERIOD_PROVENANCE_MASTER,
        confirmation=effective_confirmation,
        confirmation_mode=c_mode,
        confirmation_provenance=confirmation_provenance,
        proposed_resource=effective_resource,
        proposed_resource_mode=r_mode,
        proposed_resource_provenance=resource_provenance,
        same_as_period_id=target,
        same_as_root_period_id=None,
        same_as_state=same_as_state,
    )


def validate_period_definitions(periods: Sequence[DemandPeriodDefinition], *, allow_unbudgeted: bool = False) -> None:
    ids: set[str] = set()
    group_counts: dict[str, int] = {}
    for period in periods:
        identifier = _text(period.period_id)
        if not identifier:
            raise ValueError("Chaque période de demande doit avoir un identifiant stable.")
        if identifier in ids:
            raise ValueError(f"Identifiant de période dupliqué: {identifier}")
        ids.add(identifier)
        if period.end_date < period.start_date:
            raise ValueError(
                f"La période {identifier} se termine avant sa date de début."
            )
        if (period.hours is None and not allow_unbudgeted) or (period.hours is not None and period.hours <= 0):
            raise ValueError(f"Les heures de la période {identifier} doivent être positives.")
        if period.resource_count < 1:
            raise ValueError(
                f"Le nombre de ressources de la période {identifier} doit être au moins 1."
            )
        normalize_active_day_target(
            period.desired_active_days,
            start=period.start_date,
            end=period.end_date,
            field=f"Les jours actifs de la période {identifier}",
        )
        normalize_confirmation(period.confirmation)
        normalized_confirmation_mode(period.confirmation_mode)
        resource_mode = normalized_proposed_resource_mode(period.proposed_resource_mode)
        same_as_period_id = _text(period.same_as_period_id)
        if resource_mode == PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD:
            if not same_as_period_id:
                raise ValueError(
                    f"La période {identifier} doit référencer une période pour SAME_AS_PERIOD."
                )
            if same_as_period_id == identifier:
                raise ValueError(f"La période {identifier} ne peut pas se référencer elle-même.")
        elif same_as_period_id:
            raise ValueError(
                f"La période {identifier} ne peut définir same_as_period_id sans SAME_AS_PERIOD."
            )

        kind = _text(period.kind).upper()
        if kind not in VALID_PERIOD_KINDS:
            raise ValueError(f"Type de période non supporté: {period.kind}")
        group = _text(period.alternative_group)
        if kind == PERIOD_KIND_ALTERNATIVE:
            if not group:
                raise ValueError(
                    f"La période alternative {identifier} doit appartenir à un groupe."
                )
            group_counts[group] = group_counts.get(group, 0) + 1
        elif group:
            raise ValueError(
                f"La période cumulative {identifier} ne peut pas avoir de groupe alternatif."
            )

    period_by_id = {_text(period.period_id): period for period in periods}
    same_as_edges: dict[str, str] = {}
    for period in periods:
        identifier = _text(period.period_id)
        if normalized_proposed_resource_mode(period.proposed_resource_mode) != PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD:
            continue
        target_id = _text(period.same_as_period_id)
        target = period_by_id.get(target_id)
        if target is None:
            raise ValueError(
                f"La période {identifier} référence une période SAME_AS_PERIOD introuvable: {target_id}."
            )
        if _text(period.kind).upper() != PERIOD_KIND_CUMULATIVE or _text(target.kind).upper() != PERIOD_KIND_CUMULATIVE:
            raise ValueError("SAME_AS_PERIOD est limité aux périodes cumulatives.")
        if _text(period.alternative_group) or _text(target.alternative_group):
            raise ValueError("SAME_AS_PERIOD ne peut pas relier des périodes alternatives.")
        same_as_edges[identifier] = target_id

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visited:
            return
        if identifier in visiting:
            raise ValueError("Les références SAME_AS_PERIOD ne peuvent pas former de cycle.")
        visiting.add(identifier)
        target_id = same_as_edges.get(identifier)
        if target_id is not None:
            visit(target_id)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in same_as_edges:
        visit(identifier)

    singleton_groups = sorted(group for group, count in group_counts.items() if count < 2)
    if singleton_groups:
        raise ValueError(
            "Un groupe alternatif doit contenir au moins deux options: "
            + ", ".join(singleton_groups)
        )


def effective_period_ids(
    periods: Sequence[DemandPeriodDefinition],
    selections: Mapping[str, str],
) -> tuple[str, ...]:
    """Return periods that may materialize into requirements without double-counting."""

    validate_period_definitions(periods)
    period_by_id = {period.period_id: period for period in periods}
    for group, period_id in selections.items():
        selected = period_by_id.get(period_id)
        if selected is None:
            raise ValueError(
                f"La sélection {period_id} du groupe {group} ne correspond à aucune période."
            )
        if selected.kind.upper() != PERIOD_KIND_ALTERNATIVE:
            raise ValueError(f"La période {period_id} n'est pas une option alternative.")
        if _text(selected.alternative_group) != _text(group):
            raise ValueError(
                f"La période {period_id} n'appartient pas au groupe alternatif {group}."
            )

    effective: list[str] = []
    for period in periods:
        if period.kind.upper() == PERIOD_KIND_CUMULATIVE:
            effective.append(period.period_id)
            continue
        group = _text(period.alternative_group)
        if selections.get(group) == period.period_id:
            effective.append(period.period_id)
    return tuple(effective)


def projected_hours_without_double_counting(
    periods: Sequence[DemandPeriodDefinition],
    selections: Mapping[str, str] | None = None,
) -> float:
    """Project total workforce effort while counting an exclusive group only once."""

    validate_period_definitions(periods)
    chosen = dict(selections or {})
    if chosen:
        effective_period_ids(periods, chosen)

    total = 0.0
    alternatives: dict[str, list[DemandPeriodDefinition]] = {}
    for period in periods:
        if period.kind.upper() == PERIOD_KIND_CUMULATIVE:
            total += float(period.hours or 0)
        else:
            alternatives.setdefault(_text(period.alternative_group), []).append(period)

    for group, options in alternatives.items():
        selected_id = chosen.get(group)
        if selected_id:
            selected = next(period for period in options if period.period_id == selected_id)
            total += float(selected.hours or 0)
        else:
            total += max(float(period.hours or 0) for period in options)
    return round(total, 2)


def projected_hours_in_window(
    total_hours: float,
    start_date: date,
    end_date: date,
    window_start: date,
    window_end: date,
) -> float:
    """Spread a macro workload over its date range and return the window share.

    Medium-term ranges do not contain daily shifts yet. Their hours are therefore
    spread proportionally over weekdays. Weekend-only ranges fall back to calendar
    days so a legitimate weekend request is never projected as zero.
    """

    if end_date < start_date:
        raise ValueError("La date de fin de la charge ne peut pas précéder son début.")
    if window_end < window_start:
        raise ValueError("La fenêtre de projection est invalide.")
    hours = max(float(total_hours or 0.0), 0.0)
    if hours <= 0 or end_date < window_start or start_date > window_end:
        return 0.0

    clipped_start = max(start_date, window_start)
    clipped_end = min(end_date, window_end)

    def day_count(start: date, end: date, *, weekdays_only: bool) -> int:
        count = 0
        cursor = start
        while cursor <= end:
            if not weekdays_only or cursor.weekday() < 5:
                count += 1
            cursor += timedelta(days=1)
        return count

    total_days = day_count(start_date, end_date, weekdays_only=True)
    overlap_days = day_count(clipped_start, clipped_end, weekdays_only=True)
    if total_days == 0:
        total_days = day_count(start_date, end_date, weekdays_only=False)
        overlap_days = day_count(clipped_start, clipped_end, weekdays_only=False)
    return round(hours * overlap_days / total_days, 2) if total_days else 0.0


def projected_period_hours_in_window_without_double_counting(
    periods: Sequence[DemandPeriodDefinition],
    window_start: date,
    window_end: date,
    selections: Mapping[str, str] | None = None,
) -> float:
    """Project period workload into one window without summing exclusive options."""

    validate_period_definitions(periods)
    chosen = dict(selections or {})
    if chosen:
        effective_period_ids(periods, chosen)

    def contribution(period: DemandPeriodDefinition) -> float:
        return projected_hours_in_window(
            float(period.hours or 0),
            period.start_date,
            period.end_date,
            window_start,
            window_end,
        )

    total = 0.0
    alternatives: dict[str, list[DemandPeriodDefinition]] = {}
    for period in periods:
        if period.kind.upper() == PERIOD_KIND_CUMULATIVE:
            total += contribution(period)
        else:
            alternatives.setdefault(_text(period.alternative_group), []).append(period)

    for group, options in alternatives.items():
        selected_id = chosen.get(group)
        if selected_id:
            selected = next(period for period in options if period.period_id == selected_id)
            total += contribution(selected)
        else:
            total += max(contribution(period) for period in options)
    return round(total, 2)
