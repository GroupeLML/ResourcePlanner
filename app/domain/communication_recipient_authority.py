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


@dataclass(frozen=True, slots=True)
class ManagerPresentationResource:
    """Physical Resource fact for legacy presentation, not identity authority."""
    resource_id: str
    employee_external_id: str
    raw_email: str | None
    active: bool
    erp_active: bool

    def __post_init__(self):
        if (type(self.resource_id) is not str or not _nonblank_id(self.resource_id)
                or type(self.employee_external_id) is not str
                or not self.employee_external_id.strip()
                or (self.raw_email is not None and type(self.raw_email) is not str)
                or type(self.active) is not bool or type(self.erp_active) is not bool):
            raise ValueError("Invalid manager presentation Resource fact")


@dataclass(frozen=True, slots=True)
class ManagerPresentationEvidence:
    """Effective role and independent presentation facts, never delivery authority."""
    project_id: str
    role: str
    role_id: str
    employee_key: str | None
    resolution_status: str
    resolver_display_name: str
    linked_user_id: str | None
    linked_user_active: bool | None
    raw_user_employee_id: str | None
    linked_user_contact_id: str | None
    raw_user_display_name: str | None
    contact_id: str | None
    contact_status: str
    contact_active: bool | None
    raw_contact_email: str | None
    raw_contact_display_name: str | None

    def __post_init__(self):
        def exact(value):
            return type(value) is str and _nonblank_id(value)

        if (not exact(self.project_id) or not exact(self.role_id)
                or type(self.role) is not str or self.role not in ("MANAGER", "CO_MANAGER")
                or (self.employee_key is not None and not exact(self.employee_key))
                or type(self.resolver_display_name) is not str
                or self.resolution_status not in (
                    "RESOLVED", "UNRESOLVED_USER", "UNRESOLVED_CONTACT",
                    "INVALID_REFERENCE", "IDENTITY_CONFLICT")
                or any(value is not None and not exact(value) for value in
                       (self.linked_user_id, self.linked_user_contact_id, self.contact_id))
                or any(value is not None and type(value) is not str for value in
                       (self.raw_user_employee_id, self.raw_user_display_name,
                        self.raw_contact_email, self.raw_contact_display_name))):
            raise ValueError("Invalid manager presentation role")
        if self.linked_user_id is None:
            if any(value is not None for value in (
                    self.linked_user_active, self.raw_user_employee_id,
                    self.linked_user_contact_id, self.raw_user_display_name)):
                raise ValueError("Presentation user facts without direct user")
        elif (type(self.linked_user_active) is not bool
              or type(self.raw_user_display_name) is not str
              or self.linked_user_contact_id != self.contact_id):
            raise ValueError("Contradictory presentation user binding")
        if self.contact_status == "RESOLVED":
            if (self.contact_id is None or type(self.contact_active) is not bool
                    or type(self.raw_contact_display_name) is not str):
                raise ValueError("Missing resolved presentation contact")
        elif self.contact_status in ("ABSENT", "UNRESOLVED"):
            if ((self.contact_status == "ABSENT") != (self.contact_id is None)
                    or any(value is not None for value in
                           (self.contact_active, self.raw_contact_email, self.raw_contact_display_name))):
                raise ValueError("Contradictory presentation contact absence")
        else:
            raise ValueError("Invalid presentation contact status")
        status = self.resolution_status
        if ((status == "RESOLVED" and self.contact_status != "RESOLVED")
                or (status == "UNRESOLVED_USER" and
                    (self.role != "MANAGER" or self.linked_user_id is not None
                     or self.contact_status != "ABSENT"))
                or (status == "UNRESOLVED_CONTACT" and
                    (self.role != "MANAGER" or self.linked_user_id is None
                     or self.contact_status != "ABSENT"))
                or (status == "INVALID_REFERENCE" and self.contact_status != "UNRESOLVED")):
            raise ValueError("Contradictory presentation resolution status")
        user_key = (self.raw_user_employee_id or "").strip() or None
        if self.role == "MANAGER":
            if (self.employee_key != self.role_id
                    or (self.linked_user_id is not None and user_key != self.role_id)
                    or (self.linked_user_id is None and self.contact_id is not None)):
                raise ValueError("Wrong ERP presentation origin")
        elif self.contact_id != self.role_id or self.employee_key != user_key:
            raise ValueError("Wrong RP presentation origin or employee key")

    @property
    def binding(self):
        return self.project_id, self.role, self.role_id

    @property
    def display_name(self):
        if self.role == "MANAGER" and self.contact_id is not None:
            return (self.raw_user_display_name or "").strip() or self.resolver_display_name
        return self.resolver_display_name


