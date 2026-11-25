"""Inactive ADR-039 message context; neither composition nor delivery authority.

The reader requires independently supplied current and communicated snapshots.
It never fetches live identity data or selects a database baseline.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import json
import re

from .communication_recipient_authority import ERPManagerAuthority
from .communication_recipient_composition import RecipientCandidate, contact_rooted_candidate_v1
from .communication_recipient_identity import RecipientIdentity
from .communication_recipient_policy import _nonblank_id, _require_keys
from .communication_recipient_provenance import MessageRecipientInputs, RecipientPolicyContext
from .project_communication import ProjectCommunicationParticipant, ProjectCommunicationProjection
from .project_communication_messages import (
    MESSAGE_KIND_PLANNING_CHANGE, MESSAGE_KIND_WEEKLY_CONFIRMATION,
    compare_project_communication_projections, project_projection_fingerprint,
)
from .superintendent_recipient_resolution import resolve_address_v1


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json(value):
    return json.loads(_canonical(value))


@dataclass(frozen=True, slots=True)
class CommunicatedBaselineReference:
    batch_id: str
    week_start: date
    week_end: date
    snapshot_fingerprint: str
    status: str = "COMMUNICATED"
    model_version: str = "project_v2"

    def __post_init__(self):
        if (not _nonblank_id(self.batch_id) or type(self.week_start) is not date
                or type(self.week_end) is not date or self.week_end < self.week_start
                or type(self.snapshot_fingerprint) is not str
                or re.fullmatch(r"[0-9a-f]{64}", self.snapshot_fingerprint) is None
                or self.status != "COMMUNICATED" or self.model_version != "project_v2"):
            raise ValueError("Invalid communicated baseline reference")

    def to_payload(self):
        return {**asdict(self), "week_start": self.week_start.isoformat(),
                "week_end": self.week_end.isoformat()}


@dataclass(frozen=True, slots=True)
class CommunicatedBaselineEvidence:
    """Trusted selector result, including row identity, not a second baseline store."""
    reference: CommunicatedBaselineReference
    snapshot: ProjectCommunicationProjection

    def __post_init__(self):
        if (type(self.reference) is not CommunicatedBaselineReference
                or type(self.snapshot) is not ProjectCommunicationProjection
                or self.snapshot.week_start != self.reference.week_start
                or self.snapshot.week_end != self.reference.week_end
                or project_projection_fingerprint(self.snapshot) != self.reference.snapshot_fingerprint):
            raise ValueError("Contradictory communicated baseline evidence")


@dataclass(frozen=True, slots=True)
class ParticipantObservation:
    """Exactly the presentation facts captured in the project snapshot.

    A recorded contact/user is not proof of an ERP person or a cross-root alias.
    Only a captured assigned Resource ID is independently usable as Resource identity.
    """
    scope: str
    role: str
    participant: ProjectCommunicationParticipant
    recorded_identity: RecipientIdentity | None

    @property
    def proven_resource_id(self):
        if self.role == "ASSIGNED" and self.recorded_identity is not None:
            return self.recorded_identity.id
        return None


@dataclass(frozen=True, slots=True)
class TypedSourceObservation:
    """Selected source candidate, with identity resolved only in its origin graph.

    All original raw observations and relationship evidence remain in source_inputs.
    For historical rows that graph is exclusively the frozen baseline's current graph.
    """
    scope: str
    candidate: RecipientCandidate
    canonical_identity: RecipientIdentity

    @property
    def proven_resource_id(self):
        return self.canonical_identity.id if self.canonical_identity.kind == "RESOURCE" else None


@dataclass(frozen=True, slots=True)
class ProjectRecipientSource:
    project_id: str
    scope: str
    snapshot_fingerprint: str
    baseline: CommunicatedBaselineReference | None
    participants: tuple[ParticipantObservation, ...]
    source_inputs: MessageRecipientInputs | None
    typed_observations: tuple[TypedSourceObservation, ...]

    def to_payload(self):
        return _json({
            "project_id": self.project_id, "scope": self.scope,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "baseline": None if self.baseline is None else self.baseline.to_payload(),
            "participants": [asdict(row) for row in self.participants],
            "source_inputs": None if self.source_inputs is None else self.source_inputs.to_payload(),
            "typed_observations": [asdict(row) for row in self.typed_observations],
        })

    def supports_mandatory_resource(self, resource_id: str) -> bool:
        """Identity/address evidence only, NOT a claim of fulfilled delivery.

        Historical presentation is never an alias lookup into the current graph.
        For v5 use its source authority instead of presentation addresses. The final
        composition must still prove that the eligible candidate was selected.
        """
        if self.source_inputs is not None:
            return any(row.proven_resource_id == resource_id and row.candidate.active
                       and resolve_address_v1(row.candidate.email).address is not None
                       for row in self.typed_observations)
        return any(row.proven_resource_id == resource_id and row.participant.active
                   and resolve_address_v1(row.participant.email).address is not None
                   for row in self.participants)


def _participants(project, scope):
    rows = []
    known = {}

    def add(role, participant, identity):
        if type(participant) is not ProjectCommunicationParticipant:
            raise ValueError("Invalid project participant observation")
        if (type(participant.display_name) is not str or type(participant.active) is not bool
                or (participant.email is not None and type(participant.email) is not str)):
            raise ValueError("Malformed project participant observation")
        observation = ParticipantObservation(scope, role, participant, identity)
        if identity is not None:
            key = (role, identity)
            if key in known:
                if known[key] != observation:
                    raise ValueError("Contradictory scoped project participant observation")
                return
            known[key] = observation
        rows.append(observation)

    for role, participants in (("MANAGER", (project.project_manager,)),
                               ("CO_MANAGER", project.project_co_managers),
                               ("REQUESTER", project.requesters)):
        for participant in participants:
            if role == "REQUESTER" and participant.user_id:
                identity = RecipientIdentity("USER", participant.user_id)
            elif participant.contact_id:
                identity = RecipientIdentity("CONTACT", participant.contact_id)
            elif participant.user_id:
                identity = RecipientIdentity("USER", participant.user_id)
            else:
                identity = None
            add(role, participant, identity)
    for day in project.days:
        for task in day.tasks:
            for resource in task.resources:
                add("ASSIGNED", resource.contact, RecipientIdentity("RESOURCE", resource.resource_id))
    return tuple(rows)


def _source(project, projection, fingerprint, baseline=None):
    scope = "CURRENT" if baseline is None else "HISTORICAL"
    inputs = None
    observations = []
    context = projection.recipient_policy_context
    if context is not None:
        inputs = next(row for row in context.message_inputs if row.project_id == project.project_id)
        index = context.current_index()
        for row in inputs.candidates:
            if type(row) is ERPManagerAuthority:
                candidate = row.candidate(context.identities, index)
            elif row.primary.identity.kind == "CONTACT":
                candidate = contact_rooted_candidate_v1(row.primary, row.resource_fallback, index)
            else:
                candidate = row.primary
            observations.append(TypedSourceObservation(scope, candidate, index.canonical(candidate.identity)))
    return ProjectRecipientSource(project.project_id, scope, fingerprint, baseline,
                                  _participants(project, scope), inputs, tuple(observations))


@dataclass(frozen=True, slots=True)
class ProjectMessageContext:
    message_key: str
    project_id: str
    mode: str
    change_kind: str | None
    included: bool
    before: ProjectRecipientSource | None
    after: ProjectRecipientSource | None

    def to_payload(self):
        return {"message_key": self.message_key, "project_id": self.project_id,
                "mode": self.mode, "change_kind": self.change_kind, "included": self.included,
                "before": None if self.before is None else self.before.to_payload(),
                "after": None if self.after is None else self.after.to_payload()}


@dataclass(frozen=True, slots=True)
class MessageContextEvidence:
    """Eligible and explicitly included message contexts, not composed artifacts.

    Keep current snapshot and baseline as independent input authorities. Recompute
    every serialized claim from those inputs. No operational generation is called.
    """
    mode: str
    current: ProjectCommunicationProjection
    baseline: CommunicatedBaselineEvidence | None
    excluded_message_keys: tuple[str, ...] = ()

    def __post_init__(self):
        if (type(self.current) is not ProjectCommunicationProjection
                or self.current.request_provenance_version != 5
                or type(self.current.recipient_policy_context) is not RecipientPolicyContext
                or self.mode not in (MESSAGE_KIND_WEEKLY_CONFIRMATION, MESSAGE_KIND_PLANNING_CHANGE)):
            raise ValueError("Expected current v5 and supported message mode")
        project_projection_fingerprint(self.current)
        if self.mode == MESSAGE_KIND_PLANNING_CHANGE:
            if type(self.baseline) is not CommunicatedBaselineEvidence:
                raise ValueError("Delta context requires communicated baseline evidence")
            self.baseline.__post_init__()
            if (self.baseline.reference.week_start != self.current.week_start
                    or self.baseline.reference.week_end != self.current.week_end):
                raise ValueError("Baseline week differs from current snapshot")
            ids = [row.project_id for row in self.baseline.snapshot.projects]
            if any(not _nonblank_id(key) for key in ids) or len(ids) != len(set(ids)):
                raise ValueError("Contradictory baseline project coverage")
        elif self.baseline is not None:
            raise ValueError("Full message context has no applicable delta reference")
        keys = self.excluded_message_keys
        if (type(keys) is not tuple or any(not _nonblank_id(key) for key in keys)
                or tuple(sorted(set(keys))) != keys):
            raise ValueError("Explicit exclusions must be unique and canonical")
        if not set(keys) <= {row.message_key for row in self.messages}:
            raise ValueError("Exclusion outside eligible message coverage")

    @property
    def current_policy(self):
        return self.current.recipient_policy_context

    @property
    def messages(self):
        fingerprint = project_projection_fingerprint(self.current)
        if self.mode == MESSAGE_KIND_WEEKLY_CONFIRMATION:
            changes = ((p.project_id, None, None, p) for p in sorted(self.current.projects, key=lambda p: p.project_id))
        else:
            changes = ((c.project_id, c.kind, c.before, c.after) for c in
                       compare_project_communication_projections(self.baseline.snapshot, self.current))
        result = []
        for project_id, kind, before, after in changes:
            key = f"project:{project_id}"
            reference = None if self.baseline is None else self.baseline.reference
            result.append(ProjectMessageContext(
                key, project_id, self.mode, kind, key not in self.excluded_message_keys,
                None if before is None else _source(before, self.baseline.snapshot, reference.snapshot_fingerprint, reference),
                None if after is None else _source(after, self.current, fingerprint),
            ))
        return tuple(result)

    @property
    def included_messages(self):
        return tuple(row for row in self.messages if row.included)

    def to_payload(self):
        policy = self.current_policy
        return _json({
            "mode": self.mode,
            "current_snapshot_fingerprint": project_projection_fingerprint(self.current),
            "baseline": None if self.baseline is None else self.baseline.reference.to_payload(),
            "excluded_message_keys": self.excluded_message_keys,
            "messages": [row.to_payload() for row in self.messages],
            "current_population": policy.population.to_payload(),
            "current_generator": None if policy.generator is None else policy.generator.to_payload(),
            "current_provisional_composition_fingerprint": policy.provisional_composition_fingerprint,
        })

    @classmethod
    def from_payload(cls, payload, *, current, baseline=None):
        data = _require_keys(payload, {"mode", "current_snapshot_fingerprint", "baseline",
                                       "excluded_message_keys", "messages", "current_population",
                                       "current_generator", "current_provisional_composition_fingerprint"})
        if type(data["excluded_message_keys"]) is not list:
            raise ValueError("Missing explicit message exclusions")
        evidence = cls(data["mode"], current, baseline, tuple(data["excluded_message_keys"]))
        if _canonical(evidence.to_payload()) != _canonical(data):
            raise ValueError("Contradictory or incomplete message context evidence")
        return evidence

    def serialize(self) -> str:
        return _canonical(self.to_payload())

    @classmethod
    def deserialize(cls, payload: str, *, current, baseline=None):
        def read_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate message context evidence key")
                result[key] = value
            return result

        if type(payload) is not str:
            raise ValueError("Expected serialized message context evidence")
        return cls.from_payload(json.loads(payload, object_pairs_hook=read_object),
                                current=current, baseline=baseline)
