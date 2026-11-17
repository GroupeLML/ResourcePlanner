from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
from html import escape
import json
import math
import re
from typing import Sequence

from .operational_contacts import ContactResolution, STATUS_RESOLVED
from .project_communication import (
    ProjectCommunicationAsset,
    ProjectCommunicationDay,
    ProjectCommunicationParticipant,
    ProjectCommunicationProject,
    ProjectCommunicationProjection,
    ProjectCommunicationResource,
    ProjectCommunicationRequestAssignment,
    ProjectCommunicationResponsibilityAssignment,
    ProjectCommunicationTask,
    employee_assigned_assets,
)


MESSAGE_KIND_WEEKLY_CONFIRMATION = "project_confirmation"
MESSAGE_KIND_PLANNING_CHANGE = "project_planning_change"

DIAGNOSTIC_TO_EMAIL_MISSING = "PROJECT_TO_EMAIL_MISSING"
DIAGNOSTIC_TO_INACTIVE = "PROJECT_TO_INACTIVE"
DIAGNOSTIC_CO_MANAGER_TO_EMAIL_MISSING = "PROJECT_CO_MANAGER_TO_EMAIL_MISSING"
DIAGNOSTIC_CO_MANAGER_TO_INACTIVE = "PROJECT_CO_MANAGER_TO_INACTIVE"
DIAGNOSTIC_REQUESTER_TO_EMAIL_MISSING = "PROJECT_REQUESTER_TO_EMAIL_MISSING"
DIAGNOSTIC_REQUESTER_TO_INACTIVE = "PROJECT_REQUESTER_TO_INACTIVE"
DIAGNOSTIC_CC_EMAIL_MISSING = "PROJECT_CC_EMAIL_MISSING"
DIAGNOSTIC_CC_INACTIVE = "PROJECT_CC_INACTIVE"
DIAGNOSTIC_SOURCE = "PROJECT_SOURCE_DIAGNOSTIC"
ASSET_QUALIFICATION_DIAGNOSTIC_PREFIX = "ASSET_QUALIFICATION_"

SEVERITY_BLOCKING = "BLOCKING"
SEVERITY_WARNING = "WARNING"

