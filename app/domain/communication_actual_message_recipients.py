"""Inactive actual-message recipient selection for ADR-039 (scoped v1).

This is deliberately separate from frozen snapshot/provisional composition v1.
It derives an envelope from validated message context, never from caller-provided
recipient lists, and does not create a draft, approve or persist anything.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json

from .communication_message_context import MessageContextEvidence, _canonical, _json
from .communication_recipient_composition import MandatoryResolution
from .communication_recipient_identity import RecipientIdentity
from .communication_recipient_policy import _require_keys
from .superintendent_recipient_resolution import AddressResolution, resolve_address_v1


@dataclass(frozen=True, slots=True)
class ScopedRecipientObservation:
    scope: str
    source_kind: str
    source_position: int
    identity: RecipientIdentity | None
    canonical_identity: RecipientIdentity | None
    display_name: str
    role: str
    requested_channel: str
    raw_address: str | None
    address_source: str
    active: bool
    superseded: bool = False

    @property
    def resolution(self) -> AddressResolution:
        return resolve_address_v1(self.raw_address)

    @property
    def selectable(self):
        return self.active and not self.superseded and self.resolution.address is not None

    def to_payload(self):
        return {**asdict(self), "resolution": asdict(self.resolution)}


@dataclass(frozen=True, slots=True)
class IdentityDelivery:
    """A valid selection for one origin-proven identity (or one unknown row)."""
    canonical_identity: RecipientIdentity | None
    selected_observation: int
    contributing_observations: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ActualEnvelopeRecipient:
    channel: str
    address: str
    selected_observation: int
    identity_deliveries: tuple[IdentityDelivery, ...]


@dataclass(frozen=True, slots=True)
class ActualMandatoryDelivery:
    resolution: MandatoryResolution
    selected_observation: int | None

    def to_payload(self):
        blocker = self.resolution.blocker
        return {**asdict(self), "blocker": None if blocker is None else
                {**asdict(blocker), "message": blocker.message}}


@dataclass(frozen=True, slots=True)
class ExistingRecipientDiagnostic:
    observation: int
    severity: str
    reason: str


@dataclass(frozen=True, slots=True)
class ActualMessageRecipients:
    message_key: str
    project_id: str
    mode: str
    observations: tuple[ScopedRecipientObservation, ...]
    recipients: tuple[ActualEnvelopeRecipient, ...]
    mandatory: tuple[ActualMandatoryDelivery, ...]
    existing_diagnostics: tuple[ExistingRecipientDiagnostic, ...]
    binding_diagnostic: str | None
    population_complete: bool

    @property
    def to_addresses(self):
        return tuple(row.address for row in self.recipients if row.channel == "TO")

    @property
    def cc_addresses(self):
        return tuple(row.address for row in self.recipients if row.channel == "CC")

    @property
    def mandatory_complete(self):
        return self.population_complete and all(row.resolution.effective_address is not None for row in self.mandatory)

    @property
    def blockers(self):
        return tuple(row.resolution.blocker for row in self.mandatory if row.resolution.blocker is not None)

    def to_payload(self):
        return _json({
            "message_key": self.message_key, "project_id": self.project_id, "mode": self.mode,
            "observations": [row.to_payload() for row in self.observations],
            "recipients": [asdict(row) for row in self.recipients],
            "mandatory": [row.to_payload() for row in self.mandatory],
            "existing_diagnostics": [asdict(row) for row in self.existing_diagnostics],
            "binding_diagnostic": self.binding_diagnostic,
            "population_complete": self.population_complete,
            "mandatory_complete": self.mandatory_complete,
        })


def _candidate_observation(candidate, canonical, scope, source_kind, position, *, superseded=False):
    return ScopedRecipientObservation(
        scope, source_kind, position, candidate.identity, canonical, candidate.display_name,
        candidate.role, candidate.channel, candidate.email, candidate.address_source,
        candidate.active, superseded,
    )


def _source_observations(source):
    if source is None:
        return ()
    if source.source_inputs is not None:
        rows = [_candidate_observation(row.candidate, row.canonical_identity, source.scope, "TYPED", i)
                for i, row in enumerate(source.typed_observations)]
        # A completely unresolved participant can lack a typed source. Keep its
        # presentation evidence for diagnostics, never promote it to authority.
        represented_roles = {row.role for row in rows}
        for i, row in enumerate(source.participants):
            if (row.recorded_identity is None
                    and not (row.role == "MANAGER" and "MANAGER" in represented_roles)):
                participant = row.participant
                rows.append(ScopedRecipientObservation(
                    source.scope, "UNRESOLVED_PRESENTATION", i, None, None,
                    participant.display_name, row.role, "CC" if row.role == "ASSIGNED" else "TO",
                    participant.email, "NO_ADDRESS_AUTHORITY", False,
                ))
        return tuple(rows)
    # P0-P4 only establish the captured participant address and recorded root.
    # In particular a legacy contact/user is NOT an ERP or Resource alias.
    return tuple(ScopedRecipientObservation(
        source.scope, "PRESENTATION", i, row.recorded_identity, row.recorded_identity,
        row.participant.display_name, row.role, "CC" if row.role == "ASSIGNED" else "TO",
        row.participant.email, "HISTORICAL_CAPTURED_ADDRESS", row.participant.active,
    ) for i, row in enumerate(source.participants))


def _existing_observations(message, policy):
    after = _source_observations(message.after)
    before = _source_observations(message.before)
    # ADR-037: the complete current project To context precedes the old one.
    # For assigned Resources present in both, the latest captured Resource
    # contact wins even if inactive or unusable. Retain the old row for audit.
    current_assigned = {row.identity for row in after if row.role == "ASSIGNED"}
    old = tuple(replace(row, superseded=True)
                if row.role == "ASSIGNED" and row.identity in current_assigned else row for row in before)
    ordered = after + old
    to = tuple(row for row in ordered if row.requested_channel == "TO")
    cc = tuple(sorted((row for row in ordered if row.requested_channel == "CC"), key=lambda row: (
        row.display_name.casefold(), (row.raw_address or "").strip().casefold(),
        "" if row.identity is None else row.identity.id, row.scope != "CURRENT", row.source_position,
    )))
    if policy.generator is not None:
        generator = policy.generator.candidate
        cc += (_candidate_observation(generator, policy.index().canonical(generator.identity),
                                      "AUTHENTICATED", "GENERATOR", 0),)
    return to + cc


def _compose_message(message, policy):
    if not message.included:
        raise ValueError("Actual recipient composition requires an included message")
    observations = list(_existing_observations(message, policy))
    # Do not union origin graphs. Canonical keys are resolved independently.
    # Exact equal canonical keys share a delivery. A historical observation
    # with only a recorded root may also contribute to an already selected
    # current observation of that exact record; this does not establish any
    # historical alias or change the selected observation's proof.
    groups = {}
    current_selected_roots = {}
    diagnostics = []
    for i, row in enumerate(observations):
        if row.superseded:
            continue
        severity = "BLOCKING" if row.requested_channel == "TO" else "WARNING"
        if not row.active:
            diagnostics.append(ExistingRecipientDiagnostic(i, severity, "INACTIVE"))
        if row.resolution.failure is not None:
            diagnostics.append(ExistingRecipientDiagnostic(i, severity, row.resolution.failure.value))
        if not row.selectable:
            continue
        key = row.canonical_identity if row.canonical_identity is not None else ("UNKNOWN", i)
        if (row.scope == "HISTORICAL" and row.identity is not None
                and row.canonical_identity == row.identity and row.identity in current_selected_roots
                and key not in groups):
            # Match the same captured record, not a historical alias from the
            # current graph. Only the already selected current delivery proves
            # the canonical identity. Different frozen canonical roots are not
            # joined via a common raw root.
            key = current_selected_roots[row.identity]
        group = groups.setdefault(key, [])
        if not group and row.scope == "CURRENT" and row.identity is not None:
            current_selected_roots[row.identity] = key
        group.append(i)

    # Keep superseded assigned observations as provenance of the selected
    # identity, if it exists, without allowing them to select an address.
    for i, row in enumerate(observations):
        if row.superseded and row.canonical_identity in groups:
            groups[row.canonical_identity].append(i)

    existing_keys = frozenset(groups)
    mandatory_pending = []
    for position, employee in enumerate(policy.population.employees):
        identity = RecipientIdentity("RESOURCE", employee.employee.resource_id)
        i = len(observations)
        observations.append(ScopedRecipientObservation(
            "CURRENT_POLICY", "POPULATION", position, identity, identity,
            employee.employee.display_name, "SUPERINTENDENT", "CC", employee.employee.email,
            "RESOURCE_EMAIL", True,
        ))
        # An existing identity group must already have a valid selected
        # observation with this exact independently proven Resource key.
        if identity in existing_keys:
            groups[identity].append(i)
        elif employee.resolution.address is not None:
            groups[identity] = [i]
        mandatory_pending.append((employee, identity))

    # Identity selection precedes transport-address deduplication. Keep each
    # identity's independently selected observation even when the address is
    # shared by different identities. Only valid selected observations prove
    # delivery; a suppressed or invalid contribution cannot do so.
    by_address = {}
    selected_by_identity = {}
    for key, indices in groups.items():
        selected = indices[0]
        row = observations[selected]
        address = row.resolution.address
        if address is None or not row.selectable:
            raise ValueError("Invalid selected recipient observation")
        delivery = IdentityDelivery(row.canonical_identity, selected, tuple(indices))
        normalized = address.casefold()
        by_address.setdefault(normalized, []).append(delivery)
        selected_by_identity[key] = (normalized, selected)

    recipients = []
    actual_addresses = {}
    for normalized, deliveries in by_address.items():
        first = deliveries[0].selected_observation
        row = observations[first]
        address = row.resolution.address
        recipients.append(ActualEnvelopeRecipient(row.requested_channel, address, first, tuple(deliveries)))
        actual_addresses[normalized] = address

    mandatory = []
    for employee, identity in mandatory_pending:
        selected = selected_by_identity.get(identity)
        if selected is None:
            mandatory.append(ActualMandatoryDelivery(MandatoryResolution(employee, None, False), None))
        else:
            normalized, index = selected
            mandatory.append(ActualMandatoryDelivery(
                MandatoryResolution(employee, actual_addresses[normalized], identity in existing_keys), index,
            ))
    return ActualMessageRecipients(
        message.message_key, message.project_id, message.mode, tuple(observations), tuple(recipients),
        tuple(mandatory), tuple(diagnostics), policy.population.binding_diagnostic,
        policy.population.population_complete,
    )


@dataclass(frozen=True, slots=True)
class ActualMessageComposition:
    """Structural recipient authority only; no reviewed content or final digest."""
    context: MessageContextEvidence
    messages: tuple[ActualMessageRecipients, ...] = field(init=False)

    def __post_init__(self):
        if type(self.context) is not MessageContextEvidence:
            raise ValueError("Actual composition requires validated message context")
        self.context.__post_init__()
        rows = tuple(_compose_message(row, self.context.current_policy) for row in self.context.included_messages)
        object.__setattr__(self, "messages", rows)

    @property
    def eligible_message_keys(self):
        return tuple(row.message_key for row in self.context.messages)

    @property
    def included_message_keys(self):
        return tuple(row.message_key for row in self.messages)

    def to_payload(self):
        return _json({
            "representation": "actual_message_recipients_scoped_v1",
            "context": self.context.to_payload(),
            "eligible_message_keys": self.eligible_message_keys,
            "included_message_keys": self.included_message_keys,
            "messages": [row.to_payload() for row in self.messages],
        })

    def serialize(self):
        return _canonical(self.to_payload())

    @classmethod
    def from_payload(cls, payload, *, context):
        data = _require_keys(payload, {"representation", "context", "eligible_message_keys",
                                       "included_message_keys", "messages"})
        result = cls(context)
        if _canonical(result.to_payload()) != _canonical(data):
            raise ValueError("Contradictory or incomplete actual recipient composition")
        return result

    @classmethod
    def deserialize(cls, payload, *, context):
        def read_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate actual recipient composition key")
                result[key] = value
            return result

        if type(payload) is not str:
            raise ValueError("Expected serialized actual recipient composition")
        return cls.from_payload(json.loads(payload, object_pairs_hook=read_object), context=context)
