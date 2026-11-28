"""Read immutable recipient authority from the same communication SQL session."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...domain.communication_recipient_composition import RecipientCandidate, contact_rooted_candidate_v1
from ...domain.communication_recipient_identity import RecipientIdentity
from ...domain.communication_recipient_policy import RecipientPolicyPopulation
from ...domain.communication_recipient_provenance import (
    MessageRecipientInputs, RecipientAuthorityInput, RecipientPolicyContext,
)
from ...domain.project_communication import ProjectCommunicationProjection
from ...domain.superintendent_population import SuperintendentPopulation
from .business_contact_models import BusinessContact
from .communication_recipient_identity import _read_matches, read_recipient_identity_evidence
from .identity_models import AppUser
from .models import Resource


def _text(value):
    return (value or "").strip()


def _project_sources(project):
    """Preserve manager/co-manager/requester precedence and assigned-resource roles."""
    result = []
    for role, participants in (
        ("MANAGER", (project.project_manager,)),
        ("CO_MANAGER", project.project_co_managers),
        ("REQUESTER", project.requesters),
    ):
        for participant in participants:
            if role == "REQUESTER" and participant.user_id:
                root = RecipientIdentity("USER", participant.user_id)
            elif participant.contact_id:
                root = RecipientIdentity("CONTACT", participant.contact_id)
            elif participant.user_id:
                root = RecipientIdentity("USER", participant.user_id)
            elif participant.active or participant.email:
                # In particular, ERP manager-only Resource fallback cannot be
                # expressed as a contact/user-rooted manager in the v5 model.
                raise ValueError("Recipient authority has no representable project participant root")
            else:
                continue
            result.append((root, role, "TO", participant))
    resources = {}
    for day in project.days:
        for task in day.tasks:
            for resource in task.resources:
                resources[resource.resource_id] = resource.contact
    for key, participant in sorted(resources.items(), key=lambda item: (
        item[1].display_name.casefold(), _text(item[1].email).casefold(), item[0],
    )):
        result.append((RecipientIdentity("RESOURCE", key), "ASSIGNED", "CC", participant))
    # Multiple Shifts/roles may contribute the same identity, but the same
    # source role must not be duplicated.
    unique = {}
    for root, role, channel, participant in result:
        key = (root, role, channel)
        if key in unique and unique[key] != participant:
            raise ValueError("Contradictory project recipient source facts")
        unique.setdefault(key, participant)
    return tuple((root, role, channel, participant)
                 for (root, role, channel), participant in unique.items())


def capture_recipient_policy_context(
    session: Session,
    projection: ProjectCommunicationProjection,
    population: SuperintendentPopulation,
) -> RecipientPolicyContext:
    """Capture once across the complete current v4 business projection.

    Only server-resolved project participants establish roots. Raw address
    fields are read in bounded exact-ID batches, never reconstructed from
    trimmed presentation values. Stable identity closure includes competitors.
    """
    if projection.request_provenance_version != 4 or not projection.request_provenance_complete:
        raise ValueError("Expected complete current v4 business projection")
    sources = {project.project_id: _project_sources(project) for project in projection.projects}
    roots = {root for rows in sources.values() for root, _, _, _ in rows}
    roots.update(RecipientIdentity("RESOURCE", employee.resource_id) for employee in population.employees)
    evidence = read_recipient_identity_evidence(session, roots)
    index = evidence.index()
    if not roots <= (set(index.aliases) | index.ambiguous):
        raise ValueError("Missing persisted recipient identity root")
    source_roots = {root for rows in sources.values() for root, _, _, _ in rows}
    resource_ids = {root.id for root in source_roots if root.kind == "RESOURCE"}
    contact_ids = {root.id for root in source_roots if root.kind == "CONTACT"}
    user_ids = {root.id for root in source_roots if root.kind == "USER"}
    # Only proven canonical Resource links can supply contact fallback.
    for root in source_roots:
        canonical = index.canonical(root)
        if root.kind == "CONTACT" and canonical.kind == "RESOURCE":
            resource_ids.add(canonical.id)

    resources = {row[0]: row[1:] for row in _read_matches(
        session, (Resource.id, Resource.name, Resource.email, Resource.active, Resource.erp_active),
        Resource.id, resource_ids,
    )}
    contacts = {row[0]: row[1:] for row in _read_matches(
        session, (BusinessContact.id, BusinessContact.email, BusinessContact.active),
        BusinessContact.id, contact_ids,
    )}
    users = {row[0]: row[1:] for row in _read_matches(
        session, (AppUser.id, AppUser.email, AppUser.active), AppUser.id, user_ids,
    )}
    if set(resources) != resource_ids or set(contacts) != contact_ids or set(users) != user_ids:
        raise ValueError("Missing persisted recipient address authority")

    messages = []
    for project_id, rows in sources.items():
        candidates = []
        for root, role, channel, participant in rows:
            fallback = None
            active = participant.active
            if root.kind == "RESOURCE":
                _, email, local_active, erp_active = resources[root.id]
                if active != bool(local_active and erp_active):
                    raise ValueError("Assigned resource changed during recipient capture")
                authority = "RESOURCE_EMAIL"
            elif root.kind == "USER":
                email, user_active = users[root.id]
                if role == "REQUESTER" and active != bool(user_active):
                    raise ValueError("Requester changed during recipient capture")
                authority = "APP_USER_EMAIL"
            else:
                email, contact_active = contacts[root.id]
                if active and not contact_active:
                    raise ValueError("Contact changed during recipient capture")
                authority = "BUSINESS_CONTACT_EMAIL"
                canonical = index.canonical(root)
                if canonical.kind == "RESOURCE":
                    name, raw_email, local_active, erp_active = resources[canonical.id]
                    fallback = RecipientCandidate(
                        canonical, name, role, channel, raw_email, "RESOURCE_EMAIL",
                        bool(local_active and erp_active),
                    )
            primary = RecipientCandidate(root, participant.display_name, role, channel, email, authority, active)
            selected = contact_rooted_candidate_v1(primary, fallback, index) if root.kind == "CONTACT" else primary
            # Do not silently discard a previously legitimate address (including
            # a legacy fallback that the accepted v5 evidence cannot prove), or
            # invent a new address from a conflicting concurrent/source read.
            if _text(selected.email) != _text(participant.email):
                raise ValueError("Project recipient address authority cannot be preserved")
            candidates.append(RecipientAuthorityInput(primary, fallback))
        messages.append(MessageRecipientInputs(project_id, tuple(candidates)))
    return RecipientPolicyContext.capture(RecipientPolicyPopulation.capture(population), evidence, messages)
