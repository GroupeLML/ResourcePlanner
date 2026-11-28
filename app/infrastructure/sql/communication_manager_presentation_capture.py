"""Inactive shared manager presentation acquisition for ADR-039 / ADR-017.

Reproduces the originating v4 lookup, not canonical recipient selection.
A shared Session does not prove transaction-wide coherence.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.project_managers import ProjectManagerResolutionService
from ...domain.communication_recipient_authority import (
    ManagerPresentationEvidence, ManagerPresentationLookup, ManagerPresentationResource,
)
from .business_contact_models import BusinessContact
from .communication_recipient_identity import _read_matches
from .identity_models import AppUser
from .models import Project, Resource
from .project_manager_resolution_repository import SqlProjectManagerResolutionRepository


def capture_manager_presentation(
    session: Session, project_ids: Iterable[str],
) -> ManagerPresentationLookup:
    """Read complete originating projected scope, including later excluded projects."""
    supplied = tuple(project_ids)
    if any(type(key) is not str or not key or key != key.strip() for key in supplied):
        raise ValueError("Expected exact presentation project scope")
    wanted = set(supplied)
    found = {key for key, in _read_matches(session, (Project.id,), Project.id, wanted)}
    effective = ProjectManagerResolutionService(
        SqlProjectManagerResolutionRepository(session),
    ).resolve_projects(sorted(wanted))
    if found != wanted or set(effective) != wanted:
        raise ValueError("Missing or inexact presentation project scope")

    # The existing resolver owns effective role suppression. Hidden nominations
    # do not add keys, even if they have separate canonical delivery authority.
    managers = []
    for key, projection in effective.items():
        if projection.primary is not None:
            managers.append((key, "MANAGER", projection.primary))
        managers.extend((key, "CO_MANAGER", row) for row in projection.co_managers)
    user_ids = {row.app_user_id for _, _, row in managers if row.app_user_id is not None}
    contact_ids = {row.business_contact_id for _, _, row in managers
                   if row.business_contact_id is not None}
    users = {row[0]: row[1:] for row in _read_matches(
        session, (AppUser.id, AppUser.employee_external_id, AppUser.business_contact_id,
                  AppUser.active, AppUser.display_name), AppUser.id, user_ids,
    )}
    contacts = {row[0]: row[1:] for row in _read_matches(
        session, (BusinessContact.id, BusinessContact.email, BusinessContact.active,
                  BusinessContact.display_name), BusinessContact.id, contact_ids,
    )}
    if set(users) != user_ids:
        raise ValueError("Missing resolved presentation user")

    roles = []
    for project_id, role, manager in managers:
        key = (manager.employee_external_id or "").strip() or None
        user_id = manager.app_user_id
        contact_id = manager.business_contact_id
        employee = linked_contact = user_active = user_name = None
        if user_id is not None:
            employee, linked_contact, user_active, user_name = users[user_id]
            linked_contact = (linked_contact or "").strip() or None
            user_active = bool(user_active)
            if (linked_contact != contact_id or user_active != manager.user_active
                    or ((employee or "").strip() or None) != key):
                raise ValueError("Contradictory persisted presentation user")
        contact_status = "ABSENT" if contact_id is None else "UNRESOLVED"
        raw = contact_active = contact_name = None
        if contact_id in contacts:
            raw, contact_active, contact_name = contacts[contact_id]
            contact_active = bool(contact_active)
            contact_status = "RESOLVED"
        if contact_active != manager.contact_active:
            raise ValueError("Contradictory persisted presentation contact")
        # ERP uses the canonical resolved employee identifier. RP uses only
        # the directly resolved AppUser employee identifier, never contact metadata.
        role_id = key if role == "MANAGER" else contact_id
        roles.append(ManagerPresentationEvidence(
            project_id, role, role_id, key, manager.resolution_status,
            manager.display_name, user_id, user_active, employee, linked_contact,
            user_name, contact_id, contact_status, contact_active, raw, contact_name,
        ))

    # Match the v4 presenter exactly: sort all effective keys and query batches
    # of 900, but filter returned normalized keys against the COMPLETE set.
    # A physical row returned in two partitions counts twice for legacy
    # uniqueness; only its immutable physical fact is deduplicated.
    keys = sorted({row.employee_key for row in roles if row.employee_key is not None})
    all_keys = set(keys)
    results = []
    for offset in range(0, len(keys), 900):
        rows = []
        for resource_id, employee, raw, active, erp_active in session.execute(
            select(Resource.id, Resource.external_id, Resource.email,
                   Resource.active, Resource.erp_active)
            .where(Resource.external_id.in_(keys[offset:offset + 900]))
        ):
            if (employee or "").strip() in all_keys:
                rows.append(ManagerPresentationResource(
                    resource_id, employee, raw, bool(active), bool(erp_active),
                ))
        results.append(tuple(rows))
    return ManagerPresentationLookup.capture(wanted, roles, results)
