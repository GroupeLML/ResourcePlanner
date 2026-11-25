"""Immutable ADR-039 policy-v1 message provenance (not live composition)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json

from .communication_recipient_authority import (
    AuthenticatedGeneratorAuthority, ERPManagerAuthority, RecipientAddressObservation,
)

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

    def index(self, *, erp_principals=(), authenticated_principals=()):
        return build_recipient_identity_index_v1(
            self.resources, self.contacts, self.users,
            erp_principals=erp_principals, authenticated_principals=authenticated_principals,
        )

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
        or candidate.address_source != sources.get(candidate.identity.kind)
        or not isinstance(candidate.display_name, str)
        or candidate.role not in ("MANAGER", "CO_MANAGER", "REQUESTER", "ASSIGNED", "GENERATOR")
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
        # Legacy direct inputs remain for the inactive Phase 2A capture. A
        # generator may only originate from typed authenticated authority.
        roles = {"MANAGER": ("CONTACT", "TO"), "CO_MANAGER": ("CONTACT", "TO"),
                 "REQUESTER": ("USER", "TO"), "ASSIGNED": ("RESOURCE", "CC")}
        if roles.get(self.primary.role) != (self.primary.identity.kind, self.primary.channel):
            raise ValueError("Unsupported direct recipient role and source authority")
        if self.resource_fallback is not None:
            _validate_candidate(self.resource_fallback)
            if (self.primary.identity.kind != "CONTACT" or self.resource_fallback.identity.kind != "RESOURCE"
                    or self.resource_fallback.role != self.primary.role
                    or self.resource_fallback.channel != self.primary.channel):
                raise ValueError("Invalid contact fallback authority")

    @property
    def observations(self):
        return tuple(RecipientAddressObservation(
            row.address_source, row.identity, "CURRENT", row.email,
        ) for row in (self.primary, self.resource_fallback) if row is not None)

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"primary", "resource_fallback", "observations"})
        fallback = data["resource_fallback"]
        result = cls(_read_candidate(data["primary"]), None if fallback is None else _read_candidate(fallback))
        if result.to_payload() != data:
            raise ValueError("Contradictory direct address observation")
        return result

    def to_payload(self):
        return _json_value({**asdict(self), "observations": [row.to_payload() for row in self.observations]})


@dataclass(frozen=True, slots=True)
class MessageRecipientInputs:
    project_id: str
    candidates: tuple[RecipientAuthorityInput | ERPManagerAuthority, ...]

    def __post_init__(self):
        if (not _nonblank_id(self.project_id) or not isinstance(self.candidates, tuple)
                or any(type(row) not in (RecipientAuthorityInput, ERPManagerAuthority) for row in self.candidates)):
            raise ValueError("Missing explicit message recipient inputs")
        for row in self.candidates:
            if type(row) is ERPManagerAuthority and row.principal.project_id != self.project_id:
                raise ValueError("ERP principal must originate from this project")
        if sum(type(row) is ERPManagerAuthority or row.primary.role == "MANAGER"
               for row in self.candidates) > 1:
            raise ValueError("Multiple principal manager claims")
        keys = [(row.principal.identity, "MANAGER", "TO") if type(row) is ERPManagerAuthority
                else (row.primary.identity, row.primary.role, row.primary.channel)
                for row in self.candidates]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate message recipient role claim")
        # Preserve established within-role order and canonical role precedence.
        priority = {"MANAGER": 0, "CO_MANAGER": 1, "REQUESTER": 2, "ASSIGNED": 3}
        object.__setattr__(self, "candidates", tuple(sorted(
            self.candidates, key=lambda row: 0 if type(row) is ERPManagerAuthority
            else priority[row.primary.role],
        )))

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"project_id", "candidates", "role_evidence"})
        def read(row):
            if isinstance(row, dict) and set(row) == {"erp_manager"}:
                return ERPManagerAuthority.from_payload(row["erp_manager"])
            return RecipientAuthorityInput.from_payload(row)
        result = cls(data["project_id"], _read_list(data["candidates"], read))
        if result.to_payload() != data:
            raise ValueError("Contradictory project role evidence")
        return result

    def to_payload(self):
        roles = []
        for row in self.candidates:
            if type(row) is ERPManagerAuthority:
                role, root, relationship = "MANAGER", row.principal.identity, "ERP_PROJECT_PRINCIPAL"
            else:
                role, root = row.primary.role, row.primary.identity
                relationship = {
                    "MANAGER": "PROVISIONAL_CONTACT_CAPTURE",
                    "CO_MANAGER": "RP_PROJECT_NOMINATION",
                    "REQUESTER": "PROJECT_REQUESTER",
                    "ASSIGNED": "PROJECT_ASSIGNMENT",
                }[role]
            roles.append({"project_id": self.project_id, "role": role, "identity": asdict(root),
                          "relationship": relationship})
        return _json_value({"project_id": self.project_id, "candidates": [
            {"erp_manager": row.to_payload()} if type(row) is ERPManagerAuthority else row.to_payload()
            for row in self.candidates
        ], "role_evidence": roles})


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
    generator: AuthenticatedGeneratorAuthority | None = None
    messages: tuple[MessageRecipientProvenance, ...] = field(init=False)

    def __post_init__(self):
        if (type(self.population) is not RecipientPolicyPopulation
                or type(self.identities) is not RecipientIdentityEvidence
                or not isinstance(self.message_inputs, tuple)
                or any(type(row) is not MessageRecipientInputs for row in self.message_inputs)):
            raise ValueError("Missing recipient policy context")
        if self.generator is not None and type(self.generator) is not AuthenticatedGeneratorAuthority:
            raise ValueError("Invalid generator option evidence")
        ids = [row.project_id for row in self.message_inputs]
        if ids != sorted(set(ids)):
            raise ValueError("Message coverage must be unique and ordered")
        index = self.index()
        roots = set(index.aliases) | index.ambiguous
        employees = {row.employee.resource_id: row.employee for row in self.population.employees}
        if any(RecipientIdentity("RESOURCE", key) not in roots for key in employees):
            raise ValueError("Missing eligible employee identity evidence")
        messages = []
        address_facts = {}
        for employee in employees.values():
            observation = RecipientAddressObservation(
                "RESOURCE_EMAIL", RecipientIdentity("RESOURCE", employee.resource_id),
                "CURRENT", employee.email,
            )
            address_facts[observation.consistency_key] = observation.raw_address
        manager_states = {}
        resource_states = {}
        resource_activity_flags = {}
        requester_states = {}
        active_contacts = set()
        for message in self.message_inputs:
            candidates = []
            for row in message.candidates:
                for observation in row.observations:
                    if observation.record not in roots:
                        raise ValueError("Missing recipient address authority record")
                    key = observation.consistency_key
                    if key in address_facts and address_facts[key] != observation.raw_address:
                        raise ValueError("Contradictory scoped raw recipient address authority")
                    address_facts[key] = observation.raw_address
                if type(row) is ERPManagerAuthority:
                    for kind, record, state in (
                        ("USER", row.linked_user_id, row.linked_user_active),
                        ("CONTACT", row.contact_id, row.contact_active),
                    ):
                        if record is not None:
                            key = RecipientIdentity(kind, record)
                            if key in manager_states and manager_states[key] != state:
                                raise ValueError("Contradictory manager source activity")
                            manager_states[key] = state
                            if kind == "USER" and key in requester_states and requester_states[key] != state:
                                raise ValueError("Contradictory requester/manager user activity")
                            if kind == "CONTACT" and state is not True and key in active_contacts:
                                raise ValueError("Contradictory resolved contact activity")
                    if row.resource_address is not None:
                        key = row.resource_address.record
                        flags = (row.resource_active, row.resource_erp_active)
                        if key in resource_activity_flags and resource_activity_flags[key] != flags:
                            raise ValueError("Contradictory fallback Resource activity flags")
                        resource_activity_flags[key] = flags
                        state = all(flags)
                        if key in resource_states and resource_states[key] != state:
                            raise ValueError("Contradictory Resource activity evidence")
                        resource_states[key] = state
                        if key.id in employees and not state:
                            raise ValueError("Inactive fallback Resource cannot be an eligible superintendent")
                    candidates.append(row.candidate(self.identities, index))
                    continue
                primary, fallback = row.primary, row.resource_fallback
                if primary.identity not in roots:
                    raise ValueError("Missing recipient identity evidence")
                if primary.role == "REQUESTER":
                    key = primary.identity
                    if ((key in requester_states and requester_states[key] != primary.active)
                            or (key in manager_states and manager_states[key] != primary.active)):
                        raise ValueError("Contradictory requester/manager user activity")
                    requester_states[key] = primary.active
                elif primary.identity.kind == "CONTACT" and primary.active:
                    key = primary.identity
                    if key in manager_states and manager_states[key] is not True:
                        raise ValueError("Contradictory resolved contact activity")
                    active_contacts.add(key)
                for resource in (primary if primary.role == "ASSIGNED" else None, fallback):
                    if resource is None:
                        continue
                    key = resource.identity
                    if key in resource_states and resource_states[key] != resource.active:
                        raise ValueError("Contradictory Resource activity evidence")
                    resource_states[key] = resource.active
                    if key.id in employees and not resource.active:
                        raise ValueError("Inactive Resource cannot be an eligible superintendent")
                if fallback is not None and not index.equivalent(primary.identity, fallback.identity):
                    raise ValueError("Unproven contact fallback")
                candidates.append(contact_rooted_candidate_v1(primary, fallback, index)
                                  if primary.identity.kind == "CONTACT" else primary)
            if self.generator is not None:
                candidates.append(self.generator.candidate)
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
    def capture(cls, population, identities, message_inputs, *, generator=None):
        return cls(population, identities, tuple(sorted(message_inputs, key=lambda row: row.project_id)), generator)

    def index(self):
        return self.identities.index(
            erp_principals=(row.principal for message in self.message_inputs
                            for row in message.candidates if type(row) is ERPManagerAuthority),
            authenticated_principals=() if self.generator is None else (self.generator.principal,),
        )

    def to_payload(self):
        index = self.index()

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
                            "message_inputs": [row.to_payload() for row in self.message_inputs],
                            "generator": None if self.generator is None else self.generator.to_payload(),
                            "generator_identity_fingerprint": None if self.generator is None else self.generator.identity_fingerprint,
                            "messages": [message(row) for row in self.messages]})

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"population", "identities", "message_inputs", "messages",
                                       "generator", "generator_identity_fingerprint"})
        context = cls(RecipientPolicyPopulation.from_payload(data["population"]),
                      RecipientIdentityEvidence.from_payload(data["identities"]),
                      _read_list(data["message_inputs"], MessageRecipientInputs.from_payload),
                      None if data["generator"] is None else AuthenticatedGeneratorAuthority.from_payload(data["generator"]))
        # Compare canonical JSON rather than Python's bool/int-coercing equality.
        if json.dumps(context.to_payload(), sort_keys=True, allow_nan=False) != json.dumps(data, sort_keys=True, allow_nan=False):
            raise ValueError("Contradictory or incomplete recipient provenance claims")
        return context
