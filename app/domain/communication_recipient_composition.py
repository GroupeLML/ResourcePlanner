"""Inactive ADR-039 composition primitives; no live scheduling integration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Literal

from .communication_recipient_identity import RecipientIdentity, RecipientIdentityIndex
from .superintendent_population import SuperintendentPopulation
from .superintendent_recipient_resolution import (
    AddressFailure,
    AddressResolution,
    EmployeeAddressDiagnostic,
    SuperintendentAddress,
    resolve_address_v1,
    resolve_superintendent_addresses_v1,
)


Channel = Literal["TO", "CC"]
AddressSource = Literal["RESOURCE_EMAIL", "BUSINESS_CONTACT_EMAIL", "APP_USER_EMAIL",
                        "AUTHENTICATED_EMAIL", "NO_ADDRESS_AUTHORITY"]


@dataclass(frozen=True)
class RecipientCandidate:
    identity: RecipientIdentity
    display_name: str
    role: str
    channel: Channel
    email: str | None
    address_source: AddressSource
    active: bool = True


def contact_rooted_candidate_v1(
    candidate: RecipientCandidate,
    resource: RecipientCandidate | None,
    identities: RecipientIdentityIndex,
) -> RecipientCandidate:
    """Keep contact authority; only a missing address permits a proven Resource fallback."""
    if candidate.identity.kind != "CONTACT" or candidate.address_source != "BUSINESS_CONTACT_EMAIL":
        raise ValueError("Expected a contact-rooted candidate")
    if (
        resolve_address_v1(candidate.email).failure == AddressFailure.MISSING
        and resource is not None
        and resource.identity.kind == "RESOURCE"
        and resource.address_source == "RESOURCE_EMAIL"
        and identities.equivalent(candidate.identity, resource.identity)
    ):
        return replace(candidate, email=resource.email, address_source="RESOURCE_EMAIL")
    return candidate


@dataclass(frozen=True)
class RecipientContribution:
    identity: RecipientIdentity
    display_name: str
    role: str
    requested_channel: Channel
    address_source: AddressSource
    resolution: AddressResolution


@dataclass(frozen=True)
class EffectiveRecipient:
    channel: Channel
    address: str
    selected: RecipientContribution
    contributions: tuple[RecipientContribution, ...]


@dataclass(frozen=True)
class MandatoryResolution:
    employee: SuperintendentAddress
    effective_address: str | None
    fulfilled_by_existing: bool

    @property
    def blocker(self) -> EmployeeAddressDiagnostic | None:
        return self.employee.diagnostic if self.effective_address is None else None


@dataclass(frozen=True)
class RecipientComposition:
    population: SuperintendentPopulation
    recipients: tuple[EffectiveRecipient, ...]
    mandatory: tuple[MandatoryResolution, ...]
    unusable_existing: tuple[RecipientContribution, ...]

    @property
    def blockers(self) -> tuple[EmployeeAddressDiagnostic, ...]:
        return tuple(row.blocker for row in self.mandatory if row.blocker is not None)

    @property
    def mandatory_complete(self) -> bool:
        return self.population.complete and not self.blockers


def _contribution(candidate: RecipientCandidate) -> RecipientContribution:
    return RecipientContribution(
        candidate.identity, candidate.display_name, candidate.role, candidate.channel,
        candidate.address_source, resolve_address_v1(candidate.email),
    )


def compose_mandatory_recipients_v1(
    population: SuperintendentPopulation,
    existing: Iterable[RecipientCandidate],
    identities: RecipientIdentityIndex,
) -> RecipientComposition:
    """Deduplicate identity first, then address; retain contributing roots/roles.

    Existing valid To precedes existing valid CC, then mandatory CC. An invalid
    mandatory address can only be fulfilled by a valid existing recipient of
    its proven identity, never by a shared name/address or an ambiguous link.
    """
    by_identity: dict[RecipientIdentity, EffectiveRecipient] = {}
    unusable: list[RecipientContribution] = []
    # Stable sort preserves caller order within each established recipient role.
    for candidate in sorted(existing, key=lambda row: row.channel != "TO"):
        if not candidate.active:
            continue
        contribution = _contribution(candidate)
        address = contribution.resolution.address
        if address is None:
            unusable.append(contribution)
            continue
        key = identities.canonical(candidate.identity)
        if key in by_identity:
            old = by_identity[key]
            by_identity[key] = replace(old, contributions=old.contributions + (contribution,))
        else:
            by_identity[key] = EffectiveRecipient(candidate.channel, address, contribution, (contribution,))

    existing_keys = frozenset(by_identity)
    mandatory: list[MandatoryResolution] = []
    for row in resolve_superintendent_addresses_v1(population):
        identity = RecipientIdentity("RESOURCE", row.employee.resource_id)
        key = identities.canonical(identity)
        contribution = RecipientContribution(
            identity, row.employee.display_name, "SUPERINTENDENT", "CC", "RESOURCE_EMAIL", row.resolution,
        )
        if key in existing_keys:
            old = by_identity[key]
            by_identity[key] = replace(old, contributions=old.contributions + (contribution,))
            mandatory.append(MandatoryResolution(row, old.address, True))
        elif row.resolution.address is not None:
            address = row.resolution.address
            by_identity[key] = EffectiveRecipient("CC", address, contribution, (contribution,))
            mandatory.append(MandatoryResolution(row, address, False))
        else:
            mandatory.append(MandatoryResolution(row, None, False))

    by_address: dict[str, EffectiveRecipient] = {}
    for recipient in by_identity.values():
        key = recipient.address.casefold()
        if key in by_address:
            old = by_address[key]
            by_address[key] = replace(old, contributions=old.contributions + recipient.contributions)
        else:
            by_address[key] = recipient
    # Report the actual selected envelope spelling even when distinct identities share it.
    mandatory = [
        replace(row, effective_address=by_address[row.effective_address.casefold()].address)
        if row.effective_address is not None else row
        for row in mandatory
    ]
    return RecipientComposition(population, tuple(by_address.values()), tuple(mandatory), tuple(unusable))


# Preserve the historical v1 composition authority for frozen recipient contexts.
contact_rooted_candidate = contact_rooted_candidate_v1
compose_mandatory_recipients = compose_mandatory_recipients_v1
