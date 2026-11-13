from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
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
    message_kind: str
    week_start: date
    content_fingerprint: str
    approvable: bool
    diagnostics: tuple[ProjectMessageDiagnostic, ...]


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
    if projection.request_provenance_version != 1 or not projection.request_provenance_complete:
        raise ValueError("Provenance chantier courante absente ou non prise en charge")
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
                for assignment in task.request_assignments:
                    if not assignment.shift_id or assignment.shift_id in seen:
                        raise ValueError("Shift de provenance manquant ou dupliqué")
                    if (not assignment.requirement_id or not assignment.resource_id or
                        assignment.hours <= 0):
                        raise ValueError("Attribution chantier invalide")
                    if not assignment.workforce_request_id and (
                        assignment.request_number or assignment.site_contact_name
                        or assignment.site_contact_phone
                    ):
                        raise ValueError("Contact chantier sans provenance demande")
                    if not any(
                        resource.resource_id == assignment.resource_id
                        and assignment.shift_id in resource.shift_ids
                        for resource in task.resources
                    ):
                        raise ValueError("Ressource de provenance incohérente")
                    actual.add(assignment.shift_id)
                    seen.add(assignment.shift_id)
                if actual != expected:
                    raise ValueError("Couverture de provenance chantier incomplète")


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
    _validate_provenance(projection)
    return _fingerprint_payload(
        (
            projection.week_start.isoformat(),
            projection.week_end.isoformat(),
            tuple(_project_semantic_payload(project, include_provenance=True) for project in projection.projects),
            tuple(projection.diagnostics),
        )
    )


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


def _unique_responsibles(
    project: ProjectCommunicationProject,
) -> tuple[ContactResolution, ...]:
    result: dict[tuple[object, ...], ContactResolution] = {}
    for day in project.days:
        for task in day.tasks:
            for responsible in task.operational_responsibles:
                key = (
                    responsible.status,
                    responsible.contact_id,
                    responsible.display_name,
                    responsible.phone,
                    responsible.source_type,
                )
                result.setdefault(key, responsible)
    return tuple(
        result[key]
        for key in sorted(
            result,
            key=lambda value: tuple("" if part is None else str(part) for part in value),
        )
    )


def _format_responsible(value: ContactResolution) -> str:
    if value.status != STATUS_RESOLVED or not (value.display_name or "").strip():
        return "Non résolu"
    parts = [value.display_name.strip()]
    if (value.phone or "").strip():
        parts.append(value.phone.strip())
    return " — ".join(parts)


def _project_header(project: ProjectCommunicationProject) -> list[str]:
    lines = [f"{project.project_number} — {project.project_name}".strip(" —")]
    responsibles = _unique_responsibles(project)
    resolved = tuple(
        value
        for value in responsibles
        if value.status == STATUS_RESOLVED and (value.display_name or "").strip()
    )
    if len(resolved) == 1:
        lines.append(f"Responsable : {_format_responsible(resolved[0])}")
    elif len(resolved) > 1:
        lines.append(
            "Responsables opérationnels : voir chaque affectation ci-dessous"
        )
    else:
        lines.append("Responsable : Non résolu")
    return lines


def _asset_display(value: ProjectCommunicationAsset) -> str:
    return (value.asset_code or value.asset_id).strip()


def _resource_suffix(resource) -> str:
    signals: list[str] = []
    if any(value == "Tentative" for value in resource.confirmations):
        signals.append("Tentative")
    if resource.outside_schedule:
        signals.append("hors horaire")
    if not signals:
        return ""
    return f" ({', '.join(signals)})"


