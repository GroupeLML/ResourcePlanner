"""Capture the stable identity component of relevant recipient roots (ADR-039)."""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.communication_recipient_identity import (
    ContactIdentityFact,
    ERPPrincipalIdentityFact,
    RecipientIdentity,
    RecipientIdentityIndex,
    ResourceIdentityFact,
    UserIdentityFact,
)

from app.domain.communication_recipient_provenance import RecipientIdentityEvidence

from .business_contact_models import BusinessContact
from .identity_models import AppUser
from .models import Resource


def _read_matches(session, columns, match_column, values):
    ordered = sorted(values)
    match_index = next(index for index, column in enumerate(columns) if column is match_column)
    for offset in range(0, len(ordered), 900):
        subset = ordered[offset:offset + 900]
        # Recheck exact keys in Python: SQL Server collation may match variants.
        exact = set(subset)
        for row in session.execute(select(*columns).where(match_column.in_(subset))):
            if row[match_index] in exact:
                yield row


def read_recipient_identity_evidence(
    session: Session, roots: Iterable[RecipientIdentity],
    *, erp_principals: Iterable[ERPPrincipalIdentityFact] = (),
) -> RecipientIdentityEvidence:
    """Read stable links, including noneligible competitors, in bounded batches.

    Resolve the complete reachable identity components once per composition.
    Each search key is queried at most once per column; do not scope Resource
    competitors to active employees or to membership in the policy population.
    Typed ERP principals seed exact employee-key searches without requiring a
    local user/contact/Resource root. This read has no cache, performs no writes
    and does not select email authority.
    """
    pending = {kind: set() for kind in ("RESOURCE", "CONTACT", "USER", "EMPLOYEE")}
    for root in roots:
        pending[root.kind].add(root.id)
    for principal in erp_principals:
        if type(principal) is not ERPPrincipalIdentityFact:
            raise ValueError("Expected typed ERP principal authority")
        pending["EMPLOYEE"].add(principal.employee_external_id)
    searched = {kind: set() for kind in pending}
    resources: dict[str, ResourceIdentityFact] = {}
    contacts: dict[str, ContactIdentityFact] = {}
    users: dict[str, UserIdentityFact] = {}

    resource_columns = (Resource.id, Resource.external_id)
    contact_columns = (
        BusinessContact.id, BusinessContact.external_system,
        BusinessContact.external_entity, BusinessContact.external_id,
    )
    user_columns = (AppUser.id, AppUser.employee_external_id, AppUser.business_contact_id)

    def enqueue_employee(value):
        if value and value == value.strip():
            pending["EMPLOYEE"].add(value)

    def collect_resources(rows):
        for row in rows:
            fact = ResourceIdentityFact(*row)
            resources[fact.resource_id] = fact
            enqueue_employee(fact.employee_external_id)

    def collect_contacts(rows, *, employee_only=False):
        for row in rows:
            fact = ContactIdentityFact(*row)
            if employee_only and fact.employee_external_id is None:
                continue
            contacts[fact.contact_id] = fact
            pending["CONTACT"].add(fact.contact_id)
            enqueue_employee(fact.employee_external_id)

    def collect_users(rows):
        for row in rows:
            fact = UserIdentityFact(*row)
            users[fact.user_id] = fact
            enqueue_employee(fact.employee_external_id)
            if fact.business_contact_id:
                pending["CONTACT"].add(fact.business_contact_id)

    while any(pending[kind] - searched[kind] for kind in pending):
        for kind in pending:
            keys = pending[kind] - searched[kind]
            if not keys:
                continue
            searched[kind].update(keys)
            if kind == "RESOURCE":
                collect_resources(_read_matches(session, resource_columns, Resource.id, keys))
            elif kind == "CONTACT":
                collect_contacts(_read_matches(session, contact_columns, BusinessContact.id, keys))
                collect_users(_read_matches(session, user_columns, AppUser.business_contact_id, keys))
            elif kind == "USER":
                collect_users(_read_matches(session, user_columns, AppUser.id, keys))
            else:
                collect_resources(_read_matches(session, resource_columns, Resource.external_id, keys))
                collect_contacts(
                    _read_matches(session, contact_columns, BusinessContact.external_id, keys),
                    employee_only=True,
                )
                collect_users(_read_matches(session, user_columns, AppUser.employee_external_id, keys))
    return RecipientIdentityEvidence.capture(resources.values(), contacts.values(), users.values())


def read_recipient_identity_index(
    session: Session, roots: Iterable[RecipientIdentity],
) -> RecipientIdentityIndex:
    return read_recipient_identity_evidence(session, roots).index()
