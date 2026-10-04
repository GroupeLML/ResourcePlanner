from __future__ import annotations

from dataclasses import dataclass


STATUS_RESOLVED = "RESOLVED"
STATUS_UNRESOLVED = "UNRESOLVED"
STATUS_INVALID_REFERENCE = "INVALID_REFERENCE"
STATUS_INACTIVE = "INACTIVE"

RESPONSIBILITY_CONTEXT_VERSION = 1
PROVENANCE_APPROVAL_CAPTURE = "APPROVAL_CAPTURE"
PROVENANCE_OPERATIONAL_CAPTURE = "OPERATIONAL_CAPTURE"
PROVENANCE_MIGRATION_OBSERVED = "MIGRATION_OBSERVED"
PROVENANCE_LEGACY_UNKNOWN = "LEGACY_UNKNOWN"

SOURCE_SHIFT_OVERRIDE = "SHIFT_OVERRIDE"
SOURCE_REQUIREMENT_OVERRIDE = "RESOURCE_REQUIREMENT_OVERRIDE"
SOURCE_REQUEST_OVERRIDE = "REQUEST_OVERRIDE"
SOURCE_TASK_RESPONSIBLE = "TASK_RESPONSIBLE"
SOURCE_PROJECT_OVERRIDE = "PROJECT_OVERRIDE"
SOURCE_PROJECT_MANAGER = "PROJECT_MANAGER"
SOURCE_RESOURCE_COORDINATOR = "RESOURCE_COORDINATOR"
SOURCE_TASK_COORDINATOR = "TASK_COORDINATOR"
SOURCE_NONE = "NONE"

DIAGNOSTIC_CONTACT_REFERENCE_INVALID = "CONTACT_REFERENCE_INVALID"
DIAGNOSTIC_CONTACT_INACTIVE = "CONTACT_INACTIVE"
DIAGNOSTIC_CONTACT_EMAIL_MISSING = "CONTACT_EMAIL_MISSING"
DIAGNOSTIC_CONTACT_PHONE_MISSING = "CONTACT_PHONE_MISSING"
DIAGNOSTIC_CONTACT_UNRESOLVED = "CONTACT_UNRESOLVED"
DIAGNOSTIC_TASK_REFERENCE_INVALID = "TASK_REFERENCE_INVALID"
DIAGNOSTIC_TASK_REFERENCE_LEGACY_CODE = "TASK_REFERENCE_LEGACY_CODE"
DIAGNOSTIC_TASK_REFERENCE_UNRESOLVED = "TASK_REFERENCE_UNRESOLVED"
DIAGNOSTIC_TASK_PROJECT_MISMATCH = "TASK_PROJECT_MISMATCH"
DIAGNOSTIC_TASK_INACTIVE = "TASK_INACTIVE"
DIAGNOSTIC_RESOURCE_REFERENCE_INVALID = "RESOURCE_REFERENCE_INVALID"
DIAGNOSTIC_RESOURCE_INACTIVE = "RESOURCE_INACTIVE"
DIAGNOSTIC_PROJECT_MANAGER_CONTACT_UNMIGRATED = "PROJECT_MANAGER_CONTACT_UNMIGRATED"
DIAGNOSTIC_APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN = (
    "APPROVED_CONTACT_CONTEXT_LEGACY_UNKNOWN"
)
DIAGNOSTIC_APPROVED_TASK_REFERENCE_INVALID = "APPROVED_TASK_REFERENCE_INVALID"
DIAGNOSTIC_APPROVED_TASK_PROJECT_MISMATCH = "APPROVED_TASK_PROJECT_MISMATCH"
DIAGNOSTIC_SHIFT_RESOURCE_DIFFERS_FROM_REQUIREMENT = (
    "SHIFT_RESOURCE_DIFFERS_FROM_REQUIREMENT"
)


@dataclass(frozen=True, slots=True)
class BusinessContactSnapshot:
    contact_id: str
    display_name: str
    email: str | None = None
    phone: str | None = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class ContactCandidate:
    """One configured level in a contact-resolution hierarchy.

    contact_id is the persisted reference. When it is None, that hierarchy
    level is absent and the resolver may continue to the next candidate. When an ID
    exists but contact is missing, the reference is invalid and fallback must stop.
    """

    source_type: str
    source_entity_id: str | None
    source_label: str | None
    contact_id: str | None = None
    contact: BusinessContactSnapshot | None = None
    diagnostics: tuple[str, ...] = ()
    external_id: str | None = None
    display_name_hint: str | None = None
    captured_status: str | None = None


