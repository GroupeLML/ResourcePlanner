"""Immutable ADR-039 policy-v1 message provenance (not live composition)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import hashlib
import json

from .communication_recipient_authority import (
    AuthenticatedGeneratorAuthority, ERPManagerAuthority, ManagerPresentationLookup,
    RecipientAddressObservation,
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
class RPNominationSource:
    """Independent raw ProjectCoManager coverage, not a derived role claim.

    This frozen record is structural evidence, not authentication of its
    acquisition. Only the trusted persisted adapter may establish completeness.
    """
    project_id: str
    source_kind: str
    contact_ids: tuple[str, ...] | None

    def __post_init__(self):
        if not _nonblank_id(self.project_id):
            raise ValueError("Invalid RP nomination source project")
        if self.source_kind == "UNAVAILABLE_PROVISIONAL":
            if self.contact_ids is not None:
                raise ValueError("Unavailable RP nomination source cannot claim coverage")
        elif self.source_kind == "CANONICAL_PERSISTED":
            if (type(self.contact_ids) is not tuple
                    or any(type(key) is not str or not _nonblank_id(key) for key in self.contact_ids)
                    or list(self.contact_ids) != sorted(set(self.contact_ids))):
                raise ValueError("RP nomination source identities must be unique and ordered")
        else:
            raise ValueError("Unknown RP nomination source kind")

    @classmethod
    def capture(cls, project_id, contact_ids):
        return cls(project_id, "CANONICAL_PERSISTED", tuple(sorted(contact_ids)))

    def to_payload(self):
        return _json_value(asdict(self))

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"project_id", "source_kind", "contact_ids"})
        ids = data["contact_ids"]
        return cls(data["project_id"], data["source_kind"],
                   None if ids is None else _read_list(ids, lambda item: item))


@dataclass(frozen=True, slots=True)
class MessageRecipientInputs:
    project_id: str
    candidates: tuple[RecipientAuthorityInput | ERPManagerAuthority, ...]
    # None means unavailable, never complete empty. Serialize it explicitly.
    nomination_source: RPNominationSource | None = None

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
        if self.nomination_source is not None:
            source = self.nomination_source
            if (type(source) is not RPNominationSource or source.project_id != self.project_id
                    or source.source_kind != "CANONICAL_PERSISTED"):
                raise ValueError("Invalid or cross-project canonical RP nomination source")
            candidates = {row.primary.identity.id for row in self.candidates
                          if type(row) is RecipientAuthorityInput and row.primary.role == "CO_MANAGER"}
            if candidates != set(source.contact_ids):
                raise ValueError("RP nomination candidate coverage differs from persisted source")
            if any(type(row) is RecipientAuthorityInput and row.primary.role == "MANAGER"
                   for row in self.candidates):
                raise ValueError("Canonical RP nomination coverage cannot use provisional manager authority")
        # Preserve established within-role order and canonical role precedence.
        priority = {"MANAGER": 0, "CO_MANAGER": 1, "REQUESTER": 2, "ASSIGNED": 3}
        object.__setattr__(self, "candidates", tuple(sorted(
            self.candidates, key=lambda row: 0 if type(row) is ERPManagerAuthority
            else priority[row.primary.role],
        )))

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"project_id", "candidates", "role_evidence", "nomination_source"})
        def read(row):
            if isinstance(row, dict) and set(row) == {"erp_manager"}:
                return ERPManagerAuthority.from_payload(row["erp_manager"])
            return RecipientAuthorityInput.from_payload(row)
        source = RPNominationSource.from_payload(data["nomination_source"])
        if source.project_id != data["project_id"]:
            raise ValueError("Cross-project RP nomination source")
        result = cls(data["project_id"], _read_list(data["candidates"], read),
                     source if source.source_kind == "CANONICAL_PERSISTED" else None)
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
        ], "role_evidence": roles, "nomination_source": (self.nomination_source or
            RPNominationSource(self.project_id, "UNAVAILABLE_PROVISIONAL", None)).to_payload()})


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
    """Current project source authority and provisional composition evidence.

    Current project inputs cover the entire snapshot, not actual generated messages.
    Generator-only stable-link evidence is captured separately. The provisional
    per-project envelope is not evidence that a message was generated.
    Reconstruct with frozen v1 primitives, never current master data.
    """
    population: RecipientPolicyPopulation
    identities: RecipientIdentityEvidence
    message_inputs: tuple[MessageRecipientInputs, ...]
    generator: AuthenticatedGeneratorAuthority | None = None
    composition_identities: RecipientIdentityEvidence | None = None
    # The old inactive fixtures, including R1 persisted nomination evidence,
    # cannot claim complete independent presentation acquisition.
    manager_presentation: ManagerPresentationLookup | None = None
    presentation_mode: str = "UNAVAILABLE_PRE_R2B"
    current_resolution: tuple[MessageRecipientProvenance, ...] = field(init=False)
    messages: tuple[MessageRecipientProvenance, ...] = field(init=False)

    def __post_init__(self):
        if (type(self.population) is not RecipientPolicyPopulation
                or type(self.identities) is not RecipientIdentityEvidence
                or not isinstance(self.message_inputs, tuple)
                or any(type(row) is not MessageRecipientInputs for row in self.message_inputs)):
            raise ValueError("Missing recipient policy context")
        if (type(self.presentation_mode) is not str
                or self.presentation_mode not in ("UNAVAILABLE_PRE_R2B", "CANONICAL_R2B")
                or (self.presentation_mode == "CANONICAL_R2B") !=
                   (type(self.manager_presentation) is ManagerPresentationLookup)
                or (self.presentation_mode == "UNAVAILABLE_PRE_R2B" and self.manager_presentation is not None)):
            raise ValueError("Missing or contradictory manager presentation authority mode")
        if self.generator is not None and type(self.generator) is not AuthenticatedGeneratorAuthority:
            raise ValueError("Invalid generator option evidence")
        ids = [row.project_id for row in self.message_inputs]
        if ids != sorted(set(ids)):
            raise ValueError("Message coverage must be unique and ordered")
        if self.composition_identities is not None and (
                type(self.composition_identities) is not RecipientIdentityEvidence or self.generator is None):
            raise ValueError("Invalid composition-only identity evidence")
        index = self.current_index()
        composition_index = index if self.generator is None else self.index()
        roots = set(index.aliases) | index.ambiguous
        employees = {row.employee.resource_id: row.employee for row in self.population.employees}
        if any(RecipientIdentity("RESOURCE", key) not in roots for key in employees):
            raise ValueError("Missing eligible employee identity evidence")
        current_resolution = []
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
            def resolve(rows, identity_index):
                result = compose_mandatory_recipients_v1(self.population.to_population(), rows, identity_index)
                return MessageRecipientProvenance(
                    message.project_id, result.recipients,
                    tuple(MandatoryRecipientFact(row.employee.employee.resource_id, row.effective_address,
                                                 row.fulfilled_by_existing, row.blocker.reason if row.blocker else None)
                          for row in result.mandatory),
                    result.unusable_existing, result.mandatory_complete,
                )

            current = resolve(candidates, index)
            current_resolution.append(current)
            messages.append(current if self.generator is None else
                            resolve((*candidates, self.generator.candidate), composition_index))
        if self.presentation_mode == "CANONICAL_R2B":
            self._validate_manager_presentation(address_facts, manager_states, requester_states,
                                                active_contacts, resource_states, resource_activity_flags)
        object.__setattr__(self, "current_resolution", tuple(current_resolution))
        object.__setattr__(self, "messages", tuple(messages))

    def _validate_manager_presentation(self, addresses, manager_states, requester_states,
                                       active_contacts, resource_states, resource_activity_flags):
        """Corroborate independent current-source observations without adding identity roots.

        The standalone lookup is legacy presentation authority only. It may
        contain physical Resources outside the exact canonical identity graph.
        Comparing common persisted facts does not prove acquisition completeness
        or transaction-wide consistency; neither does a recomputable digest.
        """
        lookup = self.manager_presentation
        if type(lookup) is not ManagerPresentationLookup:
            raise ValueError("Invalid independent manager presentation lookup")
        if lookup.project_ids != tuple(row.project_id for row in self.message_inputs):
            raise ValueError("Manager presentation lookup differs from originating snapshot scope")
        users = {row.user_id: row for row in self.identities.users}
        contacts = {row.contact_id for row in self.identities.contacts}
        resources = {row.resource_id: row for row in self.identities.resources}
        roles = {row.binding: row for row in lookup.roles}
        index = self.current_index()
        users_by_contact = {}
        users_by_employee = {}
        for user in users.values():
            users_by_contact.setdefault(user.business_contact_id, []).append(user)
            users_by_employee.setdefault(user.employee_external_id, []).append(user)

        def corroborate(facts, key, value):
            if key in facts and facts[key] != value:
                raise ValueError("Contradictory independent manager presentation source fact")

        # Verify precisely the effective bindings from the independent
        # nomination/ERP sources. Hidden nominations retain delivery authority
        # but are not a source of additional presentation keys.
        expected = set()
        for message in self.message_inputs:
            if message.nomination_source is None or any(
                    type(row) is RecipientAuthorityInput and row.primary.role == "MANAGER"
                    for row in message.candidates):
                raise ValueError("Canonical manager presentation requires independent project sources")
            manager = next((row for row in message.candidates if type(row) is ERPManagerAuthority), None)
            if manager is not None:
                binding = (message.project_id, "MANAGER", manager.principal.employee_external_id)
                expected.add(binding)
                role = roles.get(binding)
                if role is None:
                    raise ValueError("Missing independent ERP manager presentation")
                canonical = index.canonical(manager.principal.identity)
                if (canonical.kind == "RESOURCE") != (manager.resource_address is not None):
                    raise ValueError("Missing or unproven canonical ERP Resource observation")
                manager_role = role
                if (role.linked_user_id != manager.linked_user_id
                        or role.linked_user_active != manager.linked_user_active
                        or role.contact_id != manager.contact_id
                        or role.contact_status != manager.contact_status
                        or role.contact_active != manager.contact_active
                        or role.raw_contact_email != (None if manager.contact_address is None
                                                      else manager.contact_address.raw_address)
                        or role.display_name != manager.display_name):
                    raise ValueError("Contradictory ERP manager presentation source")
                if manager.linked_user_id is None:
                    matches = len(users_by_employee.get(manager.principal.employee_external_id, ()))
                    status = "IDENTITY_CONFLICT" if matches > 1 else "UNRESOLVED_USER"
                else:
                    status = {"ABSENT": "UNRESOLVED_CONTACT", "UNRESOLVED": "INVALID_REFERENCE",
                              "RESOLVED": "RESOLVED"}[manager.contact_status]
                if role.resolution_status != status:
                    raise ValueError("Contradictory ERP manager presentation resolution")
                if (role.contact_active and (role.raw_contact_display_name or "").strip()
                        and role.resolver_display_name != role.raw_contact_display_name.strip()):
                    raise ValueError("Contradictory ERP manager presentation display name")
            for row in message.candidates:
                if type(row) is not RecipientAuthorityInput or row.primary.role != "CO_MANAGER":
                    continue
                key = row.primary.identity.id
                linked = users_by_contact.get(key, ())
                if key not in contacts or len(linked) > 1:
                    raise ValueError("Missing or contradictory independent nomination identity")
                user_id = linked[0].user_id if linked else None
                # The persisted RP adapter retains the proven Resource observation
                # even if a direct contact address wins. No presentation fact
                # may supply or repair this canonical authority.
                canonical = index.canonical(row.primary.identity)
                if (canonical.kind == "RESOURCE") != (row.resource_fallback is not None):
                    raise ValueError("Missing or unproven canonical RP Resource observation")
                if row.resource_fallback is not None and row.resource_fallback.identity != canonical:
                    raise ValueError("Contradictory canonical RP Resource observation")
                same_user = (manager is not None and manager.linked_user_id is not None
                             and manager.linked_user_id == user_id)
                same_contact = manager is not None and manager.contact_id == key
                if same_user or same_contact:
                    if (not same_user or not same_contact or manager.contact_status != "RESOLVED"
                            or row.primary.active != bool(manager.linked_user_active and manager.contact_active)):
                        raise ValueError("Contradictory independent nomination suppression evidence")
                    if row.primary.display_name != ((manager_role.raw_contact_display_name or "").strip() or key):
                        raise ValueError("Contradictory hidden RP nomination display name")
                    continue
                binding = (message.project_id, "CO_MANAGER", key)
                expected.add(binding)
                role = roles.get(binding)
                if (role is None or role.linked_user_id != user_id
                        or role.contact_status != "RESOLVED" or role.resolution_status != "RESOLVED"
                        or role.raw_contact_email != row.primary.email
                        or bool(role.contact_active and role.linked_user_active is not False) != row.primary.active
                        or role.resolver_display_name != row.primary.display_name):
                    raise ValueError("Contradictory independent RP manager presentation source")
                if role.resolver_display_name != ((role.raw_contact_display_name or "").strip() or key):
                    raise ValueError("Contradictory RP manager presentation display name")
        if set(roles) != expected:
            raise ValueError("Extra or missing independent manager presentation binding")

        for role in lookup.roles:
            if role.linked_user_id is not None:
                user = users.get(role.linked_user_id)
                if user != UserIdentityFact(role.linked_user_id, role.raw_user_employee_id,
                                            role.linked_user_contact_id):
                    raise ValueError("Contradictory manager presentation user identity")
                key = RecipientIdentity("USER", role.linked_user_id)
                corroborate(manager_states, key, role.linked_user_active)
                corroborate(requester_states, key, role.linked_user_active)
            if role.contact_id is not None:
                if (role.contact_id in contacts) != (role.contact_status == "RESOLVED"):
                    raise ValueError("Contradictory manager presentation contact identity")
                key = RecipientIdentity("CONTACT", role.contact_id)
                corroborate(manager_states, key, role.contact_active)
                if key in active_contacts and role.contact_active is not True:
                    raise ValueError("Contradictory manager presentation contact activity")
                if role.contact_status == "RESOLVED":
                    corroborate(addresses, ("BUSINESS_CONTACT_EMAIL", key, "CURRENT"),
                                role.raw_contact_email)

        # An exact canonical Resource key in the lookup's effective key set
        # must be among the results for the partition containing that key.
        # Other results may occur across partitions under database collation.
        # Do not infer equality or an alias from a trimmed identifier.
        partition_by_key = {key: i for i, keys in enumerate(lookup.partitions) for key in keys}
        results = tuple(frozenset(rows) for rows in lookup.partition_results)
        for row in resources.values():
            partition = partition_by_key.get(row.employee_external_id)
            if partition is not None and row.resource_id not in results[partition]:
                raise ValueError("Missing overlapping canonical Resource presentation observation")
        eligible_ids = {employee.employee.resource_id for employee in self.population.employees}
        for row in lookup.resources:
            other = resources.get(row.resource_id)
            if other is not None and other.employee_external_id != row.employee_external_id:
                raise ValueError("Contradictory raw Resource employee identifier")
            key = RecipientIdentity("RESOURCE", row.resource_id)
            corroborate(addresses, ("RESOURCE_EMAIL", key, "CURRENT"), row.raw_email)
            corroborate(resource_states, key, bool(row.active and row.erp_active))
            corroborate(resource_activity_flags, key, (row.active, row.erp_active))
            if row.resource_id in eligible_ids:
                if not (row.active and row.erp_active):
                    raise ValueError("Inactive presentation Resource cannot be an eligible superintendent")

    @classmethod
    def capture(cls, population, identities, message_inputs, *, generator=None, composition_identities=None,
                manager_presentation=None, presentation_mode="UNAVAILABLE_PRE_R2B"):
        return cls(population, identities, tuple(sorted(message_inputs, key=lambda row: row.project_id)),
                   generator, composition_identities, manager_presentation, presentation_mode)

    @classmethod
    def capture_canonical(cls, population, identities, message_inputs, *, manager_presentation):
        """Inactive R2b construction boundary; never synthesize presentation facts."""
        if type(manager_presentation) is not ManagerPresentationLookup:
            raise ValueError("Missing independent canonical manager presentation")
        return cls.capture(population, identities, message_inputs,
                           manager_presentation=manager_presentation, presentation_mode="CANONICAL_R2B")

    def _erp_principals(self):
        return tuple(row.principal for message in self.message_inputs
                     for row in message.candidates if type(row) is ERPManagerAuthority)

    def current_index(self):
        """Resolve current source authority without the authenticated generator."""
        return self.identities.index(erp_principals=self._erp_principals())

    def _composition_evidence(self):
        extension = self.composition_identities
        if extension is None:
            return self.identities
        if self.generator is None or self.generator.principal.local_user_id is None:
            raise ValueError("Composition-only evidence requires a proven generator user link")
        merged = RecipientIdentityEvidence.capture(
            self.identities.resources + extension.resources,
            self.identities.contacts + extension.contacts,
            self.identities.users + extension.users,
        )
        # Current evidence must already include every competing stable link,
        # including ambiguous components. An extension cannot repair or widen it.
        current = self.current_index()
        extended = merged.index(erp_principals=self._erp_principals())
        if any(extended.components.get(root) != members for root, members in current.components.items()):
            raise ValueError("Composition evidence contradicts complete current identity closure")
        generator_root = RecipientIdentity("USER", self.generator.principal.local_user_id)
        extension_roots = frozenset(extension.index().components)
        if not extension_roots or extended.components.get(generator_root) != extension_roots:
            raise ValueError("Unrelated or incomplete composition-only identity evidence")
        return merged

    def index(self):
        """Resolve provisional composition, including the original generator."""
        return self._composition_evidence().index(
            erp_principals=self._erp_principals(),
            authenticated_principals=() if self.generator is None else (self.generator.principal,),
        )

    @staticmethod
    def _messages_payload(messages, index):

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

        return [message(row) for row in messages]

    def current_state_payload(self):
        """P5 source authority: no generator, composition extension or envelope selection."""
        return _json_value({
            "population": self.population.to_payload(),
            "identities": asdict(self.identities),
            "message_inputs": [row.to_payload() for row in self.message_inputs],
            "current_resolution": self._messages_payload(self.current_resolution, self.current_index()),
            "presentation_mode": self.presentation_mode,
            "manager_presentation": (None if self.manager_presentation is None
                                     else self.manager_presentation.to_payload()),
        })

    def _composition_payload(self):
        return _json_value({
            "composition_identities": None if self.composition_identities is None else asdict(self.composition_identities),
            "generator": None if self.generator is None else self.generator.to_payload(),
            "generator_identity_fingerprint": None if self.generator is None else self.generator.identity_fingerprint,
            "messages": self._messages_payload(self.messages, self.index()),
        })

    @staticmethod
    def _composition_digest(current, composition):
        payload = ("project_recipient_provisional_composition_v1", current, composition)
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()

    @property
    def provisional_composition_fingerprint(self):
        """Integrity of provisional source composition, not a reviewed message fingerprint."""
        return self._composition_digest(self.current_state_payload(), self._composition_payload())

    def to_payload(self):
        current = self.current_state_payload()
        composition = self._composition_payload()
        return {**current, **composition,
                "provisional_composition_fingerprint": self._composition_digest(current, composition)}

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {"population", "identities", "message_inputs", "current_resolution",
                                       "messages", "generator", "generator_identity_fingerprint",
                                       "composition_identities", "manager_presentation", "presentation_mode",
                                       "provisional_composition_fingerprint"})
        context = cls(RecipientPolicyPopulation.from_payload(data["population"]),
                      RecipientIdentityEvidence.from_payload(data["identities"]),
                      _read_list(data["message_inputs"], MessageRecipientInputs.from_payload),
                      None if data["generator"] is None else AuthenticatedGeneratorAuthority.from_payload(data["generator"]),
                      None if data["composition_identities"] is None else RecipientIdentityEvidence.from_payload(
                          data["composition_identities"]),
                      None if data["manager_presentation"] is None else ManagerPresentationLookup.from_payload(
                          data["manager_presentation"]), data["presentation_mode"])
        # Compare canonical JSON rather than Python's bool/int-coercing equality.
        if json.dumps(context.to_payload(), sort_keys=True, allow_nan=False) != json.dumps(data, sort_keys=True, allow_nan=False):
            raise ValueError("Contradictory or incomplete recipient provenance claims")
        return context
