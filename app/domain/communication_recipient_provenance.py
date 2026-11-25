"""Immutable ADR-039 policy-v1 message provenance (not live composition)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json

from .communication_recipient_composition import (
    EffectiveRecipient, RecipientCandidate, RecipientContribution,
    compose_mandatory_recipients_v1, contact_rooted_candidate_v1,
)
from .communication_recipient_identity import (
    ContactIdentityFact, RecipientIdentity, ResourceIdentityFact, UserIdentityFact,
    build_recipient_identity_index_v1,
)
from .communication_recipient_policy import RecipientPolicyPopulation, _nonblank_id, _require_keys
from .superintendent_recipient_resolution import AddressFailure


def _json_value(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _read_fact(cls, payload):
    return cls(**_require_keys(payload, {item.name for item in fields(cls)}))


def _read_list(value, read):
    if not isinstance(value, list):
        raise ValueError("Expected explicit recipient provenance list")
    return tuple(read(item) for item in value)


@dataclass(frozen=True, slots=True)
class RecipientIdentityEvidence:
    """Complete reachable stable-link facts, including competing identities."""
    resources: tuple[ResourceIdentityFact, ...]
    contacts: tuple[ContactIdentityFact, ...]
    users: tuple[UserIdentityFact, ...]

    def __post_init__(self):
        for rows, cls, key in (
            (self.resources, ResourceIdentityFact, "resource_id"),
            (self.contacts, ContactIdentityFact, "contact_id"),
            (self.users, UserIdentityFact, "user_id"),
        ):
            if not isinstance(rows, tuple):
                raise ValueError("Identity evidence must be immutable")
            ids = []
            for row in rows:
                if type(row) is not cls or not _nonblank_id(getattr(row, key)):
                    raise ValueError("Missing stable identity fact")
                if any(value is not None and not isinstance(value, str) for value in asdict(row).values()):
                    raise ValueError("Invalid stable identity fact")
                ids.append(getattr(row, key))
            if ids != sorted(set(ids)):
                raise ValueError("Identity evidence must be unique and ordered")

    @classmethod
    def capture(cls, resources, contacts, users):
        return cls(tuple(sorted(resources, key=lambda row: row.resource_id)),
                   tuple(sorted(contacts, key=lambda row: row.contact_id)),
                   tuple(sorted(users, key=lambda row: row.user_id)))

    def index(self):
        return build_recipient_identity_index_v1(self.resources, self.contacts, self.users)

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"resources", "contacts", "users"})
        return cls(_read_list(data["resources"], lambda row: _read_fact(ResourceIdentityFact, row)),
                   _read_list(data["contacts"], lambda row: _read_fact(ContactIdentityFact, row)),
                   _read_list(data["users"], lambda row: _read_fact(UserIdentityFact, row)))


def _validate_candidate(candidate):
    sources = {"RESOURCE": "RESOURCE_EMAIL", "CONTACT": "BUSINESS_CONTACT_EMAIL", "USER": "APP_USER_EMAIL"}
    if (
        type(candidate) is not RecipientCandidate
        or type(candidate.identity) is not RecipientIdentity
        or not _nonblank_id(candidate.identity.id)
        or candidate.address_source != sources[candidate.identity.kind]
        or not isinstance(candidate.display_name, str)
        or not _nonblank_id(candidate.role) or candidate.role == "SUPERINTENDENT"
        or candidate.channel not in ("TO", "CC")
        or type(candidate.active) is not bool
        or (candidate.email is not None and not isinstance(candidate.email, str))
    ):
        raise ValueError("Invalid recipient input or address authority")


def _read_candidate(payload):
    data = _require_keys(payload, {item.name for item in fields(RecipientCandidate)})
    identity = _require_keys(data["identity"], {"kind", "id"})
    if not isinstance(identity["kind"], str) or not _nonblank_id(identity["id"]):
        raise ValueError("Invalid recipient identity")
    return RecipientCandidate(**{**data, "identity": RecipientIdentity(**identity)})


@dataclass(frozen=True, slots=True)
class RecipientAuthorityInput:
    """Primary authority and original Resource fallback facts, before resolution."""
    primary: RecipientCandidate
    resource_fallback: RecipientCandidate | None = None

    def __post_init__(self):
        _validate_candidate(self.primary)
        if self.resource_fallback is not None:
            _validate_candidate(self.resource_fallback)
            if self.primary.identity.kind != "CONTACT" or self.resource_fallback.identity.kind != "RESOURCE":
                raise ValueError("Invalid contact fallback authority")

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"primary", "resource_fallback"})
        fallback = data["resource_fallback"]
        return cls(_read_candidate(data["primary"]), None if fallback is None else _read_candidate(fallback))


@dataclass(frozen=True, slots=True)
class MessageRecipientInputs:
    project_id: str
    candidates: tuple[RecipientAuthorityInput, ...]

    def __post_init__(self):
        if (not _nonblank_id(self.project_id) or not isinstance(self.candidates, tuple)
                or any(type(row) is not RecipientAuthorityInput for row in self.candidates)):
            raise ValueError("Missing explicit message recipient inputs")
        keys = [(row.primary.identity, row.primary.role, row.primary.channel) for row in self.candidates]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate message recipient role claim")
        # Preserve same-channel authority precedence; cross-channel order is immaterial.
        object.__setattr__(self, "candidates", tuple(sorted(self.candidates, key=lambda row: row.primary.channel != "TO")))

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"project_id", "candidates"})
        return cls(data["project_id"], _read_list(data["candidates"], RecipientAuthorityInput.from_payload))


@dataclass(frozen=True, slots=True)
class MandatoryRecipientFact:
    resource_id: str
    effective_address: str | None
    fulfilled_by_existing: bool
    blocker_reason: AddressFailure | None


@dataclass(frozen=True, slots=True)
class MessageRecipientProvenance:
    project_id: str
    recipients: tuple[EffectiveRecipient, ...]
    mandatory: tuple[MandatoryRecipientFact, ...]
    unusable_existing: tuple[RecipientContribution, ...]
    mandatory_complete: bool


@dataclass(frozen=True, slots=True)
class RecipientPolicyContext:
    """One global roster and identity evidence; one immutable result per project.

    Candidate order is authority order (including same-channel precedence), not
    incidental input order. Global identities and project IDs are canonical sets.
    Reconstruct with the frozen v1 primitives, never current master data.
    """
    population: RecipientPolicyPopulation
    identities: RecipientIdentityEvidence
    message_inputs: tuple[MessageRecipientInputs, ...]
    messages: tuple[MessageRecipientProvenance, ...] = field(init=False)

    def __post_init__(self):
        if (type(self.population) is not RecipientPolicyPopulation
                or type(self.identities) is not RecipientIdentityEvidence
                or not isinstance(self.message_inputs, tuple)
                or any(type(row) is not MessageRecipientInputs for row in self.message_inputs)):
            raise ValueError("Missing recipient policy context")
        ids = [row.project_id for row in self.message_inputs]
        if ids != sorted(set(ids)):
            raise ValueError("Message coverage must be unique and ordered")
        index = self.identities.index()
        roots = set(index.aliases) | index.ambiguous
        employees = {row.employee.resource_id: row.employee for row in self.population.employees}
        if any(RecipientIdentity("RESOURCE", key) not in roots for key in employees):
            raise ValueError("Missing eligible employee identity evidence")
        messages = []
        address_facts: dict[RecipientIdentity, str | None] = {}
        for message in self.message_inputs:
            candidates = []
            for row in message.candidates:
                primary, fallback = row.primary, row.resource_fallback
                for candidate in (primary, fallback):
                    if candidate is None:
                        continue
                    if candidate.identity not in roots:
                        raise ValueError("Missing recipient identity evidence")
                    if candidate.identity in address_facts and address_facts[candidate.identity] != candidate.email:
                        raise ValueError("Contradictory raw recipient address authority")
                    address_facts[candidate.identity] = candidate.email
                    if candidate.identity.kind == "RESOURCE" and candidate.identity.id in employees:
                        if candidate.email != employees[candidate.identity.id].email:
                            raise ValueError("Contradictory canonical Resource.email")
                if fallback is not None and not index.equivalent(primary.identity, fallback.identity):
                    raise ValueError("Unproven contact fallback")
                candidates.append(contact_rooted_candidate_v1(primary, fallback, index)
                                  if primary.identity.kind == "CONTACT" else primary)
            result = compose_mandatory_recipients_v1(self.population.to_population(), candidates, index)
            messages.append(MessageRecipientProvenance(
                message.project_id, result.recipients,
                tuple(MandatoryRecipientFact(row.employee.employee.resource_id, row.effective_address,
                                             row.fulfilled_by_existing, row.blocker.reason if row.blocker else None)
                      for row in result.mandatory),
                result.unusable_existing, result.mandatory_complete,
            ))
        object.__setattr__(self, "messages", tuple(messages))

    @classmethod
    def capture(cls, population, identities, message_inputs):
        return cls(population, identities, tuple(sorted(message_inputs, key=lambda row: row.project_id)))

    def to_payload(self):
        index = self.identities.index()

        def contribution(row):
            return {**asdict(row), "canonical_identity": asdict(index.canonical(row.identity))}

        def message(row):
            return {
                "project_id": row.project_id,
                "recipients": [{"channel": item.channel, "address": item.address,
                                "selected": contribution(item.selected),
                                "contributions": [contribution(value) for value in item.contributions]}
                               for item in row.recipients],
                "mandatory": [asdict(item) for item in row.mandatory],
                "unusable_existing": [contribution(item) for item in row.unusable_existing],
                "mandatory_complete": row.mandatory_complete,
            }

        return _json_value({"population": self.population.to_payload(), "identities": asdict(self.identities),
                            "message_inputs": [asdict(row) for row in self.message_inputs],
                            "messages": [message(row) for row in self.messages]})

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"population", "identities", "message_inputs", "messages"})
        context = cls(RecipientPolicyPopulation.from_payload(data["population"]),
                      RecipientIdentityEvidence.from_payload(data["identities"]),
                      _read_list(data["message_inputs"], MessageRecipientInputs.from_payload))
        # Compare canonical JSON rather than Python's bool/int-coercing equality.
        if json.dumps(context.to_payload(), sort_keys=True, allow_nan=False) != json.dumps(data, sort_keys=True, allow_nan=False):
            raise ValueError("Contradictory or incomplete recipient provenance claims")
        return context