@dataclass(frozen=True, slots=True)
class ContactResolution:
    status: str
    contact_id: str | None
    display_name: str | None
    email: str | None
    phone: str | None
    source_type: str
    source_entity_id: str | None = None
    source_label: str | None = None
    diagnostics: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.status == STATUS_RESOLVED


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _candidate_resolution(candidate: ContactCandidate) -> ContactResolution:
    captured_status = candidate.captured_status
    if captured_status is not None and captured_status not in {
        STATUS_RESOLVED,
        STATUS_UNRESOLVED,
        STATUS_INVALID_REFERENCE,
        STATUS_INACTIVE,
    }:
        raise ValueError(f"Unsupported captured contact status: {captured_status}")

    if candidate.contact_id is None:
        if captured_status is not None:
            return ContactResolution(
                status=captured_status,
                contact_id=None,
                display_name=candidate.display_name_hint,
                email=None,
                phone=None,
                source_type=candidate.source_type,
                source_entity_id=candidate.source_entity_id,
                source_label=candidate.source_label,
                diagnostics=_unique(candidate.diagnostics),
            )
        if candidate.external_id is None:
            raise ValueError("An absent candidate cannot be materialized as a resolution.")
        return ContactResolution(
            status=STATUS_UNRESOLVED,
            contact_id=None,
            display_name=candidate.display_name_hint or candidate.external_id,
            email=None,
            phone=None,
            source_type=candidate.source_type,
            source_entity_id=candidate.source_entity_id,
            source_label=candidate.source_label,
            diagnostics=_unique(
                candidate.diagnostics + (DIAGNOSTIC_CONTACT_UNRESOLVED,)
            ),
        )

    contact = candidate.contact
    if contact is None:
        return ContactResolution(
            status=STATUS_INVALID_REFERENCE,
            contact_id=candidate.contact_id,
            display_name=None,
            email=None,
            phone=None,
            source_type=candidate.source_type,
            source_entity_id=candidate.source_entity_id,
            source_label=candidate.source_label,
            diagnostics=_unique(
                candidate.diagnostics + (DIAGNOSTIC_CONTACT_REFERENCE_INVALID,)
            ),
        )

    diagnostics = candidate.diagnostics
    if captured_status is not None and captured_status != STATUS_RESOLVED:
        status = captured_status
    elif not contact.active:
        diagnostics += (DIAGNOSTIC_CONTACT_INACTIVE,)
        status = STATUS_INACTIVE
    else:
        status = STATUS_RESOLVED
        if not (contact.email or "").strip():
            diagnostics += (DIAGNOSTIC_CONTACT_EMAIL_MISSING,)
        if not (contact.phone or "").strip():
            diagnostics += (DIAGNOSTIC_CONTACT_PHONE_MISSING,)

    return ContactResolution(
        status=status,
        contact_id=contact.contact_id,
        display_name=contact.display_name,
        email=contact.email,
        phone=contact.phone,
        source_type=candidate.source_type,
        source_entity_id=candidate.source_entity_id,
        source_label=candidate.source_label,
        diagnostics=_unique(diagnostics),
    )


def resolve_contact_candidates(
    *candidates: ContactCandidate,
    unresolved_diagnostics: tuple[str, ...] = (),
) -> ContactResolution:
    """Resolve first configured candidate without hiding broken explicit references."""

    for candidate in candidates:
        if candidate.contact_id is None and candidate.external_id is None:
            continue
        return _candidate_resolution(candidate)

    return ContactResolution(
        status=STATUS_UNRESOLVED,
        contact_id=None,
        display_name=None,
        email=None,
        phone=None,
        source_type=SOURCE_NONE,
        diagnostics=_unique(
            unresolved_diagnostics + (DIAGNOSTIC_CONTACT_UNRESOLVED,)
        ),
    )


def resolve_operational_responsible(
    *,
    shift_override: ContactCandidate | None = None,
    requirement_override: ContactCandidate | None = None,
    request_override: ContactCandidate | None = None,
    task_responsible: ContactCandidate | None = None,
    project_override: ContactCandidate | None = None,
    project_manager: ContactCandidate | None = None,
    captured_inherited: ContactCandidate | None = None,
) -> ContactResolution:
    """Resolve the single canonical operational-responsibility hierarchy.

    Materialized planning supplies captured_inherited instead of rereading the
    lower request/task/project/ERP levels. Shift and requirement overrides remain
    live explicit decisions layered above that immutable inherited context.
    """

    dynamic_inherited = (
        request_override,
        task_responsible,
        project_override,
        project_manager,
    )
    if captured_inherited is not None and any(
        candidate is not None for candidate in dynamic_inherited
    ):
        raise ValueError(
            "Captured operational responsibility cannot be mixed with live inherited candidates."
        )

    candidates = (
        shift_override,
        requirement_override,
        captured_inherited,
    ) if captured_inherited is not None else (
        shift_override,
        requirement_override,
        request_override,
        task_responsible,
        project_override,
        project_manager,
    )
    return resolve_contact_candidates(
        *(candidate for candidate in candidates if candidate is not None)
    )


def resolve_coordinator(
    *,
    resource_coordinator: ContactCandidate,
    task_coordinator: ContactCandidate,
) -> ContactResolution:
    """Resolve resource coordinator > task coordinator."""

    return resolve_contact_candidates(
        resource_coordinator,
        task_coordinator,
    )
