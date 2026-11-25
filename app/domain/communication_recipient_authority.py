"""Typed source evidence for the undeployed ADR-039 recipient domain.

Only trusted server capture may create these facts. This module does not read
the database, accept client authority, or activate scheduling generation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json

from .communication_recipient_composition import RecipientCandidate
from .communication_recipient_identity import (
    AuthenticatedPrincipalIdentityFact, ERPPrincipalIdentityFact, RecipientIdentity,
    RecipientIdentityIndex,
)
from .communication_recipient_policy import _nonblank_id, _require_keys
from .superintendent_recipient_resolution import AddressFailure, AddressResolution, resolve_address_v1


@dataclass(frozen=True, slots=True)
class RecipientAddressObservation:
    """Address source and scope, independent of the recipient's person identity."""
    source: str
    record: RecipientIdentity
    scope: str
    raw_address: str | None
    resolution: AddressResolution = field(init=False)

    def __post_init__(self):
        if type(self.record) is not RecipientIdentity:
            raise ValueError("Missing address authority record")
        allowed = {
            "RESOURCE_EMAIL": ("RESOURCE", "CURRENT"),
            "BUSINESS_CONTACT_EMAIL": ("CONTACT", "CURRENT"),
            "APP_USER_EMAIL": ("USER", "CURRENT"),
            "AUTHENTICATED_EMAIL": ("AUTHENTICATED_PRINCIPAL", "AUTHENTICATED"),
        }
        if (type(self.source) is not str or self.source not in allowed
                or allowed[self.source] != (self.record.kind, self.scope)
                or (self.raw_address is not None and type(self.raw_address) is not str)):
            raise ValueError("Invalid address authority or observation scope")
        object.__setattr__(self, "resolution", resolve_address_v1(self.raw_address))

    @property
    def consistency_key(self):
        return self.source, self.record, self.scope

    def to_payload(self):
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"source", "record", "scope", "raw_address", "resolution"})
        record = RecipientIdentity(**_require_keys(data["record"], {"kind", "id"}))
        result = cls(data["source"], record, data["scope"], data["raw_address"])
        if result.to_payload() != data:
            raise ValueError("Contradictory address observation resolution")
        return result