def _project_body(
    project: ProjectCommunicationProject,
    *,
    change_kind: str | None = None,
) -> str:
    lines = _project_header(project)
    if change_kind is not None:
        labels = {
            "added": "Projet ajouté au planning communiqué.",
            "removed": "Projet retiré du planning communiqué.",
            "modified": "Mise à jour du planning communiqué.",
        }
        lines.extend(("", labels[change_kind]))

    all_responsibles = _unique_responsibles(project)
    multiple_responsibles = len(
        {
            value.contact_id or (value.display_name or "")
            for value in all_responsibles
            if value.status == STATUS_RESOLVED
        }
    ) > 1

    for day in project.days:
        for task in day.tasks:
            lines.extend(("", f"Description : {task.task_description}"))
            if task.request_assignments:
                by_request: dict[str | None, list[ProjectCommunicationRequestAssignment]] = {}
                for item in task.request_assignments:
                    by_request.setdefault(item.workforce_request_id, []).append(item)
                for request_id, group in sorted(by_request.items(), key=lambda item: item[0] or ""):
                    first = group[0]
                    label = first.request_number or ("Sans demande" if request_id is None else request_id)
                    people = " / ".join(sorted({person.resource_name for person in group}, key=str.casefold))
                    if first.site_contact_name or first.site_contact_phone:
                        contact = " — ".join(value for value in (first.site_contact_name, first.site_contact_phone) if value)
                        lines.append(f"Contact chantier — Demande {label} ({people}) : {contact}")
                    elif len(by_request) > 1:
                        lines.append(f"Demande {label} ({people}) : Aucun contact chantier renseigné")
            lines.append(f"Horaire : {french_long_date(day.day)}")
            assignments_by_resource: dict[str, list[ContactResolution]] = {}
            for assignment in task.responsibility_assignments:
                assignments_by_resource.setdefault(
                    assignment.resource_id,
                    [],
                ).append(assignment.operational_responsible)
            for resource in task.resources:
                responsible_suffix = ""
                if multiple_responsibles:
                    associated: dict[tuple[object, ...], ContactResolution] = {}
                    for value in assignments_by_resource.get(resource.resource_id, ()):
                        associated.setdefault(_responsible_payload(value), value)
                    resolved = tuple(
                        value
                        for value in associated.values()
                        if value.status == STATUS_RESOLVED
                    )
                    if resolved:
                        responsible_suffix = (
                            " · Responsable : "
                            + " / ".join(
                                _format_responsible(value)
                                for value in sorted(
                                    resolved,
                                    key=lambda value: (
                                        (value.display_name or "").casefold(),
                                        value.contact_id or "",
                                    ),
                                )
                            )
                        )
                    elif assignments_by_resource.get(resource.resource_id):
                        responsible_suffix = " · Responsable : Non résolu"
                asset_suffix = (
                    " · Actif : "
                    + " / ".join(_asset_display(asset) for asset in resource.assets)
                    if resource.assets
                    else ""
                )
                lines.append(
                    f"  • {resource.resource_name} — {resource.hours:g} h"
                    f"{_resource_suffix(resource)}{asset_suffix}{responsible_suffix}"
                )
    return "\n".join(lines)


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
    body = _project_body(project, change_kind=change_kind)
    fingerprint = _fingerprint_payload(
        _draft_fingerprint_payload(
            message_key=message_key,
            to_recipients=to_recipients,
            cc_recipients=cc_recipients,
            subject=subject,
            body=body,
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
    if current.request_provenance_version != 1 or not current.request_provenance_complete:
        raise ValueError("Projection courante sans provenance chantier complète")
    _validate_provenance(current)
    if previous.request_provenance_version not in (None, 1):
        raise ValueError("Version historique de provenance chantier non supportée")
    if previous.request_provenance_version == 1:
        _validate_provenance(previous)
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
        elif previous.request_provenance_version is None:
            if (_project_payload(old) != _project_payload(new)
                or _has_site_contact(new)):
                changes.append(ProjectCommunicationChange(project_id, "modified", old, new))
        elif _project_semantic_payload(old, include_provenance=True) != _project_semantic_payload(new, include_provenance=True):
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
    return json.dumps(
        convert(asdict(projection)),
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
    elif provenance_version != 1 or provenance_complete is not True:
        raise ValueError("Version ou couverture de provenance chantier invalide")

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
