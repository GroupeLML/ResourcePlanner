from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from typing import Sequence

from .operational_contacts import ContactResolution, STATUS_RESOLVED
from .project_communication import (
    ProjectCommunicationDay,
    ProjectCommunicationParticipant,
    ProjectCommunicationProject,
    ProjectCommunicationProjection,
    ProjectCommunicationResource,
    ProjectCommunicationTask,
)


MESSAGE_KIND_WEEKLY_CONFIRMATION = "project_confirmation"
MESSAGE_KIND_PLANNING_CHANGE = "project_planning_change"

DIAGNOSTIC_TO_EMAIL_MISSING = "PROJECT_TO_EMAIL_MISSING"
DIAGNOSTIC_TO_INACTIVE = "PROJECT_TO_INACTIVE"
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
                    tuple(resource.diagnostics),
                )
                for resource in task.resources
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
        tuple(days),
        tuple(project.diagnostics),
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
    return _fingerprint_payload(
        (
            projection.week_start.isoformat(),
            projection.week_end.isoformat(),
            tuple(_project_payload(project) for project in projection.projects),
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


def _cc_recipients(
    projects: Sequence[ProjectCommunicationProject],
    *,
    exclude_email: str | None = None,
) -> tuple[ProjectCommunicationParticipant, ...]:
    by_email: dict[str, ProjectCommunicationParticipant] = {}
    for participant in _resource_participants(projects).values():
        email = (participant.email or "").strip()
        if not participant.active or not email:
            continue
        if exclude_email and email.casefold() == exclude_email.casefold():
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
        lines.append("Responsables opérationnels :")
        lines.extend(f"  • {_format_responsible(value)}" for value in resolved)
    else:
        lines.append("Responsable : Non résolu")
    return lines


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
            lines.extend(("", f"{french_long_date(day.day)} — {task.task_description}"))
            if multiple_responsibles:
                resolved = tuple(
                    value
                    for value in task.operational_responsibles
                    if value.status == STATUS_RESOLVED
                )
                if resolved:
                    lines.append(
                        "  Responsable : "
                        + " / ".join(_format_responsible(value) for value in resolved)
                    )
            for resource in task.resources:
                lines.append(
                    f"  • {resource.resource_name} — {resource.hours:g} h"
                    f"{_resource_suffix(resource)}"
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
    to_recipient: ProjectCommunicationParticipant,
    cc_recipients: Sequence[ProjectCommunicationParticipant],
    subject: str,
    body: str,
    message_kind: str,
    diagnostics: Sequence[ProjectMessageDiagnostic],
    source_projects: Sequence[ProjectCommunicationProject],
) -> tuple[object, ...]:
    return (
        message_key,
        _participant_payload(to_recipient),
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
    cc_recipients = _cc_recipients(
        cc_sources,
        exclude_email=project.project_manager.email,
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
            to_recipient=project.project_manager,
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
        elif _project_payload(old) != _project_payload(new):
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

    return json.dumps(
        convert(asdict(projection)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def deserialize_project_projection(payload: str) -> ProjectCommunicationProjection:
    """Restore a projection snapshot without consulting mutable business tables."""

    data = json.loads(payload)

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
                        diagnostics=tuple(
                            resource_row.get("diagnostics") or ()
                        ),
                    )
                    for resource_row in task_row.get("resources") or ()
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
                diagnostics=tuple(project_row.get("diagnostics") or ()),
            )
        )
    return ProjectCommunicationProjection(
        week_start=date.fromisoformat(str(data["week_start"])),
        week_end=date.fromisoformat(str(data["week_end"])),
        projects=tuple(projects),
        diagnostics=tuple(data.get("diagnostics") or ()),
    )
