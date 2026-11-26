"""Inactive persisted ERP manager source capture for ADR-039 / ADR-017."""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from ...application.project_managers import (
    PROJECT_MANAGER_SOURCE_ERP,
    ProjectManagerResolutionService,
)
from ...domain.communication_recipient_authority import (
    ERPManagerAuthority, RecipientAddressObservation,
)
from ...domain.communication_recipient_identity import (
    ContactIdentityFact, ERPPrincipalIdentityFact, RecipientIdentity,
    ResourceIdentityFact, UserIdentityFact,
)
from ...domain.communication_recipient_provenance import RecipientIdentityEvidence
from .business_contact_models import BusinessContact
from .communication_recipient_identity import _read_matches, read_recipient_identity_evidence
from .identity_models import AppUser
from .models import Project, Resource
from .project_manager_resolution_repository import SqlProjectManagerResolutionRepository


def capture_erp_manager_authorities(
    session: Session, project_ids: Iterable[str],
) -> tuple[tuple[ERPManagerAuthority, ...], RecipientIdentityEvidence]:
    """Capture canonical ERP principals and their manager-only identity closure.

    Missing ERP principals yield no manager row; missing local identities do not
    remove an existing ERP principal. RP nominations are not manager authority,
    including when ADR-017's effective projection merges same-person roles.
    Complete cross-source closure/coherence and operational integration are
    deliberately outside this checkpoint. A shared Session is not proof of a
    transaction-wide coherent snapshot.
    """
    wanted = set(project_ids)
    if any(type(key) is not str or not key or key != key.strip() for key in wanted):
        raise ValueError("Expected exact project identity")
    # Corroborate the canonical resolver with raw persisted project keys. The
    # existing resolver normalizes text and SQL Server collation can match
    # variants; never use such a variant as new v5 stable identity evidence.
    projects = {key: employee for key, employee in _read_matches(
        session, (Project.id, Project.project_manager_external_id), Project.id, wanted,
    )}
    resolved = ProjectManagerResolutionService(
        SqlProjectManagerResolutionRepository(session)
    ).resolve_projects(sorted(wanted))
    if set(projects) != wanted or set(resolved) != wanted:
        raise ValueError("Missing or inexact persisted project authority")

    primaries = {}
    for project_id in sorted(wanted):
        primary = resolved[project_id].primary
        employee = projects[project_id]
        if primary is None:
            if employee and employee.strip():
                raise ValueError("Missing canonical ERP manager resolution")
            continue
        if (PROJECT_MANAGER_SOURCE_ERP not in primary.sources
                or primary.employee_external_id != employee):
            raise ValueError("Contradictory or inexact ERP principal resolution")
        principal = ERPPrincipalIdentityFact(project_id, employee)
        primaries[principal] = primary

    evidence = read_recipient_identity_evidence(session, (), erp_principals=primaries)
    index = evidence.index(erp_principals=primaries)
    user_facts = {row.user_id: row for row in evidence.users}
    contact_facts = {row.contact_id: row for row in evidence.contacts}
    resource_facts = {row.resource_id: row for row in evidence.resources}
    user_ids = {row.app_user_id for row in primaries.values() if row.app_user_id is not None}
    contact_ids = {row.business_contact_id for row in primaries.values()
                   if row.business_contact_id is not None}
    resource_ids = {
        root.id for principal in primaries
        if (root := index.canonical(principal.identity)).kind == "RESOURCE"
    }
    # Keep raw address observations and independent activity flags. Read
    # scalar columns rather than cached ORM objects, and corroborate the
    # stable links with the identity closure before constructing authority.
    # AppUser.email is intentionally not read.
    users = {row[0]: row[1:] for row in _read_matches(
        session, (AppUser.id, AppUser.employee_external_id, AppUser.business_contact_id,
                  AppUser.active, AppUser.display_name), AppUser.id, user_ids,
    )}
    contacts = {row[0]: row[1:] for row in _read_matches(
        session, (BusinessContact.id, BusinessContact.external_system,
                  BusinessContact.external_entity, BusinessContact.external_id,
                  BusinessContact.email, BusinessContact.active),
        BusinessContact.id, contact_ids,
    )}
    resources = {row[0]: row[1:] for row in _read_matches(
        session, (Resource.id, Resource.external_id, Resource.email,
                  Resource.active, Resource.erp_active), Resource.id, resource_ids,
    )}
    if (set(users) != user_ids or set(resources) != resource_ids
            or set(contacts) != contact_ids.intersection(contact_facts)):
        raise ValueError("Manager identity or address source changed during capture")

    authorities = []
    for principal, primary in primaries.items():
        user_id = primary.app_user_id
        contact_id = primary.business_contact_id
        display_name = primary.display_name
        user_active = None
        if user_id is not None:
            employee, linked_contact, active, user_name = users[user_id]
            if (user_facts.get(user_id) != UserIdentityFact(user_id, employee, linked_contact)
                    or employee != principal.employee_external_id
                    or linked_contact != contact_id or bool(active) != primary.user_active):
                raise ValueError("Contradictory persisted manager user authority")
            user_active = bool(active)
            # Match the current project communications manager label convention.
            if contact_id is not None and (user_name or "").strip():
                display_name = user_name.strip()

        contact_status = "ABSENT" if contact_id is None else "UNRESOLVED"
        contact_active = None
        contact_address = None
        if contact_id in contacts:
            system, entity, external_id, raw, active = contacts[contact_id]
            if contact_facts[contact_id] != ContactIdentityFact(
                    contact_id, system, entity, external_id):
                raise ValueError("Manager contact identity changed during capture")
            contact_status = "RESOLVED"
            contact_active = bool(active)
            contact_address = RecipientAddressObservation(
                "BUSINESS_CONTACT_EMAIL", RecipientIdentity("CONTACT", contact_id), "CURRENT", raw,
            )
        if primary.contact_active != contact_active:
            raise ValueError("Contradictory persisted manager contact activity")

        resource_address = None
        resource_active = None
        resource_erp_active = None
        root = index.canonical(principal.identity)
        # Only the accepted identity index may prove a unique Resource alias.
        # Ambiguity never selects fallback, even if competing addresses agree.
        if root.kind == "RESOURCE":
            employee, raw, active, erp_active = resources[root.id]
            if (resource_facts[root.id] != ResourceIdentityFact(root.id, employee)
                    or employee != principal.employee_external_id):
                raise ValueError("Manager Resource identity changed during capture")
            resource_address = RecipientAddressObservation("RESOURCE_EMAIL", root, "CURRENT", raw)
            resource_active, resource_erp_active = bool(active), bool(erp_active)

        authority = ERPManagerAuthority(
            principal, display_name, user_id, user_active, contact_id, contact_status,
            contact_active, contact_address, resource_address, resource_active, resource_erp_active,
        )
        # Delivery precedence and blocking semantics remain the domain's
        # authority; this adapter only acquires persisted facts.
        authority.validate(evidence, index)
        authorities.append(authority)
    return tuple(authorities), evidence