_WEEKDAYS_FR = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
)
_MONTHS_FR = (
    "",
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


@dataclass(frozen=True, slots=True)
class ProjectMessageDiagnostic:
    code: str
    severity: str
    entity_type: str
    entity_id: str
    message: str


@dataclass(frozen=True, slots=True)
class ProjectCommunicationDraft:
    message_key: str
    audience: str
    project_id: str
    project_number: str
    to_recipient: ProjectCommunicationParticipant
    to_recipients: tuple[ProjectCommunicationParticipant, ...]
    cc_recipients: tuple[ProjectCommunicationParticipant, ...]
    subject: str
    body: str
    body_html: str | None
    message_kind: str
    week_start: date
    content_fingerprint: str
    approvable: bool
    diagnostics: tuple[ProjectMessageDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class ProjectMessageLine:
    kind: str
    text: str = ""


@dataclass(frozen=True, slots=True)
class ProjectMessageSemantic:
    lines: tuple[ProjectMessageLine, ...]


@dataclass(frozen=True, slots=True)
class ProjectCommunicationMessageBatch:
    drafts: tuple[ProjectCommunicationDraft, ...]
    snapshot_fingerprint: str
    diagnostics: tuple[ProjectMessageDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class ProjectCommunicationChange:
    project_id: str
    kind: str  # added | removed | modified
    before: ProjectCommunicationProject | None
    after: ProjectCommunicationProject | None


def french_long_date(value: date) -> str:
    return (
        f"{_WEEKDAYS_FR[value.weekday()].capitalize()} le "
        f"{value.day} {_MONTHS_FR[value.month]} {value.year}"
    )


def _participant_payload(value: ProjectCommunicationParticipant) -> tuple[object, ...]:
    return (
        value.contact_id,
        value.user_id,
        value.display_name,
        (value.email or "").strip().casefold(),
        value.phone,
        bool(value.active),
        tuple(value.diagnostics),
    )


def _responsible_payload(value: ContactResolution) -> tuple[object, ...]:
    return (
        value.status,
        value.contact_id,
        value.display_name,
        (value.email or "").strip().casefold(),
        value.phone,
        value.source_type,
        value.source_entity_id,
        value.source_label,
        tuple(value.diagnostics),
    )


def _asset_payload(value: ProjectCommunicationAsset) -> tuple[object, ...]:
    return (
        value.allocation_id,
        value.asset_id,
        value.asset_code,
        value.asset_label,
        value.asset_type_label,
        value.asset_category,
        value.association_kind,
    )


def _project_payload(project: ProjectCommunicationProject) -> tuple[object, ...]:
    days: list[object] = []
    for day in project.days:
        tasks: list[object] = []
        for task in day.tasks:
            resources = tuple(
                (
                    resource.resource_id,
                    resource.resource_name,
                    _participant_payload(resource.contact),
                    round(float(resource.hours), 2),
                    tuple(resource.allocation_types),
                    tuple(resource.confirmations),
                    bool(resource.outside_schedule),
                    *(
                        (("assets", tuple(_asset_payload(asset) for asset in resource.assets)),)
                        if resource.assets
                        else ()
                    ),
                    tuple(resource.diagnostics),
                )
                for resource in task.resources
            )
            responsibility_assignments = tuple(
                sorted(
                    (
                        (
                            assignment.resource_id,
                            assignment.resource_name,
                            _responsible_payload(
                                assignment.operational_responsible
                            ),
                        )
                        for assignment in task.responsibility_assignments
                    ),
                    key=repr,
                )
            )
            tasks.append(
                (
                    task.task_description,
                    tuple(task.task_ids),
                    tuple(task.task_codes),
                    tuple(
                        _responsible_payload(responsible)
                        for responsible in task.operational_responsibles
                    ),
                    responsibility_assignments,
                    resources,
                    tuple(task.diagnostics),
                )
            )
        days.append((day.day.isoformat(), tuple(tasks)))
    return (
        project.project_id,
        project.project_number,
        project.project_name,
        _participant_payload(project.project_manager),
        *((("project_manager_erp_phone", project.project_manager_erp_phone),)
          if project.project_manager_erp_phone else ()),
        tuple(
            _participant_payload(manager)
            for manager in project.project_co_managers
        ),
        *(
            (
                (
                    "requesters",
                    tuple(
                        _participant_payload(requester)
                        for requester in project.requesters
                    ),
                ),
            )
            if project.requesters
            else ()
        ),
        tuple(days),
        tuple(project.diagnostics),
    )



def _validate_provenance(projection: ProjectCommunicationProjection) -> None:
    version = projection.request_provenance_version
    if version not in (1, 2) or not projection.request_provenance_complete:
        raise ValueError("Version ou couverture de provenance chantier invalide")

    seen: set[str] = set()
    for project in projection.projects:
        for day in project.days:
            for task in day.tasks:
                expected = {
                    shift_id
                    for resource in task.resources
                    for shift_id in resource.shift_ids
                }
                actual: set[str] = set()
                hours_by_resource: dict[str, float] = {}
                for assignment in task.request_assignments:
                    if not assignment.shift_id or assignment.shift_id in seen:
                        raise ValueError("Shift de provenance manquant ou dupliqué")
                    if (
                        not assignment.requirement_id
                        or not assignment.resource_id
                        or not math.isfinite(float(assignment.hours))
                        or assignment.hours <= 0
                    ):
                        raise ValueError("Attribution chantier invalide")
                    if not assignment.workforce_request_id and (
                        assignment.request_number
                        or assignment.site_contact_name
                        or assignment.site_contact_phone
                    ):
                        raise ValueError("Contact chantier sans provenance demande")
                    if not any(
                        resource.resource_id == assignment.resource_id
                        and assignment.shift_id in resource.shift_ids
                        for resource in task.resources
                    ):
                        raise ValueError("Ressource de provenance incohérente")
                    if version == 2:
                        standard_hours = assignment.standard_hours
                        if (
                            standard_hours is None
                            or not math.isfinite(float(standard_hours))
                            or float(standard_hours) < 0
                        ):
                            raise ValueError(
                                "Heures standard v2 manquantes ou invalides"
                            )
                        if not isinstance(assignment.assets, tuple):
                            raise ValueError("Actifs Shift v2 non explicites")
                        if any(
                            not asset.association_kind
                            for asset in assignment.assets
                        ):
                            raise ValueError(
                                "Provenance d'association d'actif v2 manquante"
                            )
                        hours_by_resource[assignment.resource_id] = round(
                            hours_by_resource.get(assignment.resource_id, 0.0)
                            + float(assignment.hours),
                            2,
                        )
                    actual.add(assignment.shift_id)
                    seen.add(assignment.shift_id)
                if actual != expected:
                    raise ValueError("Couverture de provenance chantier incomplète")
                if version == 2:
                    for resource in task.resources:
                        if round(float(resource.hours), 2) != hours_by_resource.get(
                            resource.resource_id,
                            0.0,
                        ):
                            raise ValueError(
                                "Heures Shift v2 incohérentes avec le total ressource"
                            )


def _contact_semantics(project: ProjectCommunicationProject) -> tuple[object, ...]:
    """Communication-owned semantics only; exclude replaceable physical identifiers."""
    days = []
    for day in project.days:
        tasks = []
        for task in day.tasks:
            grouped: dict[tuple[object, ...], float] = {}
            for row in task.request_assignments:
                key = (
                    row.workforce_request_id,
                    row.resource_id,
                    row.site_contact_name,
                    row.site_contact_phone,
                )
                grouped[key] = round(grouped.get(key, 0.0) + row.hours, 2)
            tasks.append((
                task.task_description.strip().casefold(),
                tuple(sorted(
                    ((key, hours) for key, hours in grouped.items()),
                    key=repr,
                )),
            ))
        days.append((day.day.isoformat(), tuple(tasks)))
    return tuple(days)


def _shift_v2_semantics(
    project: ProjectCommunicationProject,
) -> tuple[object, ...]:
    """Capture complete Shift communication facts without physical Shift/requirement IDs."""

    days: list[object] = []
    for day in project.days:
        tasks: list[object] = []
        for task in day.tasks:
            assignments = tuple(
                sorted(
                    (
                        (
                            row.workforce_request_id,
                            row.request_number,
                            row.resource_id,
                            row.resource_name,
                            round(float(row.hours), 2),
                            round(float(row.standard_hours or 0.0), 2),
                            tuple(
                                sorted(
                                    (
                                        _asset_payload(asset)
                                        for asset in employee_assigned_assets(row)
                                    ),
                                    key=repr,
                                )
                            ),
                        )
                        for row in task.request_assignments
                    ),
                    key=repr,
                )
            )
            tasks.append(
                (
                    task.task_description.strip().casefold(),
                    assignments,
                )
            )
        days.append((day.day.isoformat(), tuple(tasks)))
    return tuple(days)


def _has_site_contact(project: ProjectCommunicationProject) -> bool:
    return any(
        row.site_contact_name or row.site_contact_phone
        for day in project.days
        for task in day.tasks
        for row in task.request_assignments
    )


def _project_semantic_payload(
    project: ProjectCommunicationProject,
    *,
    include_provenance: bool,
) -> tuple[object, ...]:
    original = _project_payload(project)
    return (
        original,
        _contact_semantics(project) if include_provenance else (),
    )


def _project_v2_semantic_payload(
    project: ProjectCommunicationProject,
) -> tuple[object, ...]:
    return (
        _project_semantic_payload(project, include_provenance=True),
        ("shift_provenance_v2", _shift_v2_semantics(project)),
    )


def _fingerprint_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def project_projection_fingerprint(
    projection: ProjectCommunicationProjection,
) -> str:
    version = projection.request_provenance_version
    if version is not None:
        _validate_provenance(projection)

    if version == 2:
        payload = (
            "project_provenance_v2",
            projection.week_start.isoformat(),
            projection.week_end.isoformat(),
            tuple(
                _project_v2_semantic_payload(project)
                for project in projection.projects
            ),
            tuple(projection.diagnostics),
        )
    else:
        # Preserve the historical P0/P1 algorithms exactly.
        payload = (
            projection.week_start.isoformat(),
            projection.week_end.isoformat(),
            tuple(
                _project_semantic_payload(project, include_provenance=True)
                if version == 1
                else _project_payload(project)
                for project in projection.projects
            ),
            tuple(projection.diagnostics),
        )
    return _fingerprint_payload(payload)


def _diagnostic(
    *,
    code: str,
    severity: str,
    entity_type: str,
    entity_id: str,
    message: str,
) -> ProjectMessageDiagnostic:
    return ProjectMessageDiagnostic(
        code=code,
        severity=severity,
        entity_type=entity_type,
        entity_id=entity_id,
        message=message,
    )


def _recipient_diagnostics(
    project: ProjectCommunicationProject,
    cc_sources: Sequence[ProjectCommunicationProject],
) -> tuple[ProjectMessageDiagnostic, ...]:
    diagnostics: list[ProjectMessageDiagnostic] = []
    manager = project.project_manager
    if not manager.active:
        diagnostics.append(
            _diagnostic(
                code=DIAGNOSTIC_TO_INACTIVE,
                severity=SEVERITY_BLOCKING,
                entity_type="project_manager",
                entity_id=manager.contact_id or manager.user_id or project.project_id,
                message="Le chargé de projet n'est pas un utilisateur métier actif.",
            )
        )
    if not (manager.email or "").strip():
        diagnostics.append(
            _diagnostic(
                code=DIAGNOSTIC_TO_EMAIL_MISSING,
                severity=SEVERITY_BLOCKING,
                entity_type="project_manager",
                entity_id=manager.contact_id or manager.user_id or project.project_id,
                message="Le chargé de projet n'a pas de courriel explicite.",
            )
        )

    for co_manager in project.project_co_managers:
        entity_id = (
            co_manager.contact_id
            or co_manager.user_id
            or project.project_id
        )
        if not co_manager.active:
            diagnostics.append(
                _diagnostic(
                    code=DIAGNOSTIC_CO_MANAGER_TO_INACTIVE,
                    severity=SEVERITY_BLOCKING,
                    entity_type="project_co_manager",
                    entity_id=entity_id,
                    message=(
                        f"Le co-chargé {co_manager.display_name} n'est pas "
                        "un contact métier actif et ne peut pas être destinataire To."
                    ),
                )
            )
        if not (co_manager.email or "").strip():
            diagnostics.append(
                _diagnostic(
                    code=DIAGNOSTIC_CO_MANAGER_TO_EMAIL_MISSING,
                    severity=SEVERITY_BLOCKING,
                    entity_type="project_co_manager",
                    entity_id=entity_id,
                    message=(
                        f"Le co-chargé {co_manager.display_name} n'a pas de "
                        "courriel explicite et ne peut pas être destinataire To."
                    ),
                )
            )

    for requester in project.requesters:
        entity_id = (
            requester.contact_id
            or requester.user_id
            or project.project_id
        )
        if not requester.active:
            diagnostics.append(
                _diagnostic(
                    code=DIAGNOSTIC_REQUESTER_TO_INACTIVE,
                    severity=SEVERITY_BLOCKING,
                    entity_type="requester",
                    entity_id=entity_id,
                    message=(
                        f"Le demandeur {requester.display_name} n'est pas "
                        "un utilisateur actif et ne peut pas être destinataire To."
                    ),
                )
            )
        if not (requester.email or "").strip():
            diagnostics.append(
                _diagnostic(
                    code=DIAGNOSTIC_REQUESTER_TO_EMAIL_MISSING,
                    severity=SEVERITY_BLOCKING,
                    entity_type="requester",
                    entity_id=entity_id,
                    message=(
                        f"Le demandeur {requester.display_name} n'a pas de "
                        "courriel canonique et ne peut pas être destinataire To."
                    ),
                )
            )

    resources = _resource_participants(cc_sources)
    for resource_id, participant in sorted(resources.items()):
        if not participant.active:
            diagnostics.append(
                _diagnostic(
                    code=DIAGNOSTIC_CC_INACTIVE,
                    severity=SEVERITY_WARNING,
                    entity_type="resource",
                    entity_id=resource_id,
                    message=(
                        f"La ressource {participant.display_name} est inactive "
                        "ou incohérente dans le référentiel de ressources."
                    ),
                )
            )
        if not (participant.email or "").strip():
            diagnostics.append(
                _diagnostic(
                    code=DIAGNOSTIC_CC_EMAIL_MISSING,
                    severity=SEVERITY_WARNING,
                    entity_type="resource",
                    entity_id=resource_id,
                    message=(
                        f"La ressource {participant.display_name} n'a pas de "
                        "courriel explicite et ne peut pas être ajoutée en CC."
                    ),
                )
            )

    asset_qualification_messages = {
        "ASSET_QUALIFICATION_MISSING_OPERATOR": (
            "Une réservation d'actif confirmée exige un opérateur qualifié."
        ),
        "ASSET_QUALIFICATION_SKILL_MISMATCH": (
            "L'opérateur lié à une réservation d'actif ne possède plus la qualification requise."
        ),
        "ASSET_QUALIFICATION_NO_OVERLAP": (
            "L'opérateur qualifié n'a plus d'affectation humaine compatible avec la réservation d'actif."
        ),
    }
    for source_code in project.diagnostics:
        is_asset_qualification = source_code.startswith(
            ASSET_QUALIFICATION_DIAGNOSTIC_PREFIX
        )
        diagnostics.append(
            _diagnostic(
                code=source_code if is_asset_qualification else DIAGNOSTIC_SOURCE,
                severity=(
                    SEVERITY_BLOCKING
                    if is_asset_qualification
                    else SEVERITY_WARNING
                ),
                entity_type="project",
                entity_id=project.project_id,
                message=asset_qualification_messages.get(source_code, source_code),
            )
        )
    return tuple(diagnostics)


def _resource_participants(
    projects: Sequence[ProjectCommunicationProject],
) -> dict[str, ProjectCommunicationParticipant]:
    """Return one contact per resource, preferring the latest supplied projection."""

    resources: dict[str, ProjectCommunicationParticipant] = {}
    for project in projects:
        for day in project.days:
            for task in day.tasks:
                for resource in task.resources:
                    resources[resource.resource_id] = resource.contact
    return resources


def _to_recipients(
    project: ProjectCommunicationProject,
) -> tuple[ProjectCommunicationParticipant, ...]:
    recipients: list[ProjectCommunicationParticipant] = []
    seen_identities: set[tuple[str, str]] = set()
    seen_emails: set[str] = set()
    for participant in (
        project.project_manager,
        *project.project_co_managers,
        *project.requesters,
    ):
        identity = None
        if participant.contact_id:
            identity = ("contact", participant.contact_id)
        elif participant.user_id:
            identity = ("user", participant.user_id)
        email = (participant.email or "").strip().casefold()
        if identity is not None and identity in seen_identities:
            continue
        if email and email in seen_emails:
            continue
        if identity is not None:
            seen_identities.add(identity)
        if email:
            seen_emails.add(email)
        recipients.append(participant)
    return tuple(recipients)


def _cc_recipients(
    projects: Sequence[ProjectCommunicationProject],
    *,
    exclude_emails: Sequence[str] = (),
) -> tuple[ProjectCommunicationParticipant, ...]:
    excluded = {
        str(email or "").strip().casefold()
        for email in exclude_emails
        if str(email or "").strip()
    }
    by_email: dict[str, ProjectCommunicationParticipant] = {}
    for participant in _resource_participants(projects).values():
        email = (participant.email or "").strip()
        if not participant.active or not email:
            continue
        if email.casefold() in excluded:
            continue
        by_email.setdefault(email.casefold(), participant)
    return tuple(
        by_email[key]
        for key in sorted(
            by_email,
            key=lambda email: (
                by_email[email].display_name.casefold(),
                email,
            ),
        )
    )


PROJECT_RESPONSIBILITY_FOOTER = (
    "Il est de votre responsabilité de communiquer avec les ressources déployées "
    "sur votre projet pour assurer une bonne coordination de chantier et les "
    "informer de tout détail supplémentaire nécessaire."
)
_SECTION_LABELS = (
    "Responsable projet",
    "Description",
    "Contact chantier",
    "Horaire",
)
_DATE_LINE_PATTERN = re.compile(
    r"^(Lundi|Mardi|Mercredi|Jeudi|Vendredi|Samedi|Dimanche) le "
    r"\d{1,2} [A-Za-zÀ-ÿ]+ \d{4}$"
)
_EMPLOYEE_NUMBER_PREFIX = re.compile(r"^\s*\d+\s*[-–—]\s*(.+)$")


def _line(kind: str, text: str = "") -> ProjectMessageLine:
    return ProjectMessageLine(kind=kind, text=text)


def _asset_display(value: ProjectCommunicationAsset) -> str:
    return (value.asset_code or "").strip()


def _display_resource_name(value: str) -> str:
    cleaned = str(value or "").strip()
    matched = _EMPLOYEE_NUMBER_PREFIX.match(cleaned)
    return matched.group(1).strip() if matched else cleaned


def _task_display(task: ProjectCommunicationTask) -> str:
    codes = " / ".join(dict.fromkeys(code for code in task.task_codes if code))
    description = task.task_description.strip()
    if codes and description:
        return f"{codes} {description}"
    return codes or description


def _request_key(
    assignment: ProjectCommunicationRequestAssignment,
) -> tuple[str | None, str | None]:
    return assignment.workforce_request_id, assignment.request_number


def _request_label(key: tuple[str | None, str | None]) -> str:
    request_id, request_number = key
    if request_id is None:
        return "Sans demande"
    return f"Demande {request_number or request_id}"


def _contact_display(
    name: str | None,
    phone: str | None,
) -> str:
    contact_name = (name or "").strip()
    contact_phone = (phone or "").strip()
    if contact_name and contact_phone:
        return f"{contact_name} : {contact_phone}"
    return contact_name or contact_phone or "Aucun contact chantier renseigné"


def _project_request_groups(
    project: ProjectCommunicationProject,
) -> dict[
    tuple[str | None, str | None],
    list[tuple[ProjectCommunicationDay, ProjectCommunicationTask, ProjectCommunicationRequestAssignment]],
]:
    groups: dict[
        tuple[str | None, str | None],
        list[tuple[ProjectCommunicationDay, ProjectCommunicationTask, ProjectCommunicationRequestAssignment]],
    ] = {}
    for day in project.days:
        for task in day.tasks:
            for assignment in task.request_assignments:
                groups.setdefault(_request_key(assignment), []).append(
                    (day, task, assignment)
                )
    return groups


def _description_lines(project: ProjectCommunicationProject) -> tuple[str, ...]:
    values: list[str] = []
    for day in project.days:
        for task in day.tasks:
            value = _task_display(task)
            if value and value not in values:
                values.append(value)
    return tuple(values)


def _contact_lines(project: ProjectCommunicationProject) -> tuple[str, ...]:
    groups = _project_request_groups(project)
    if not groups:
        return ("Aucun contact chantier renseigné",)

    if len(groups) == 1:
        key, rows = next(iter(groups.items()))
        first = rows[0][2]
        if key[0] is None:
            return ("Aucun contact chantier renseigné",)
        return (_contact_display(first.site_contact_name, first.site_contact_phone),)

    values: list[str] = []
    for key, rows in sorted(
        groups.items(),
        key=lambda pair: (
            pair[0][1] or "",
            pair[0][0] or "",
        ),
    ):
        contacts: list[str] = []
        for _day, _task, assignment in rows:
            contact = _contact_display(
                assignment.site_contact_name,
                assignment.site_contact_phone,
            )
            if contact not in contacts:
                contacts.append(contact)
        values.append(f"{_request_label(key)} — {' / '.join(contacts)}")
    return tuple(values)


def _shift_resource_line(
    assignment: ProjectCommunicationRequestAssignment,
) -> str:
    text = _display_resource_name(assignment.resource_name)
    if (
        assignment.standard_hours is not None
        and float(assignment.hours) < float(assignment.standard_hours)
    ):
        text += f" — {float(assignment.hours):g} h"

    asset_codes = tuple(
        dict.fromkeys(
            code
            for code in (
                _asset_display(asset)
                for asset in employee_assigned_assets(assignment)
            )
            if code
        )
    )
    if asset_codes:
        text += f" ({' / '.join(asset_codes)})"
    return text


def _day_schedule_lines(
    day: ProjectCommunicationDay,
    *,
    show_request_groups: bool,
) -> tuple[ProjectMessageLine, ...]:
    grouped: dict[
        tuple[str | None, str | None],
        list[tuple[ProjectCommunicationTask, ProjectCommunicationRequestAssignment]],
    ] = {}
    legacy_resources: list[ProjectCommunicationResource] = []
    for task in day.tasks:
        if task.request_assignments:
            for assignment in task.request_assignments:
                grouped.setdefault(_request_key(assignment), []).append(
                    (task, assignment)
                )
        else:
            legacy_resources.extend(task.resources)

    lines: list[ProjectMessageLine] = [_line("date", french_long_date(day.day))]
    for key, rows in sorted(
        grouped.items(),
        key=lambda pair: (pair[0][1] or "", pair[0][0] or ""),
    ):
        if show_request_groups:
            works = tuple(
                dict.fromkeys(
                    value
                    for value in (_task_display(task) for task, _row in rows)
                    if value
                )
            )
            label = _request_label(key)
            if works:
                label += " — " + " / ".join(works)
            lines.append(_line("group", label))
        for _task, assignment in sorted(
            rows,
            key=lambda value: (
                _display_resource_name(value[1].resource_name).casefold(),
                value[1].shift_id,
            ),
        ):
            lines.append(_line("bullet", _shift_resource_line(assignment)))

    # Historical snapshots without Shift provenance stay renderable without
    # inventing standard hours or Shift assets that were never persisted.
    for resource in sorted(
        legacy_resources,
        key=lambda value: (value.resource_name.casefold(), value.resource_id),
    ):
        lines.append(_line("bullet", _display_resource_name(resource.resource_name)))
    return tuple(lines)


def _project_semantic_message(
    project: ProjectCommunicationProject,
    *,
    change_kind: str | None = None,
) -> ProjectMessageSemantic:
    lines: list[ProjectMessageLine] = [
        _line(
            "project_header",
            f"{project.project_number} - {project.project_name}".strip(" -"),
        )
    ]
    if change_kind is not None:
        labels = {
            "added": "Projet ajouté au planning communiqué.",
            "removed": "Projet retiré du planning communiqué.",
            "modified": "Mise à jour du planning communiqué.",
        }
        lines.extend((_line("blank"), _line("text", labels[change_kind])))

    lines.extend(
        (
            _line("blank"),
            _line("section", "Responsable projet"),
            _line("text", project.project_manager.display_name),
        )
    )
    if project.project_manager_erp_phone:
        lines.append(_line("text", project.project_manager_erp_phone))

    lines.extend((_line("blank"), _line("section", "Description")))
    for value in _description_lines(project):
        lines.append(_line("text", value))

    lines.extend((_line("blank"), _line("section", "Contact chantier")))
    for value in _contact_lines(project):
        lines.append(_line("text", value))

    lines.extend((_line("blank"), _line("section", "Horaire")))
    show_request_groups = len(_project_request_groups(project)) > 1
    for day in project.days:
        lines.append(_line("blank"))
        lines.extend(
            _day_schedule_lines(
                day,
                show_request_groups=show_request_groups,
            )
        )

    lines.extend(
        (
            _line("blank"),
            _line("footer", PROJECT_RESPONSIBILITY_FOOTER),
        )
    )
    return ProjectMessageSemantic(lines=tuple(lines))


def _render_project_text(message: ProjectMessageSemantic) -> str:
    values: list[str] = []
    for line in message.lines:
        if line.kind == "blank":
            values.append("")
        elif line.kind == "bullet":
            values.append(f"• {line.text}")
        else:
            values.append(line.text)
    return "\n".join(values)


def _render_project_html(message: ProjectMessageSemantic) -> str:
    rendered: list[str] = [
        '<div style="font-family:Arial,Helvetica,sans-serif;line-height:1.25;max-width:640px;">'
    ]
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            rendered.append("</ul>")
            in_list = False

    for line in message.lines:
        value = escape(line.text)
        if line.kind == "bullet":
            if not in_list:
                rendered.append(
                    '<ul style="margin:0 0 0 20px;padding:0;">'
                )
                in_list = True
            rendered.append(
                f'<li style="margin:0;padding:0;">{value}</li>'
            )
            continue

        close_list()
        if line.kind == "blank":
            rendered.append(
                '<div style="height:12px;line-height:12px;">&nbsp;</div>'
            )
        elif line.kind == "project_header":
            rendered.append(
                f'<div style="margin:0;"><strong>{value}</strong></div>'
            )
        elif line.kind == "section":
            rendered.append(
                f'<div style="margin:0 0 2px 0;"><strong><u>{value}</u></strong></div>'
            )
        elif line.kind == "date":
            rendered.append(
                f'<div style="margin:0 0 2px 0;"><strong>{value}</strong></div>'
            )
        elif line.kind == "group":
            rendered.append(f'<div style="margin:4px 0 2px 0;">{value}</div>')
        elif line.kind == "footer":
            rendered.append(f'<div style="margin-top:12px;">{value}</div>')
        else:
            rendered.append(f'<div style="margin:0;">{value}</div>')
    close_list()
    rendered.append("</div>")
    return "".join(rendered)


def _semantic_from_review_body(body: str) -> ProjectMessageSemantic:
    raw_lines = str(body).splitlines()
    nonempty = [(index, value) for index, value in enumerate(raw_lines) if value.strip()]
    if not nonempty:
        raise ValueError("Le corps révisé du courriel projet ne peut pas être vide.")
    if nonempty[-1][1].strip() != PROJECT_RESPONSIBILITY_FOOTER:
        raise ValueError(
            "Le message obligatoire de responsabilité doit rester intact à la fin du courriel."
        )

    section_positions: list[int] = []
    for label in _SECTION_LABELS:
        positions = [
            index
            for index, value in enumerate(raw_lines)
            if value.strip() == label
        ]
        if len(positions) != 1:
            raise ValueError(
                f"La section obligatoire « {label} » doit apparaître exactement une fois."
            )
        section_positions.append(positions[0])
    if section_positions != sorted(section_positions):
        raise ValueError("L'ordre obligatoire des sections du courriel projet a été modifié.")
    if nonempty[0][0] >= section_positions[0]:
        raise ValueError("L'en-tête projet obligatoire est manquant.")

    semantic: list[ProjectMessageLine] = []
    first_nonempty = nonempty[0][0]
    for index, value in enumerate(raw_lines):
        stripped = value.strip()
        if not stripped:
            semantic.append(_line("blank"))
        elif index == first_nonempty:
            semantic.append(_line("project_header", stripped))
        elif stripped in _SECTION_LABELS:
            semantic.append(_line("section", stripped))
        elif stripped == PROJECT_RESPONSIBILITY_FOOTER:
            semantic.append(_line("footer", stripped))
        elif _DATE_LINE_PATTERN.match(stripped):
            semantic.append(_line("date", stripped))
        elif stripped.startswith("• "):
            semantic.append(_line("bullet", stripped[2:].strip()))
        else:
            semantic.append(_line("text", stripped))
    return ProjectMessageSemantic(lines=tuple(semantic))


def project_review_structure_signature(body: str) -> tuple[str, ...]:
    """Return the template structure that a manual review is allowed to preserve."""

    return tuple(line.kind for line in _semantic_from_review_body(body).lines)


def render_project_review_html(body: str) -> str:
    """Regenerate safe HTML while preserving the mandatory project template."""

    return _render_project_html(_semantic_from_review_body(body))


def _subject(
    project: ProjectCommunicationProject,
    week_start: date,
    *,
    change: bool,
) -> str:
    prefix = "Modification main-d'œuvre" if change else "Confirmation main-d'œuvre"
    return (
        f"{prefix} — {project.project_number} — "
        f"semaine du {week_start.day} {_MONTHS_FR[week_start.month]} {week_start.year}"
    )


def _draft_fingerprint_payload(
    *,
    message_key: str,
    to_recipients: Sequence[ProjectCommunicationParticipant],
    cc_recipients: Sequence[ProjectCommunicationParticipant],
    subject: str,
    body: str,
    body_html: str | None,
    message_kind: str,
    diagnostics: Sequence[ProjectMessageDiagnostic],
    source_projects: Sequence[ProjectCommunicationProject],
) -> tuple[object, ...]:
    return (
        message_key,
        tuple(_participant_payload(value) for value in to_recipients),
        tuple(_participant_payload(value) for value in cc_recipients),
        subject,
        body,
        body_html,
        message_kind,
        tuple(
            (
                value.code,
                value.severity,
                value.entity_type,
                value.entity_id,
                value.message,
            )
            for value in diagnostics
        ),
        tuple(_project_payload(value) for value in source_projects),
    )


def _build_draft(
    *,
    project: ProjectCommunicationProject,
    week_start: date,
    message_kind: str,
    cc_sources: Sequence[ProjectCommunicationProject],
    change_kind: str | None = None,
) -> ProjectCommunicationDraft:
    message_key = f"project:{project.project_id}"
    to_recipients = _to_recipients(project)
    cc_recipients = _cc_recipients(
        cc_sources,
        exclude_emails=tuple(
            participant.email or ""
            for participant in to_recipients
        ),
    )
    diagnostics = _recipient_diagnostics(project, cc_sources)
    subject = _subject(
        project,
        week_start,
        change=message_kind == MESSAGE_KIND_PLANNING_CHANGE,
    )
    semantic = _project_semantic_message(project, change_kind=change_kind)
    body = _render_project_text(semantic)
    body_html = _render_project_html(semantic)
    fingerprint = _fingerprint_payload(
        _draft_fingerprint_payload(
            message_key=message_key,
            to_recipients=to_recipients,
            cc_recipients=cc_recipients,
            subject=subject,
            body=body,
            body_html=body_html,
            message_kind=message_kind,
            diagnostics=diagnostics,
            source_projects=cc_sources,
        )
    )
    approvable = not any(
        value.severity == SEVERITY_BLOCKING for value in diagnostics
    )
    return ProjectCommunicationDraft(
        message_key=message_key,
        audience="project",
        project_id=project.project_id,
        project_number=project.project_number,
        to_recipient=project.project_manager,
        to_recipients=to_recipients,
        cc_recipients=cc_recipients,
        subject=subject,
        body=body,
        body_html=body_html,
        message_kind=message_kind,
        week_start=week_start,
        content_fingerprint=fingerprint,
        approvable=approvable,
        diagnostics=diagnostics,
    )


def _batch_fingerprint(
    drafts: Sequence[ProjectCommunicationDraft],
    *,
    week_start: date,
) -> str:
    return _fingerprint_payload(
        (
            week_start.isoformat(),
            tuple(
                (
                    draft.message_key,
                    draft.content_fingerprint,
                    draft.approvable,
                )
                for draft in sorted(drafts, key=lambda value: value.message_key)
            ),
        )
    )


def build_project_confirmation_batch(
    projection: ProjectCommunicationProjection,
) -> ProjectCommunicationMessageBatch:
    drafts = tuple(
        _build_draft(
            project=project,
            week_start=projection.week_start,
            message_kind=MESSAGE_KIND_WEEKLY_CONFIRMATION,
            cc_sources=(project,),
        )
        for project in projection.projects
    )
    diagnostics = tuple(
        diagnostic for draft in drafts for diagnostic in draft.diagnostics
    )
    return ProjectCommunicationMessageBatch(
        drafts=drafts,
        snapshot_fingerprint=project_projection_fingerprint(projection),
        diagnostics=diagnostics,
    )


def compare_project_communication_projections(
    previous: ProjectCommunicationProjection,
    current: ProjectCommunicationProjection,
) -> tuple[ProjectCommunicationChange, ...]:
    if (
        previous.week_start != current.week_start
        or previous.week_end != current.week_end
    ):
        raise ValueError(
            "Les deltas de communication doivent comparer la même semaine."
        )

    current_version = current.request_provenance_version
    previous_version = previous.request_provenance_version
    if current_version not in (1, 2) or not current.request_provenance_complete:
        raise ValueError("Projection courante sans provenance chantier complète")
    _validate_provenance(current)
    if previous_version not in (None, 1, 2):
        raise ValueError("Version historique de provenance chantier non supportée")
    if previous_version is not None:
        _validate_provenance(previous)
    if previous_version == 2 and current_version != 2:
        raise ValueError("Régression de capacité de provenance chantier interdite")

    before = {project.project_id: project for project in previous.projects}
    after = {project.project_id: project for project in current.projects}
    changes: list[ProjectCommunicationChange] = []
    for project_id in sorted(set(before) | set(after)):
        old = before.get(project_id)
        new = after.get(project_id)
        if old is None:
            changes.append(ProjectCommunicationChange(project_id, "added", None, new))
        elif new is None:
            changes.append(ProjectCommunicationChange(project_id, "removed", old, None))
        elif current_version == 2 and previous_version in (None, 1):
            # Historical snapshots cannot prove equality for v2 Shift facts.
            changes.append(ProjectCommunicationChange(project_id, "modified", old, new))
        elif current_version == 2:
            if _project_v2_semantic_payload(old) != _project_v2_semantic_payload(new):
                changes.append(ProjectCommunicationChange(project_id, "modified", old, new))
        elif previous_version is None:
            if (
                _project_payload(old) != _project_payload(new)
                or _has_site_contact(new)
            ):
                changes.append(ProjectCommunicationChange(project_id, "modified", old, new))
        elif (
            _project_semantic_payload(old, include_provenance=True)
            != _project_semantic_payload(new, include_provenance=True)
        ):
            changes.append(ProjectCommunicationChange(project_id, "modified", old, new))
    return tuple(changes)


def build_project_delta_batch(
    previous: ProjectCommunicationProjection,
    current: ProjectCommunicationProjection,
) -> ProjectCommunicationMessageBatch:
    changes = compare_project_communication_projections(previous, current)
    drafts: list[ProjectCommunicationDraft] = []
    for change in changes:
        project = change.after or change.before
        assert project is not None
        cc_sources = tuple(
            value for value in (change.before, change.after) if value is not None
        )
        drafts.append(
            _build_draft(
                project=project,
                week_start=current.week_start,
                message_kind=MESSAGE_KIND_PLANNING_CHANGE,
                cc_sources=cc_sources,
                change_kind=change.kind,
            )
        )
    result = tuple(drafts)
    diagnostics = tuple(
        diagnostic for draft in result for diagnostic in draft.diagnostics
    )
    return ProjectCommunicationMessageBatch(
        drafts=result,
        snapshot_fingerprint=project_projection_fingerprint(current),
        diagnostics=diagnostics,
    )


def serialize_project_projection(
    projection: ProjectCommunicationProjection,
) -> str:
    """Serialize the immutable communicated projection for durable delta baselines."""

    def convert(value):
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return value

    if projection.request_provenance_version is not None:
        _validate_provenance(projection)
    serialized = convert(asdict(projection))
    # Absent #831 data must remain byte-for-byte compatible with previous
    # project_v2 JSON and previous P1 fingerprint inputs.
    for project in serialized["projects"]:
        if not project.get("project_manager_erp_phone"):
            project.pop("project_manager_erp_phone", None)
    if projection.request_provenance_version is None:
        serialized.pop("request_provenance_version", None)
        serialized.pop("request_provenance_complete", None)
        for project in serialized["projects"]:
            for day in project["days"]:
                for task in day["tasks"]:
                    task.pop("request_assignments", None)
    elif projection.request_provenance_version == 1:
        # Preserve historical v1 JSON byte shape: v2 fields did not exist.
        for project in serialized["projects"]:
            for day in project["days"]:
                for task in day["tasks"]:
                    for assignment in task.get("request_assignments") or ():
                        assignment.pop("assets", None)
                        assignment.pop("standard_hours", None)
    return json.dumps(
        serialized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def deserialize_project_projection(payload: str) -> ProjectCommunicationProjection:
    """Restore a projection snapshot without consulting mutable business tables."""

    data = json.loads(payload)
    provenance_version = data.get("request_provenance_version")
    provenance_complete = data.get("request_provenance_complete", False)
    if provenance_version is None:
        if provenance_complete or any(
            "request_assignments" in task
            for project in data.get("projects") or ()
            for day in project.get("days") or ()
            for task in day.get("tasks") or ()
        ):
            raise ValueError("Snapshot chantier legacy incohérent")
    elif provenance_version not in (1, 2) or provenance_complete is not True:
        raise ValueError("Version ou couverture de provenance chantier invalide")

    if provenance_version == 1:
        for project in data.get("projects") or ():
            for day in project.get("days") or ():
                for task in day.get("tasks") or ():
                    for assignment in task.get("request_assignments") or ():
                        if (
                            "assets" in assignment
                            or "standard_hours" in assignment
                        ):
                            raise ValueError(
                                "Snapshot provenance v1 contient des faits v2"
                            )

    if provenance_version == 2:
        for project in data.get("projects") or ():
            for day in project.get("days") or ():
                for task in day.get("tasks") or ():
                    for assignment in task.get("request_assignments") or ():
                        if (
                            "assets" not in assignment
                            or not isinstance(assignment.get("assets"), list)
                            or "standard_hours" not in assignment
                            or assignment.get("standard_hours") is None
                        ):
                            raise ValueError(
                                "Snapshot provenance v2 incomplet: actifs/heures standard absents"
                            )
                        try:
                            standard_hours = float(assignment["standard_hours"])
                        except (TypeError, ValueError) as exc:
                            raise ValueError(
                                "Snapshot provenance v2 invalide: heures standard non numériques"
                            ) from exc
                        if not math.isfinite(standard_hours) or standard_hours < 0:
                            raise ValueError(
                                "Snapshot provenance v2 invalide: heures standard"
                            )

    def participant(row):
        return ProjectCommunicationParticipant(
            contact_id=row.get("contact_id"),
            user_id=row.get("user_id"),
            display_name=str(row.get("display_name") or ""),
            email=row.get("email"),
            phone=row.get("phone"),
            active=bool(row.get("active")),
            diagnostics=tuple(row.get("diagnostics") or ()),
        )

    def resolution(row):
        return ContactResolution(
            status=str(row.get("status") or ""),
            contact_id=row.get("contact_id"),
            display_name=row.get("display_name"),
            email=row.get("email"),
            phone=row.get("phone"),
            source_type=str(row.get("source_type") or ""),
            source_entity_id=row.get("source_entity_id"),
            source_label=row.get("source_label"),
            diagnostics=tuple(row.get("diagnostics") or ()),
        )

    projects = []
    for project_row in data.get("projects") or ():
        days = []
        for day_row in project_row.get("days") or ():
            tasks = []
            for task_row in day_row.get("tasks") or ():
                resources = tuple(
                    ProjectCommunicationResource(
                        resource_id=str(resource_row.get("resource_id") or ""),
                        resource_name=str(resource_row.get("resource_name") or ""),
                        contact=participant(resource_row.get("contact") or {}),
                        hours=float(resource_row.get("hours") or 0),
                        shift_ids=tuple(resource_row.get("shift_ids") or ()),
                        allocation_types=tuple(
                            resource_row.get("allocation_types") or ()
                        ),
                        confirmations=tuple(
                            resource_row.get("confirmations") or ()
                        ),
                        outside_schedule=bool(
                            resource_row.get("outside_schedule")
                        ),
                        assets=tuple(
                            ProjectCommunicationAsset(
                                allocation_id=str(asset_row.get("allocation_id") or ""),
                                asset_id=str(asset_row.get("asset_id") or ""),
                                asset_code=str(asset_row.get("asset_code") or ""),
                                asset_label=str(asset_row.get("asset_label") or ""),
                                asset_type_label=str(asset_row.get("asset_type_label") or ""),
                                asset_category=str(asset_row.get("asset_category") or ""),
                                association_kind=str(asset_row.get("association_kind") or ""),
                            )
                            for asset_row in resource_row.get("assets") or ()
                        ),
                        diagnostics=tuple(
                            resource_row.get("diagnostics") or ()
                        ),
                    )
                    for resource_row in task_row.get("resources") or ()
                )
                responsibility_assignments = tuple(
                    ProjectCommunicationResponsibilityAssignment(
                        shift_id=str(value.get("shift_id") or ""),
                        resource_id=str(value.get("resource_id") or ""),
                        resource_name=str(value.get("resource_name") or ""),
                        operational_responsible=resolution(
                            value.get("operational_responsible") or {}
                        ),
                    )
                    for value in task_row.get("responsibility_assignments") or ()
                )
                tasks.append(
                    ProjectCommunicationTask(
                        task_description=str(
                            task_row.get("task_description") or ""
                        ),
                        task_ids=tuple(task_row.get("task_ids") or ()),
                        task_codes=tuple(task_row.get("task_codes") or ()),
                        operational_responsibles=tuple(
                            resolution(value)
                            for value in task_row.get(
                                "operational_responsibles"
                            ) or ()
                        ),
                        resources=resources,
                        responsibility_assignments=responsibility_assignments,
                        request_assignments=tuple(
                            ProjectCommunicationRequestAssignment(
                                shift_id=str(value["shift_id"]),
                                requirement_id=str(value["requirement_id"]),
                                workforce_request_id=value.get("workforce_request_id"),
                                request_number=value.get("request_number"),
                                resource_id=str(value["resource_id"]),
                                resource_name=str(value["resource_name"]),
                                hours=float(value["hours"]),
                                site_contact_name=value.get("site_contact_name"),
                                site_contact_phone=value.get("site_contact_phone"),
                                assets=tuple(
                                    ProjectCommunicationAsset(
                                        allocation_id=str(asset.get("allocation_id") or ""),
                                        asset_id=str(asset.get("asset_id") or ""),
                                        asset_code=str(asset.get("asset_code") or ""),
                                        asset_label=str(asset.get("asset_label") or ""),
                                        asset_type_label=str(asset.get("asset_type_label") or ""),
                                        asset_category=str(asset.get("asset_category") or ""),
                                        association_kind=str(asset.get("association_kind") or ""),
                                    )
                                    for asset in (
                                        value["assets"]
                                        if provenance_version == 2
                                        else value.get("assets") or ()
                                    )
                                ),
                                standard_hours=(
                                    float(value["standard_hours"])
                                    if provenance_version == 2
                                    else (
                                        float(value["standard_hours"])
                                        if value.get("standard_hours") is not None
                                        else None
                                    )
                                ),
                            )
                            for value in task_row.get("request_assignments") or ()
                        ),
                        diagnostics=tuple(task_row.get("diagnostics") or ()),
                    )
                )
            days.append(
                ProjectCommunicationDay(
                    day=date.fromisoformat(str(day_row["day"])),
                    tasks=tuple(tasks),
                )
            )
        projects.append(
            ProjectCommunicationProject(
                project_id=str(project_row.get("project_id") or ""),
                project_number=str(project_row.get("project_number") or ""),
                project_name=str(project_row.get("project_name") or ""),
                project_manager=participant(
                    project_row.get("project_manager") or {}
                ),
                days=tuple(days),
                project_manager_erp_phone=project_row.get("project_manager_erp_phone") or None,
                project_co_managers=tuple(
                    participant(value)
                    for value in project_row.get("project_co_managers") or ()
                ),
                requesters=tuple(
                    participant(value)
                    for value in project_row.get("requesters") or ()
                ),
                diagnostics=tuple(project_row.get("diagnostics") or ()),
            )
        )
    projection = ProjectCommunicationProjection(
        week_start=date.fromisoformat(str(data["week_start"])),
        week_end=date.fromisoformat(str(data["week_end"])),
        projects=tuple(projects),
        request_provenance_version=provenance_version,
        request_provenance_complete=provenance_complete,
        diagnostics=tuple(data.get("diagnostics") or ()),
    )
    if provenance_version is not None:
        _validate_provenance(projection)
    return projection
