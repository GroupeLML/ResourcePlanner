"""Inactive independent RP co-manager nomination capture (ADR-017 / ADR-039)."""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from ...domain.communication_recipient_composition import RecipientCandidate
from ...domain.communication_recipient_identity import (
    ContactIdentityFact, RecipientIdentity, ResourceIdentityFact, UserIdentityFact,
)
from ...domain.communication_recipient_provenance import (
    MessageRecipientInputs, RecipientAuthorityInput, RecipientIdentityEvidence, RPNominationSource,
)
from .business_contact_models import BusinessContact
from .communication_recipient_identity import _read_matches, read_recipient_identity_evidence
from .identity_models import AppUser
from .models import Project, Resource
from .project_manager_resolution_repository import SqlProjectManagerResolutionRepository


def capture_rp_co_manager_authorities(
    session: Session, project_ids: Iterable[str],
) -> tuple[tuple[MessageRecipientInputs, ...], RecipientIdentityEvidence]:
    """Read actual RP nominations, including roles hidden by effective presentation.

    Each requested existing project has an explicit row, including empty
    nominations. The result contains only co-manager role inputs and their
    reachable identity facts, not a complete project policy context.
    This boundary performs no writes and does not prove transaction-wide
    coherence; integrated source acquisition is a separate checkpoint.
    """
    wanted = set(project_ids)
    if any(type(key) is not str or not key or key != key.strip() for key in wanted):
        raise ValueError("Expected exact project identity")
    found = {key for key, in _read_matches(session, (Project.id,), Project.id, wanted)}
    if found != wanted:
        raise ValueError("Missing or inexact persisted project authority")

    # Reuse the canonical repository's bounded raw nomination read, not the
    # effective manager presentation (which merges same-person roles).
    nominations = SqlProjectManagerResolutionRepository(session).list_co_managers(
        tuple(sorted(wanted)),
    )
    by_project: dict[str, list[str]] = {key: [] for key in sorted(wanted)}
    pairs = set()
    for row in nominations:
        # Unlike an optional identity lookup, silently filtering a mismatched
        # relationship could lose a persisted nomination under SQL collation.
        if row.project_id not in wanted:
            raise ValueError("Inexact persisted RP nomination project key")
        contact = RecipientIdentity("CONTACT", row.business_contact_id)
        pair = (row.project_id, contact.id)
        if pair in pairs:
            raise ValueError("Duplicate persisted RP nomination")
        pairs.add(pair)
        by_project[row.project_id].append(contact.id)

    # Freeze independent complete raw relationship coverage before candidate or address construction.
    sources = {key: RPNominationSource.capture(key, keys) for key, keys in by_project.items()}
    contact_ids = {contact_id for _, contact_id in pairs}
    roots = {RecipientIdentity("CONTACT", key) for key in contact_ids}
    evidence = read_recipient_identity_evidence(session, roots)
    index = evidence.index()
    contact_facts = {row.contact_id: row for row in evidence.contacts}
    resource_facts = {row.resource_id: row for row in evidence.resources}

    # Read scalar facts, not cached ORM instances. Exact-match filtering also
    # excludes collation-equivalent nonidentical lookup keys.
    contacts = {row[0]: row[1:] for row in _read_matches(
        session, (BusinessContact.id, BusinessContact.external_system,
                  BusinessContact.external_entity, BusinessContact.external_id,
                  BusinessContact.display_name, BusinessContact.email, BusinessContact.active),
        BusinessContact.id, contact_ids,
    )}
    if set(contacts) != contact_ids:
        raise ValueError("Missing or inexact persisted RP nomination contact")

    # Activity follows the existing contact-rooted manager contract: a contact
    # can be valid without a login; an exact linked inactive user blocks it.
    # Do not infer a user from names, addresses or employee IDs alone.
    expected_users = {row for row in evidence.users if row.business_contact_id in contact_ids}
    users = {}
    observed_users = set()
    for user_id, employee, contact_id, active in _read_matches(
        session, (AppUser.id, AppUser.employee_external_id,
                  AppUser.business_contact_id, AppUser.active),
        AppUser.business_contact_id, contact_ids,
    ):
        if contact_id in users:
            raise ValueError("Contradictory RP nomination user association")
        users[contact_id] = bool(active)
        observed_users.add(UserIdentityFact(user_id, employee, contact_id))
    if observed_users != expected_users:
        raise ValueError("RP nomination user identity changed during capture")

    resource_ids = {canonical.id for root in roots
                    if (canonical := index.canonical(root)).kind == "RESOURCE"}
    resources = {row[0]: row[1:] for row in _read_matches(
        session, (Resource.id, Resource.external_id, Resource.name, Resource.email,
                  Resource.active, Resource.erp_active),
        Resource.id, resource_ids,
    )}
    if set(resources) != resource_ids:
        raise ValueError("Missing persisted RP nomination fallback Resource")

    candidates = {}
    for key, (system, entity, external_id, name, email, active) in contacts.items():
        if contact_facts.get(key) != ContactIdentityFact(key, system, entity, external_id):
            raise ValueError("RP nomination contact identity changed during capture")
        root = RecipientIdentity("CONTACT", key)
        primary = RecipientCandidate(
            root, (name or "").strip() or key, "CO_MANAGER", "TO",
            email, "BUSINESS_CONTACT_EMAIL", bool(active) and users.get(key, True),
        )
        fallback = None
        canonical = index.canonical(root)
        if canonical.kind == "RESOURCE":
            employee, resource_name, raw, local_active, erp_active = resources[canonical.id]
            if resource_facts.get(canonical.id) != ResourceIdentityFact(canonical.id, employee):
                raise ValueError("RP nomination Resource identity changed during capture")
            fallback = RecipientCandidate(
                canonical, resource_name, "CO_MANAGER", "TO", raw,
                "RESOURCE_EMAIL", bool(local_active and erp_active),
            )
        # Retain missing/invalid/inactive contact roles. Address precedence,
        # aliasing and final transport deduplication belong to the domain.
        candidates[key] = RecipientAuthorityInput(primary, fallback)

    return tuple(MessageRecipientInputs(project_id, tuple(candidates[key] for key in keys),
                                        sources[project_id])
                 for project_id, keys in by_project.items()), evidence
