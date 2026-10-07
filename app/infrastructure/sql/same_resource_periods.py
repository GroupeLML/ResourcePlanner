from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...domain.demand_periods import (
    PERIOD_KIND_CUMULATIVE,
    PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
)
from .demand_period_models import (
    WorkforceRequestPeriod,
    WorkforceRequestPeriodRequirement,
)
from .models import ResourceRequirement, Shift


INACTIVE_REQUIREMENT_STATUSES = {"Annulé", "Terminé"}


def _text(value: object) -> str:
    return str(value or "").strip()


@dataclass(frozen=True, slots=True)
class SameResourcePeriodGroup:
    request_id: str
    line_id: str
    root_period_key: str
    period_keys: tuple[str, ...]
    requirement_ids: tuple[str, ...]

    @property
    def diagnostic_key(self) -> str:
        return f"{self.request_id}/{self.line_id}/{self.root_period_key}"


@dataclass(frozen=True, slots=True)
class SameResourceConstraintSummary:
    group_count: int
    unresolved_group_count: int


class SqlSameResourcePeriodCoordinator:
    """Enforce ADR-029 SAME_AS_PERIOD against the active materialized plan.

    The topology is reconstructed from physical period versions linked to active
    requirements. That keeps an approved active plan isolated from later candidate
    edits while logical period_key values remain the graph identities.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _groups(self) -> tuple[SameResourcePeriodGroup, ...]:
        rows = self._session.execute(
            select(ResourceRequirement, WorkforceRequestPeriod)
            .join(
                WorkforceRequestPeriodRequirement,
                WorkforceRequestPeriodRequirement.resource_requirement_id
                == ResourceRequirement.id,
            )
            .join(
                WorkforceRequestPeriod,
                WorkforceRequestPeriodRequirement.period_id
                == WorkforceRequestPeriod.id,
            )
            .where(
                ResourceRequirement.status.not_in(INACTIVE_REQUIREMENT_STATUSES),
            )
            .order_by(
                ResourceRequirement.workforce_request_id,
                WorkforceRequestPeriod.request_line_id,
                WorkforceRequestPeriod.period_key,
                ResourceRequirement.id,
            )
        ).all()

        periods_by_scope: dict[
            tuple[str, str],
            dict[str, WorkforceRequestPeriod],
        ] = defaultdict(dict)
        requirements_by_scope_period: dict[
            tuple[str, str],
            dict[str, list[str]],
        ] = defaultdict(lambda: defaultdict(list))

        for requirement, period in rows:
            request_id = _text(requirement.workforce_request_id)
            line_id = _text(period.request_line_id)
            period_key = _text(period.period_key)
            if not request_id or not line_id or not period_key:
                continue
            scope = (request_id, line_id)
            previous = periods_by_scope[scope].get(period_key)
            if previous is not None and previous.id != period.id:
                raise ValueError(
                    "Le plan actif contient plusieurs versions physiques de la même "
                    f"période logique {period_key}."
                )
            periods_by_scope[scope][period_key] = period
            requirements_by_scope_period[scope][period_key].append(requirement.id)

        groups: list[SameResourcePeriodGroup] = []
        for (request_id, line_id), periods in periods_by_scope.items():
            edges: dict[str, str] = {}
            for period_key, period in periods.items():
                if (
                    _text(period.proposed_resource_mode).upper()
                    != PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD
                ):
                    continue
                target_key = _text(period.same_as_period_key)
                target = periods.get(target_key)
                if target is None:
                    raise ValueError(
                        "La contrainte SAME_AS_PERIOD du plan actif référence une "
                        f"période non matérialisée: {target_key or '<vide>'}."
                    )
                if (
                    _text(period.kind).upper() != PERIOD_KIND_CUMULATIVE
                    or _text(target.kind).upper() != PERIOD_KIND_CUMULATIVE
                    or period.alternative_group is not None
                    or target.alternative_group is not None
                ):
                    raise ValueError(
                        "SAME_AS_PERIOD est limité aux périodes cumulatives du plan actif."
                    )
                if int(period.resource_count or 1) != 1 or int(target.resource_count or 1) != 1:
                    raise ValueError(
                        "SAME_AS_PERIOD exige une quantité effective de 1 sur toute la composante."
                    )
                edges[period_key] = target_key

            if not edges:
                continue

            roots: dict[str, str] = {}
            visiting: set[str] = set()

            def root_of(period_key: str) -> str:
                cached = roots.get(period_key)
                if cached is not None:
                    return cached
                if period_key in visiting:
                    raise ValueError(
                        "Les références SAME_AS_PERIOD du plan actif forment un cycle."
                    )
                visiting.add(period_key)
                target_key = edges.get(period_key)
                root = root_of(target_key) if target_key is not None else period_key
                visiting.remove(period_key)
                roots[period_key] = root
                return root

            component_periods: dict[str, set[str]] = defaultdict(set)
            for source_key, target_key in edges.items():
                root = root_of(source_key)
                component_periods[root].add(source_key)
                component_periods[root].add(target_key)

            for root_key, period_keys in component_periods.items():
                requirement_ids = tuple(
                    sorted(
                        requirement_id
                        for period_key in period_keys
                        for requirement_id in requirements_by_scope_period[
                            (request_id, line_id)
                        ].get(period_key, [])
                    )
                )
                if len(requirement_ids) != len(period_keys):
                    raise ValueError(
                        "SAME_AS_PERIOD exige exactement un besoin matérialisé par "
                        "période cumulative de la composante."
                    )
                groups.append(
                    SameResourcePeriodGroup(
                        request_id=request_id,
                        line_id=line_id,
                        root_period_key=root_key,
                        period_keys=tuple(sorted(period_keys)),
                        requirement_ids=requirement_ids,
                    )
                )

        groups.sort(
            key=lambda group: (
                group.request_id,
                group.line_id,
                group.root_period_key,
            )
        )
        return tuple(groups)

    def _locked_resource_ids(
        self,
        requirement_ids: tuple[str, ...],
    ) -> set[str]:
        if not requirement_ids:
            return set()
        return {
            _text(value)
            for value in self._session.scalars(
                select(Shift.resource_id).where(
                    Shift.resource_requirement_id.in_(requirement_ids),
                    Shift.locked.is_(True),
                )
            ).all()
            if _text(value)
        }

    def enforce_targets(self) -> SameResourceConstraintSummary:
        """Resolve one common automatic target per linked component before rebuild."""

        groups = self._groups()
        unresolved = 0
        for group in groups:
            requirements = list(
                self._session.scalars(
                    select(ResourceRequirement)
                    .where(ResourceRequirement.id.in_(group.requirement_ids))
                    .order_by(ResourceRequirement.id)
                ).all()
            )
            locked_ids = self._locked_resource_ids(group.requirement_ids)
            if len(locked_ids) > 1:
                raise ValueError(
                    "Des quarts verrouillés d'une composante SAME_AS_PERIOD utilisent "
                    f"des ressources différentes ({group.diagnostic_key})."
                )

            assigned_ids = {
                _text(requirement.assigned_resource_id)
                for requirement in requirements
                if _text(requirement.assigned_resource_id)
            }
            chosen_resource_id: str | None = None
            if locked_ids:
                chosen_resource_id = next(iter(locked_ids))
            elif len(assigned_ids) == 1:
                chosen_resource_id = next(iter(assigned_ids))
            elif len(assigned_ids) > 1:
                raise ValueError(
                    "Les cibles automatiques d'une composante SAME_AS_PERIOD sont "
                    f"incohérentes ({group.diagnostic_key})."
                )

            if chosen_resource_id is None:
                unresolved += 1
                continue

            for requirement in requirements:
                requirement.assigned_resource_id = chosen_resource_id
                if requirement.status not in INACTIVE_REQUIREMENT_STATUSES:
                    requirement.status = "Planifié"

        self._session.flush()
        return SameResourceConstraintSummary(
            group_count=len(groups),
            unresolved_group_count=unresolved,
        )

    def retarget_component(
        self,
        requirement_id: str,
        resource_id: str,
    ) -> bool:
        """Atomically retarget a linked component unless a lock contradicts it."""

        wanted = _text(requirement_id)
        target_resource_id = _text(resource_id)
        for group in self._groups():
            if wanted not in group.requirement_ids:
                continue
            locked_ids = self._locked_resource_ids(group.requirement_ids)
            if len(locked_ids) > 1:
                raise ValueError(
                    "La composante SAME_AS_PERIOD contient des quarts verrouillés "
                    "sur plusieurs ressources."
                )
            if locked_ids and target_resource_id not in locked_ids:
                raise ValueError(
                    "Un quart verrouillé fixe déjà la ressource de toute la composante "
                    "SAME_AS_PERIOD. Libère ou déplace cette décision avant de changer "
                    "la cible."
                )
            requirements = self._session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.id.in_(group.requirement_ids)
                )
            ).all()
            for requirement in requirements:
                requirement.assigned_resource_id = target_resource_id
                if requirement.status not in INACTIVE_REQUIREMENT_STATUSES:
                    requirement.status = "Planifié"
            self._session.flush()
            return True
        return False

    def assert_shift_invariant(self) -> None:
        """Fail the transaction when actual active shifts diverge inside a component."""

        for group in self._groups():
            resource_ids = {
                _text(value)
                for value in self._session.scalars(
                    select(Shift.resource_id).where(
                        Shift.resource_requirement_id.in_(group.requirement_ids)
                    )
                ).all()
                if _text(value)
            }
            if len(resource_ids) > 1:
                raise ValueError(
                    "La contrainte SAME_AS_PERIOD exige la même ressource réelle sur "
                    f"tous les quarts de la composante ({group.diagnostic_key})."
                )
