"""Inactive, transaction-owned current-only canonical communication acquisition."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ...application.operational_contacts import OperationalContactService
from ...domain.communication_recipient_composition import RecipientCandidate
from ...domain.communication_recipient_identity import (
    RecipientIdentity, ResourceIdentityFact, UserIdentityFact,
)
from ...domain.communication_recipient_policy import RecipientPolicyPopulation
from ...domain.communication_recipient_provenance import (
    MessageRecipientInputs, RecipientAuthorityInput, RecipientIdentityEvidence,
    RecipientPolicyContext,
)
from ...domain.project_communication import (
    ProjectCommunicationAssignment, ProjectCommunicationProjection,
    build_project_communication_projection,
)
from .communication_erp_manager_capture import capture_erp_manager_authorities
from .communication_manager_presentation_capture import capture_manager_presentation
from .communication_read_uow import communication_read_uow
from .communication_recipient_identity import _read_matches, read_recipient_identity_evidence
from .communication_rp_co_manager_capture import capture_rp_co_manager_authorities
from .identity_models import AppUser
from .models import Resource, ResourceRequirement, Shift, WorkforceRequest
from .operational_contact_repository import SqlOperationalContactRepository
from .project_communication_repository import SqlProjectCommunicationRepository
from .session import SqlSessionFactory


def _text(value):
    return (value or "").strip()


def _capture_assignment_inputs(
    session: Session, assignments: tuple[ProjectCommunicationAssignment, ...],
    project_ids: tuple[str, ...],
):
    """Bind requester/assigned roles to exact persisted keys and raw addresses.

    The originating assignment reader owns shift coverage and presentation.
    Read raw requester relationship keys as well as addresses: its legacy
    presenter trims requester IDs, which cannot prove an exact v5 root.
    """
    # Legacy joins may match collation-equivalent foreign keys. Independently
    # corroborate the exact persisted relationship keys, never infer a new
    # canonical root from a normalized presentation or a loose SQL join.
    shift_ids = {row.shift_id for row in assignments}
    requirement_ids = {row.requirement_id for row in assignments}
    shifts = {row[0]: row[1:] for row in _read_matches(
        session, (Shift.id, Shift.resource_requirement_id, Shift.resource_id),
        Shift.id, shift_ids,
    )}
    requirements = {row[0]: row[1:] for row in _read_matches(
        session, (ResourceRequirement.id, ResourceRequirement.project_id,
                  ResourceRequirement.workforce_request_id),
        ResourceRequirement.id, requirement_ids,
    )}
    if set(shifts) != shift_ids or set(requirements) != requirement_ids:
        raise ValueError("Missing or inexact persisted assignment relationship")
    for row in assignments:
        if (shifts[row.shift_id] != (row.requirement_id, row.resource_id)
                or requirements[row.requirement_id] != (row.project_id, row.workforce_request_id)):
            raise ValueError("Contradictory or inexact persisted assignment relationship")

    request_ids = {row.workforce_request_id for row in assignments
                   if row.workforce_request_id is not None}
    requests = {row[0]: row[1:] for row in _read_matches(
        session, (WorkforceRequest.id, WorkforceRequest.requester_user_id,
                  WorkforceRequest.requester_name), WorkforceRequest.id, request_ids,
    )}
    if set(requests) != request_ids:
        raise ValueError("Missing or inexact persisted requester relationship")

    user_ids = set()
    resource_ids = set()
    for row in assignments:
        raw_user, _ = requests.get(row.workforce_request_id, (None, None))
        presented_user = row.requester.user_id if row.requester is not None else None
        if raw_user not in (None, ""):
            root = RecipientIdentity("USER", raw_user)
            if presented_user != root.id:
                raise ValueError("Contradictory persisted requester relationship")
            user_ids.add(root.id)
        elif row.requester is not None:
            raise ValueError("Requester presentation lacks persisted relationship")
        resource_ids.add(RecipientIdentity("RESOURCE", row.resource_id).id)

    users = {row[0]: row[1:] for row in _read_matches(
        session, (AppUser.id, AppUser.employee_external_id, AppUser.business_contact_id,
                  AppUser.email, AppUser.active, AppUser.display_name), AppUser.id, user_ids,
    )}
    resources = {row[0]: row[1:] for row in _read_matches(
        session, (Resource.id, Resource.external_id, Resource.name, Resource.email,
                  Resource.active, Resource.erp_active), Resource.id, resource_ids,
    )}
    if set(users) != user_ids or set(resources) != resource_ids:
        raise ValueError("Missing or inexact persisted assignment recipient authority")

    by_project = {key: {} for key in project_ids}
    def add(project_id, candidate):
        key = (candidate.role, candidate.identity)
        inputs = by_project[project_id]
        previous = inputs.setdefault(key, RecipientAuthorityInput(candidate))
        # A blank AppUser display name permits request-specific legacy labels.
        # These are not identity/address authority; the v4 builder chooses the
        # single projected label. All repeated raw authority must still agree.
        prior = previous.primary
        if (prior.email != candidate.email or prior.active != candidate.active
                or (candidate.role == "ASSIGNED" and prior.display_name != candidate.display_name)):
            raise ValueError("Contradictory repeated assignment recipient authority")

    for row in assignments:
        if row.requester is not None:
            participant = row.requester
            key = participant.user_id
            _, contact, raw, active, user_name = users[key]
            _, request_name = requests[row.workforce_request_id]
            if (participant.contact_id != (_text(contact) or None)
                    or participant.display_name != (_text(user_name) or _text(request_name))
                    or participant.active != bool(active)
                    or _text(participant.email) != _text(raw)):
                raise ValueError("Contradictory requester source presentation")
            add(row.project_id, RecipientCandidate(
                RecipientIdentity("USER", key), participant.display_name,
                "REQUESTER", "TO", raw, "APP_USER_EMAIL", bool(active),
            ))
        _, name, raw, active, erp_active = resources[row.resource_id]
        participant = row.resource_contact
        if (participant.contact_id is not None or participant.user_id is not None
                or participant.display_name != name or row.resource_name != name
                or participant.active != bool(active and erp_active)
                or _text(participant.email) != _text(raw)):
            raise ValueError("Contradictory assigned Resource source presentation")
        add(row.project_id, RecipientCandidate(
            RecipientIdentity("RESOURCE", row.resource_id), name, "ASSIGNED", "CC",
            raw, "RESOURCE_EMAIL", bool(active and erp_active),
        ))

    # Match the v4 projection's requester order and established assigned order
    # at the call site; preserve independent repeated-role corroboration here.
    user_facts = {UserIdentityFact(key, row[0], row[1]) for key, row in users.items()}
    resource_facts = {ResourceIdentityFact(key, row[0]) for key, row in resources.items()}
    return by_project, user_facts, resource_facts


def _corroborate_partial(
    partial: RecipientIdentityEvidence, complete: RecipientIdentityEvidence,
    index, roots,
):
    """Require all and only reachable physical facts for the independent roots."""
    reachable = set()
    for root in roots:
        if root not in index.components:
            raise ValueError("Missing canonical identity closure root")
        reachable.update(index.components[root])
    for field, kind, key in (("resources", "RESOURCE", "resource_id"),
                             ("contacts", "CONTACT", "contact_id"),
                             ("users", "USER", "user_id")):
        expected = {row for row in getattr(complete, field)
                    if RecipientIdentity(kind, getattr(row, key)) in reachable}
        if set(getattr(partial, field)) != expected:
            raise ValueError("Contradictory or incomplete independent identity closure")


def acquire_canonical_current_projection(
    factory: SqlSessionFactory, *, week_start: date,
    superintendent_competency_id: str | None,
) -> ProjectCommunicationProjection:
    """Acquire the entire current projection in one accepted owned read scope.

    Not an operational application port. No prebuilt business projection,
    caller Session, generator authority, external provider or retry is accepted.
    A cleanup failure prevents delivery of the materialized result.
    """
    week = week_start - timedelta(days=week_start.weekday())
    end = week + timedelta(days=6)
    with communication_read_uow(factory) as session:
        repository = SqlProjectCommunicationRepository(
            session,
            operational_contacts=OperationalContactService(SqlOperationalContactRepository(session)),
            superintendent_competency_id=superintendent_competency_id,
        )
        assignments = repository.list_assignments(week_start=week, week_end=end)
        business = build_project_communication_projection(
            week_start=week, week_end=end, assignments=assignments,
            request_provenance_version=4,
        )
        project_ids = tuple(project.project_id for project in business.projects)
        managers, manager_evidence = capture_erp_manager_authorities(session, project_ids)
        nominations, nomination_evidence = capture_rp_co_manager_authorities(session, project_ids)
        lookup = capture_manager_presentation(session, project_ids)
        population = RecipientPolicyPopulation.capture(repository.read_superintendent_population())
        direct, user_facts, resource_facts = _capture_assignment_inputs(
            session, assignments, project_ids,
        )
        by_manager = {row.principal.project_id: row for row in managers}
        by_nomination = {row.project_id: row for row in nominations}
        if (len(by_manager) != len(managers) or not set(by_manager) <= set(project_ids)
                or len(by_nomination) != len(nominations) or set(by_nomination) != set(project_ids)
                or any(row.nomination_source is None for row in nominations)):
            raise ValueError("Incomplete independent canonical project authority")

        messages = []
        direct_roots = set()
        nomination_roots = {row.primary.identity for group in nominations
                            for row in group.candidates}
        for project in business.projects:
            key = project.project_id
            nomination = by_nomination[key]
            sources = direct[key]
            requesters = {}
            for participant in project.requesters:
                role = ("REQUESTER", RecipientIdentity("USER", participant.user_id))
                previous = requesters.setdefault(role, participant)
                if previous != participant:
                    raise ValueError("Contradictory repeated requester presentation")
            requester_keys = tuple(requesters)
            assigned = {}
            for day in project.days:
                for task in day.tasks:
                    for resource in task.resources:
                        role = ("ASSIGNED", RecipientIdentity("RESOURCE", resource.resource_id))
                        previous = assigned.setdefault(role, resource.contact)
                        if previous != resource.contact:
                            raise ValueError("Contradictory repeated assigned presentation")
            assigned_keys = tuple(sorted(assigned, key=lambda role: (
                assigned[role].display_name.casefold(),
                _text(assigned[role].email).casefold(), role[1].id,
            )))
            keys = requester_keys + assigned_keys
            if set(keys) != set(sources):
                raise ValueError("Incomplete assignment recipient role coverage")
            direct_roots.update(identity for _, identity in keys)
            # Retain the originating v4 builder's chosen label when the same
            # user requests multiple shifts with different request-name fallbacks.
            requester_inputs = []
            for role in requester_keys:
                primary = sources[role].primary
                requester_inputs.append(RecipientAuthorityInput(RecipientCandidate(
                    primary.identity, requesters[role].display_name, primary.role,
                    primary.channel, primary.email, primary.address_source, primary.active,
                )))
            messages.append(MessageRecipientInputs(
                key, ((by_manager[key],) if key in by_manager else ())
                + nomination.candidates + tuple(requester_inputs)
                + tuple(sources[role] for role in assigned_keys),
                nomination.nomination_source,
            ))

        roots = direct_roots | nomination_roots | {
            RecipientIdentity("RESOURCE", row.employee.resource_id)
            for row in population.employees
        }
        principals = tuple(row.principal for row in managers)
        complete = read_recipient_identity_evidence(session, roots, erp_principals=principals)
        index = complete.index(erp_principals=principals)
        if not roots <= set(index.components):
            raise ValueError("Missing current persisted recipient identity root")
        if not user_facts <= set(complete.users) or not resource_facts <= set(complete.resources):
            raise ValueError("Contradictory assignment identity evidence")
        _corroborate_partial(manager_evidence, complete, index,
                             {principal.identity for principal in principals})
        _corroborate_partial(nomination_evidence, complete, index, nomination_roots)
        policy = RecipientPolicyContext.capture_canonical(
            population, complete, messages, manager_presentation=lookup,
        )
        result = replace(business, request_provenance_version=5, recipient_policy_context=policy)
    return result
