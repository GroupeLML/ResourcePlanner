"""Inactive authenticated generator capture for ADR-039.

Only call with a trusted server-resolved AuthPrincipal, never request fields.
This is not an authentication provider or an operational scheduling entry point.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...application.break_glass import BREAK_GLASS_AUTH_MODE, BREAK_GLASS_ISSUER
from ...application.errors import ApplicationValidationError
from ...application.security import AuthPrincipal
from ...domain.communication_recipient_authority import (
    AuthenticatedGeneratorAuthority, RecipientAddressObservation,
)
from ...domain.communication_recipient_identity import (
    AuthenticatedPrincipalIdentityFact, RecipientIdentity, UserIdentityFact,
)
from ...domain.communication_recipient_provenance import RecipientIdentityEvidence, RecipientPolicyContext
from ...domain.superintendent_recipient_resolution import resolve_address_v1
from .communication_recipient_identity import _read_matches, read_recipient_identity_evidence
from .identity_models import AppUser


def capture_generator_context(
    session: Session,
    current: RecipientPolicyContext,
    *,
    add_generator_cc: bool,
    principal: AuthPrincipal | None = None,
) -> RecipientPolicyContext:
    """Add optional original generator evidence to complete current-only facts.

    Re-read the generator's reachable component once, including competitors,
    to corroborate any already-captured current component rather than repair
    it. Disjoint persisted evidence is composition-only. The accepted domain
    boundary checks for overlap, including ERP-only current roots. No shared
    Session alone establishes transaction-wide coherence.
    """
    if type(current) is not RecipientPolicyContext or current.generator is not None:
        raise ValueError("Expected current-only recipient context")
    if type(add_generator_cc) is not bool:
        raise ValueError("Expected explicit generator option")
    if not add_generator_cc:
        return current

    if type(principal) is not AuthPrincipal or resolve_address_v1(principal.email).address is None:
        raise ApplicationValidationError(
            "Le compte authentifie ne possede pas de courriel valide pour la copie CC.",
            code="project_communication_generator_email_invalid",
        )
    identity = AuthenticatedPrincipalIdentityFact(
        principal.issuer, principal.subject, principal.local_user_id,
    )
    generator = AuthenticatedGeneratorAuthority(
        identity, principal.display_name,
        RecipientAddressObservation("AUTHENTICATED_EMAIL", identity.identity,
                                    "AUTHENTICATED", principal.email),
    )
    extension = None
    if identity.local_user_id is not None:
        key = identity.local_user_id
        rows = tuple(_read_matches(
            session, (AppUser.id, AppUser.issuer, AppUser.subject, AppUser.active,
                      AppUser.employee_external_id, AppUser.business_contact_id),
            AppUser.id, {key},
        ))
        if len(rows) != 1 or not rows[0][3]:
            raise ValueError("Missing or inactive authenticated local association")
        _, issuer, subject, _, employee, contact = rows[0]
        # Match the existing SQL session resolver's break-glass fallback.
        # OIDC and materialized local accounts instead use their stored pair.
        # Do not resolve by email/name/employee, or read AppUser.email.
        if principal.auth_mode == BREAK_GLASS_AUTH_MODE and issuer is None and subject is None:
            issuer, subject = BREAK_GLASS_ISSUER, f"app-user:{key}"
        if (issuer, subject) != (identity.issuer, identity.subject):
            raise ValueError("Contradictory authenticated local association")

        root = RecipientIdentity("USER", key)
        observed = read_recipient_identity_evidence(session, (root,))
        if UserIdentityFact(key, employee, contact) not in observed.users:
            raise ValueError("Authenticated local association changed during capture")

        component = current.current_index().components.get(root)
        if component is None:
            extension = observed
        else:
            # Compare the persisted part of this component, including ambiguous
            # members. ERP roots are derived from project facts, not local rows.
            expected = RecipientIdentityEvidence.capture(
                (row for row in current.identities.resources
                 if RecipientIdentity("RESOURCE", row.resource_id) in component),
                (row for row in current.identities.contacts
                 if RecipientIdentity("CONTACT", row.contact_id) in component),
                (row for row in current.identities.users
                 if RecipientIdentity("USER", row.user_id) in component),
            )
            if observed != expected:
                raise ValueError("Generator contradicts complete current identity closure")

    return RecipientPolicyContext.capture(
        current.population, current.identities, current.message_inputs,
        generator=generator, composition_identities=extension,
    )