@dataclass(frozen=True, slots=True)
class ManagerPresentationLookup:
    """Versioned effective snapshot lookup with physical facts and query occurrences.

    Standalone structural evidence is not proof of trusted SQL acquisition.
    Nothing here can construct a delivery candidate or extend an identity index.
    """
    project_ids: tuple[str, ...]
    roles: tuple[ManagerPresentationEvidence, ...]
    resources: tuple[ManagerPresentationResource, ...]
    partition_results: tuple[tuple[str, ...], ...]
    version: int = 1

    def __post_init__(self):
        if (type(self.version) is not int or self.version != 1
                or type(self.project_ids) is not tuple
                or any(type(key) is not str or not _nonblank_id(key) for key in self.project_ids)
                or self.project_ids != tuple(sorted(set(self.project_ids)))
                or type(self.roles) is not tuple
                or any(type(row) is not ManagerPresentationEvidence for row in self.roles)
                or type(self.resources) is not tuple
                or any(type(row) is not ManagerPresentationResource for row in self.resources)
                or type(self.partition_results) is not tuple):
            raise ValueError("Invalid manager presentation lookup scope")
        bindings = tuple(row.binding for row in self.roles)
        if (bindings != tuple(sorted(set(bindings)))
                or any(row.project_id not in self.project_ids for row in self.roles)):
            raise ValueError("Duplicate or out-of-scope manager presentation binding")
        primary_projects = [row.project_id for row in self.roles if row.role == "MANAGER"]
        if len(primary_projects) != len(set(primary_projects)):
            raise ValueError("Multiple effective ERP principals")
        users = {}
        contacts = {}
        for row in self.roles:
            if row.linked_user_id is not None:
                fact = (row.linked_user_active, row.raw_user_employee_id,
                        row.linked_user_contact_id, row.raw_user_display_name)
                if users.setdefault(row.linked_user_id, fact) != fact:
                    raise ValueError("Contradictory presentation user facts")
            if row.contact_id is not None:
                fact = (row.contact_status, row.contact_active, row.raw_contact_email,
                        row.raw_contact_display_name)
                if contacts.setdefault(row.contact_id, fact) != fact:
                    raise ValueError("Contradictory presentation contact facts")
        ids = tuple(row.resource_id for row in self.resources)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("Duplicate or unordered presentation Resource facts")
        physical = {row.resource_id: row for row in self.resources}
        keys = set(self.employee_keys)
        if len(self.partition_results) != len(self.partitions):
            raise ValueError("Incomplete presentation partition results")
        referenced = set()
        for refs in self.partition_results:
            if (type(refs) is not tuple or any(type(ref) is not str for ref in refs)
                    or refs != tuple(sorted(set(refs)))):
                raise ValueError("Duplicate or malformed presentation partition reference")
            for ref in refs:
                if ref not in physical or physical[ref].employee_external_id.strip() not in keys:
                    raise ValueError("Unknown or out-of-scope presentation Resource reference")
                referenced.add(ref)
        if referenced != set(ids):
            raise ValueError("Unreferenced presentation Resource fact")

    @property
    def employee_keys(self):
        return tuple(sorted({row.employee_key for row in self.roles if row.employee_key is not None}))

    @property
    def partitions(self):
        keys = self.employee_keys
        return tuple(keys[i:i + 900] for i in range(0, len(keys), 900))

    @classmethod
    def capture(cls, project_ids, roles, result_rows):
        """Freeze scalar acquisition rows, retaining repeated cross-partition occurrences."""
        facts = {}
        results = []
        for partition in result_rows:
            refs = []
            for row in partition:
                if type(row) is not ManagerPresentationResource:
                    raise ValueError("Invalid presentation Resource acquisition")
                if facts.setdefault(row.resource_id, row) != row:
                    raise ValueError("Contradictory physical presentation Resource facts")
                refs.append(row.resource_id)
            results.append(tuple(sorted(refs)))
        return cls(tuple(sorted(set(project_ids))), tuple(sorted(roles, key=lambda row: row.binding)),
                   tuple(facts[key] for key in sorted(facts)), tuple(results))

    def occurrences(self, employee_key):
        physical = {row.resource_id: row for row in self.resources}
        return tuple(physical[ref] for refs in self.partition_results for ref in refs
                     if physical[ref].employee_external_id.strip() == employee_key)

    def derive(self, role: ManagerPresentationEvidence):
        if type(role) is not ManagerPresentationEvidence or role not in self.roles:
            raise ValueError("Missing originating presentation role")
        rows = self.occurrences(role.employee_key)
        contact_email = (role.raw_contact_email or "").strip()
        resource_email = (rows[0].raw_email or "").strip() if len(rows) == 1 else ""
        return (contact_email or resource_email or None,
                bool(role.linked_user_active is not False and role.contact_active is not False
                     and (role.contact_active is True or (not contact_email and resource_email))))

    def to_payload(self):
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @classmethod
    def from_payload(cls, payload):
        data = _require_keys(payload, {
            "project_ids", "roles", "resources", "partition_results", "version",
        })
        if any(type(data[key]) is not list for key in
               ("project_ids", "roles", "resources", "partition_results")):
            raise ValueError("Missing explicit presentation lookup collections")
        if any(type(refs) is not list for refs in data["partition_results"]):
            raise ValueError("Malformed presentation partition results")
        role_keys = set(ManagerPresentationEvidence.__dataclass_fields__)
        resource_keys = set(ManagerPresentationResource.__dataclass_fields__)
        return cls(
            tuple(data["project_ids"]),
            tuple(ManagerPresentationEvidence(**_require_keys(row, role_keys)) for row in data["roles"]),
            tuple(ManagerPresentationResource(**_require_keys(row, resource_keys)) for row in data["resources"]),
            tuple(tuple(refs) for refs in data["partition_results"]), data["version"],
        )

    def serialize(self):
        return json.dumps(self.to_payload(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    @classmethod
    def deserialize(cls, payload):
        def read_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate presentation lookup JSON key")
                result[key] = value
            return result

        if type(payload) is not str:
            raise ValueError("Expected serialized presentation lookup")
        return cls.from_payload(json.loads(payload, object_pairs_hook=read_object))