@dataclass(frozen=True, slots=True)
class ERPManagerAuthority:
    """Independent ERP principal, local resolution, contact and Resource evidence.

    A missing/unresolved contact is not a fabricated contact or AppUser email.
    A fallback observation is retained separately, even when not selected.
    """
    principal: ERPPrincipalIdentityFact
    display_name: str
    linked_user_id: str | None
    linked_user_active: bool | None
    contact_id: str | None
    contact_status: str
    contact_active: bool | None
    contact_address: RecipientAddressObservation | None
    resource_address: RecipientAddressObservation | None
    resource_active: bool | None = None
    resource_erp_active: bool | None = None

    def __post_init__(self):
        if (type(self.principal) is not ERPPrincipalIdentityFact
                or type(self.display_name) is not str
                or (self.linked_user_id is not None and not _nonblank_id(self.linked_user_id))
                or (self.contact_id is not None and not _nonblank_id(self.contact_id))
                or (self.linked_user_id is None and self.linked_user_active is not None)
                or (self.linked_user_id is not None and type(self.linked_user_active) is not bool)):
            raise ValueError("Invalid ERP manager identity or user resolution")
        if self.contact_status == "RESOLVED":
            if (self.linked_user_id is None or self.contact_id is None
                    or type(self.contact_active) is not bool
                    or type(self.contact_address) is not RecipientAddressObservation
                    or self.contact_address.source != "BUSINESS_CONTACT_EMAIL"
                    or self.contact_address.record != RecipientIdentity("CONTACT", self.contact_id)):
                raise ValueError("Missing resolved manager contact evidence")
        elif self.contact_status in ("ABSENT", "UNRESOLVED"):
            if (self.contact_active is not None or self.contact_address is not None
                    or (self.contact_status == "ABSENT" and self.contact_id is not None)
                    or (self.contact_status == "UNRESOLVED" and
                        (self.contact_id is None or self.linked_user_id is None))):
                raise ValueError("Contradictory manager contact resolution")
        else:
            raise ValueError("Unknown manager contact resolution")
        if self.resource_address is not None and (
                type(self.resource_address) is not RecipientAddressObservation
                or self.resource_address.source != "RESOURCE_EMAIL"):
            raise ValueError("Invalid ERP manager Resource fallback authority")
        if self.resource_address is None:
            if self.resource_active is not None or self.resource_erp_active is not None:
                raise ValueError("Resource activity without Resource authority")
        elif type(self.resource_active) is not bool or type(self.resource_erp_active) is not bool:
            raise ValueError("Missing fallback Resource activity evidence")

    @property
    def observations(self):
        return tuple(row for row in (self.contact_address, self.resource_address) if row is not None)

    def validate(self, evidence, index: RecipientIdentityIndex):
        employee = self.principal.employee_external_id
        users = tuple(row for row in evidence.users if row.employee_external_id == employee)
        if len(users) == 1:
            if self.linked_user_id != users[0].user_id:
                raise ValueError("Missing canonical ERP manager user resolution")
        elif self.linked_user_id is not None:
            raise ValueError("Ambiguous or unproven ERP manager user association")
        if self.linked_user_id is not None:
            user = users[0]
            if user.business_contact_id != self.contact_id:
                raise ValueError("Contradictory manager user/contact link")
            contact_exists = any(row.contact_id == self.contact_id for row in evidence.contacts)
            expected_status = ("ABSENT" if self.contact_id is None else
                               "RESOLVED" if contact_exists else "UNRESOLVED")
            if self.contact_status != expected_status:
                raise ValueError("Contradictory manager contact resolution evidence")
        elif self.contact_status != "ABSENT":
            raise ValueError("Unproven ERP manager contact")
        matches = tuple(row for row in evidence.resources
                        if row.employee_external_id == employee)
        proven_resource = (RecipientIdentity("RESOURCE", matches[0].resource_id)
                           if len(matches) == 1 else None)
        if proven_resource is not None and not index.equivalent(self.principal.identity, proven_resource):
            proven_resource = None
        if self.resource_address is not None:
            if self.resource_address.record != proven_resource:
                raise ValueError("Unproven unique ERP manager Resource fallback")
        elif (proven_resource is not None
                and (self.contact_address is None
                     or self.contact_address.resolution.failure == AddressFailure.MISSING)):
            raise ValueError("Missing proven ERP manager Resource address evidence")

    def candidate(self, evidence, index: RecipientIdentityIndex) -> RecipientCandidate:
        self.validate(evidence, index)
        observation = self.contact_address
        if (observation is None or observation.resolution.failure == AddressFailure.MISSING):
            if self.resource_address is not None:
                observation = self.resource_address
        active = (self.linked_user_active is not False and self.contact_active is not False
                  and (self.contact_status == "RESOLVED" or
                       (observation is not None and observation.source == "RESOURCE_EMAIL"
                        and observation.resolution.failure != AddressFailure.MISSING)))
        return RecipientCandidate(
            self.principal.identity, self.display_name, "MANAGER", "TO",
            observation.raw_address if observation is not None else None,
            observation.source if observation is not None else "NO_ADDRESS_AUTHORITY",
            active,
        )

    def to_payload(self):
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {
            "principal", "display_name", "linked_user_id", "linked_user_active",
            "contact_id", "contact_status", "contact_active", "contact_address", "resource_address",
            "resource_active", "resource_erp_active",
        })
        result = cls(**{
            **data,
            "principal": ERPPrincipalIdentityFact(**_require_keys(
                data["principal"], {"project_id", "employee_external_id"})),
            **{key: None if data[key] is None else RecipientAddressObservation.from_payload(data[key])
               for key in ("contact_address", "resource_address")},
        })
        if result.to_payload() != data:
            raise ValueError("Contradictory ERP manager source evidence")
        return result


@dataclass(frozen=True, slots=True)
class AuthenticatedGeneratorAuthority:
    """Optional generator source captured from the authenticated server principal."""
    principal: AuthenticatedPrincipalIdentityFact
    display_name: str
    address: RecipientAddressObservation

    def __post_init__(self):
        if (type(self.principal) is not AuthenticatedPrincipalIdentityFact
                or type(self.display_name) is not str
                or type(self.address) is not RecipientAddressObservation
                or self.address.source != "AUTHENTICATED_EMAIL"
                or self.address.record != self.principal.identity
                or self.address.resolution.address is None):
            raise ValueError("Invalid authenticated generator authority")

    @property
    def identity_fingerprint(self):
        # Preserve the existing preview-to-preparation generator fingerprint.
        return hashlib.sha256("\0".join((
            "project-communication-generator-cc-v1", self.principal.issuer,
            self.principal.subject, self.principal.local_user_id or "",
            self.address.resolution.address.casefold(),
        )).encode("utf-8")).hexdigest()

    @property
    def candidate(self):
        return RecipientCandidate(
            self.principal.identity, self.display_name, "GENERATOR", "CC",
            self.address.raw_address, self.address.source,
        )

    def to_payload(self):
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"principal", "display_name", "address"})
        result = cls(
            AuthenticatedPrincipalIdentityFact(**_require_keys(
                data["principal"], {"issuer", "subject", "local_user_id"})),
            data["display_name"], RecipientAddressObservation.from_payload(data["address"]),
        )
        if result.to_payload() != data:
            raise ValueError("Contradictory authenticated generator evidence")
        return result
