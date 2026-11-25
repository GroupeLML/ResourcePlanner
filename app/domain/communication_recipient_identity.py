"""Stable, fail-closed employee identity equivalence for ADR-039."""

from __future__ import annotations

from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Iterable, Mapping


@dataclass(frozen=True, order=True)
class RecipientIdentity:
    kind: str
    id: str

    def __post_init__(self) -> None:
        if (type(self.kind) is not str or self.kind not in {
                "RESOURCE", "CONTACT", "USER", "ERP_EMPLOYEE", "AUTHENTICATED_PRINCIPAL"}
                or type(self.id) is not str or not self.id or self.id != self.id.strip()):
            raise ValueError("Invalid canonical recipient identity")
        if self.kind == "AUTHENTICATED_PRINCIPAL":
            try:
                parts = json.loads(self.id)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid authenticated principal identity") from exc
            if (type(parts) is not list or len(parts) != 2
                    or any(not _stable_principal_part(part) for part in parts)
                    or self.id != _principal_key(*parts)):
                raise ValueError("Invalid authenticated principal identity")


def _stable_principal_part(value: str) -> bool:
    return (type(value) is str and bool(value) and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _principal_key(issuer: str, subject: str) -> str:
    return json.dumps([issuer, subject], ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True)
class AuthenticatedPrincipalIdentityFact:
    """Issuer/subject authority with an independently proven optional local link."""
    issuer: str
    subject: str
    local_user_id: str | None = None

    def __post_init__(self) -> None:
        if (not _stable_principal_part(self.issuer) or not _stable_principal_part(self.subject)
                or (self.local_user_id is not None and not _stable_principal_part(self.local_user_id))):
            raise ValueError("Invalid authenticated principal evidence")

    @property
    def identity(self) -> RecipientIdentity:
        return RecipientIdentity("AUTHENTICATED_PRINCIPAL", _principal_key(self.issuer, self.subject))


@dataclass(frozen=True)
class ERPPrincipalIdentityFact:
    """The ERP principal employee ID observed from the canonical project authority."""
    project_id: str
    employee_external_id: str

    def __post_init__(self) -> None:
        if not _stable_principal_part(self.project_id) or not _stable_principal_part(self.employee_external_id):
            raise ValueError("Invalid ERP principal evidence")

    @property
    def identity(self) -> RecipientIdentity:
        return RecipientIdentity("ERP_EMPLOYEE", self.employee_external_id)


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
    *,
    erp_principals: Iterable[ERPPrincipalIdentityFact] = (),
    authenticated_principals: Iterable[AuthenticatedPrincipalIdentityFact] = (),
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
    # ERP identity comes only from the canonical project principal authority,
    # never from a Resource/contact label. It joins the same stable-link graph.
    projects: dict[str, str] = {}
    for fact in erp_principals:
        if type(fact) is not ERPPrincipalIdentityFact:
            raise ValueError("Expected typed ERP principal authority")
        previous = projects.setdefault(fact.project_id, fact.employee_external_id)
        if previous != fact.employee_external_id:
            raise ValueError("Contradictory ERP principal authority for project")
        add(fact.identity, fact.employee_external_id)

    principals: dict[RecipientIdentity, str | None] = {}
    for fact in authenticated_principals:
        if type(fact) is not AuthenticatedPrincipalIdentityFact:
            raise ValueError("Expected typed authenticated principal authority")
        previous = principals.setdefault(fact.identity, fact.local_user_id)
        if previous != fact.local_user_id:
            raise ValueError("Contradictory authenticated local association")
        add(fact.identity, None)
        if fact.local_user_id is not None:
            user = RecipientIdentity("USER", fact.local_user_id)
            if user not in graph:
                raise ValueError("Missing proven authenticated local association")
            connect(fact.identity, user)

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
        priority = {"RESOURCE": 0, "CONTACT": 1, "USER": 2,
                    "ERP_EMPLOYEE": 3, "AUTHENTICATED_PRINCIPAL": 4}
        canonical = min(component, key=lambda member: (priority[member.kind], member.id))
        aliases.update((member, canonical) for member in component)
    return RecipientIdentityIndex(MappingProxyType(aliases), frozenset(ambiguous))


# Preserve the historical v1 identity authority for frozen recipient contexts.
build_recipient_identity_index = build_recipient_identity_index_v1
