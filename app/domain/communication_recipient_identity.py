"""Stable, fail-closed employee identity equivalence for ADR-039."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


@dataclass(frozen=True, order=True)
class RecipientIdentity:
    kind: str
    id: str

    def __post_init__(self) -> None:
        if self.kind not in {"RESOURCE", "CONTACT", "USER"} or not self.id:
            raise ValueError("Invalid canonical recipient identity")


@dataclass(frozen=True)
class ResourceIdentityFact:
    resource_id: str
    employee_external_id: str | None


@dataclass(frozen=True)
class ContactIdentityFact:
    contact_id: str
    external_system: str | None = None
    external_entity: str | None = None
    external_id: str | None = None

    @property
    def employee_external_id(self) -> str | None:
        # SqlIdentityRepository creates/reconciles this exact namespace. An
        # arbitrary ERP-labelled contact is not an independent employee link.
        if self.external_system == "RESOURCEPLANNER" and self.external_entity == "EMPLOYEE":
            return self.external_id
        return None


@dataclass(frozen=True)
class UserIdentityFact:
    user_id: str
    employee_external_id: str | None
    business_contact_id: str | None


def _employee_key(value: str | None) -> str | None:
    # Match exact, nonblank stable IDs; do not infer aliases from SQL collation.
    return value if value and value == value.strip() else None


@dataclass(frozen=True)
class RecipientIdentityIndex:
    aliases: Mapping[RecipientIdentity, RecipientIdentity]
    ambiguous: frozenset[RecipientIdentity]

    def canonical(self, identity: RecipientIdentity) -> RecipientIdentity:
        return self.aliases.get(identity, identity)

    def equivalent(self, left: RecipientIdentity, right: RecipientIdentity) -> bool:
        return self.canonical(left) == self.canonical(right)


def build_recipient_identity_index_v1(
    resources: Iterable[ResourceIdentityFact],
    contacts: Iterable[ContactIdentityFact],
    users: Iterable[UserIdentityFact],
) -> RecipientIdentityIndex:
    """Resolve complete stable-link facts, including competing matches.

    Components with conflicting employee IDs or multiple rows of any root kind
    do not establish cross-root equivalence. Same-root identity remains valid.
    The caller must supply all competing matches, not just eligible Resources.
    Resource.external_id is unique when non-null (filtered index); duplicate
    Resource facts are still rejected defensively if supplied to this boundary.
    """
    users = tuple(users)
    graph: dict[RecipientIdentity, set[RecipientIdentity]] = {}
    employee_ids: dict[RecipientIdentity, set[str]] = {}
    by_employee: dict[str, set[RecipientIdentity]] = {}

    def add(identity: RecipientIdentity, external_id: str | None) -> None:
        graph.setdefault(identity, set())
        key = _employee_key(external_id)
        if key is not None:
            employee_ids.setdefault(identity, set()).add(key)
            by_employee.setdefault(key, set()).add(identity)

    def connect(left: RecipientIdentity, right: RecipientIdentity) -> None:
        graph[left].add(right)
        graph[right].add(left)

    for row in resources:
        add(RecipientIdentity("RESOURCE", row.resource_id), row.employee_external_id)
    for row in contacts:
        add(RecipientIdentity("CONTACT", row.contact_id), row.employee_external_id)
    for row in users:
        add(RecipientIdentity("USER", row.user_id), row.employee_external_id)
    for row in users:
        if row.business_contact_id:
            contact = RecipientIdentity("CONTACT", row.business_contact_id)
            if contact in graph:
                connect(RecipientIdentity("USER", row.user_id), contact)
    for members in by_employee.values():
        ordered = sorted(members)
        for member in ordered[1:]:
            connect(ordered[0], member)

    aliases: dict[RecipientIdentity, RecipientIdentity] = {}
    ambiguous: set[RecipientIdentity] = set()
    remaining = set(graph)
    for root in sorted(graph):
        if root not in remaining:
            continue
        stack = [root]
        component: set[RecipientIdentity] = set()
        while stack:
            node = stack.pop()
            if node not in component:
                component.add(node)
                stack.extend(graph[node] - component)
        remaining.difference_update(component)
        external_ids = {key for member in component for key in employee_ids.get(member, ())}
        if len(external_ids) > 1 or len({member.kind for member in component}) != len(component):
            ambiguous.update(component)
            continue
        priority = {"RESOURCE": 0, "CONTACT": 1, "USER": 2}
        canonical = min(component, key=lambda member: (priority[member.kind], member.id))
        aliases.update((member, canonical) for member in component)
    return RecipientIdentityIndex(MappingProxyType(aliases), frozenset(ambiguous))


# Preserve the historical v1 identity authority for frozen recipient contexts.
build_recipient_identity_index = build_recipient_identity_index_v1
