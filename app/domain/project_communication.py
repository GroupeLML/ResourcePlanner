from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from .operational_contacts import ContactResolution


@dataclass(frozen=True, slots=True)
class ProjectCommunicationAsset:
    allocation_id: str
    asset_id: str
    asset_code: str
    asset_label: str
    asset_type_label: str
    asset_category: str
    association_kind: str


@dataclass(frozen=True, slots=True)
class ProjectCommunicationParticipant:
    contact_id: str | None
    user_id: str | None
    display_name: str
    email: str | None
    phone: str | None
    active: bool
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectCommunicationAssignment:
    shift_id: str
    requirement_id: str
    project_id: str
    project_number: str
    project_name: str
    day: date
    hours: float
    allocation_type: str
    outside_schedule: bool
    confirmation: str
    task_id: str | None
    task_code: str | None
    task_description: str
    resource_id: str
    resource_name: str
    resource_contact: ProjectCommunicationParticipant
    project_manager: ProjectCommunicationParticipant
    operational_responsible: ContactResolution
    project_co_managers: tuple[ProjectCommunicationParticipant, ...] = ()
    requester: ProjectCommunicationParticipant | None = None
    workforce_request_id: str | None = None
    request_number: str | None = None
    site_contact_name: str | None = None
    site_contact_phone: str | None = None
    assets: tuple[ProjectCommunicationAsset, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectCommunicationResource:
    resource_id: str
    resource_name: str
    contact: ProjectCommunicationParticipant
    hours: float
    shift_ids: tuple[str, ...]
    allocation_types: tuple[str, ...]
    confirmations: tuple[str, ...]
    outside_schedule: bool
    assets: tuple[ProjectCommunicationAsset, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectCommunicationResponsibilityAssignment:
    shift_id: str
    resource_id: str
    resource_name: str
    operational_responsible: ContactResolution


@dataclass(frozen=True, slots=True)
class ProjectCommunicationRequestAssignment:
    shift_id: str
    requirement_id: str
    workforce_request_id: str | None
    request_number: str | None
    resource_id: str
    resource_name: str
    hours: float
    site_contact_name: str | None
    site_contact_phone: str | None


@dataclass(frozen=True, slots=True)
class ProjectCommunicationTask:
    task_description: str
    task_ids: tuple[str, ...]
    task_codes: tuple[str, ...]
    operational_responsibles: tuple[ContactResolution, ...]
    resources: tuple[ProjectCommunicationResource, ...]
    responsibility_assignments: tuple[
        ProjectCommunicationResponsibilityAssignment, ...
    ] = ()
    request_assignments: tuple[ProjectCommunicationRequestAssignment, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectCommunicationDay:
    day: date
    tasks: tuple[ProjectCommunicationTask, ...]


@dataclass(frozen=True, slots=True)
class ProjectCommunicationProject:
    project_id: str
    project_number: str
    project_name: str
    project_manager: ProjectCommunicationParticipant
    days: tuple[ProjectCommunicationDay, ...]
    project_co_managers: tuple[ProjectCommunicationParticipant, ...] = ()
    requesters: tuple[ProjectCommunicationParticipant, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectCommunicationProjection:
    week_start: date
    week_end: date
    projects: tuple[ProjectCommunicationProject, ...]
    request_provenance_version: int | None = None
    request_provenance_complete: bool = False
    diagnostics: tuple[str, ...] = ()


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _resolution_key(value: ContactResolution) -> tuple[object, ...]:
    return (
        value.status,
        value.contact_id,
        value.source_type,
        value.source_entity_id,
        value.display_name,
        value.email,
        value.phone,
    )


def build_project_communication_projection(
    *,
    week_start: date,
    week_end: date,
    assignments: Sequence[ProjectCommunicationAssignment],
) -> ProjectCommunicationProjection:
    """Group authoritative shifts as Project -> Date -> Task -> assigned resources."""

    projects: dict[str, dict[str, object]] = {}
    global_diagnostics: list[str] = []
    seen_shifts: set[str] = set()

    for row in sorted(
        assignments,
        key=lambda item: (
            item.project_number.casefold(),
            item.project_id,
            item.day,
            item.task_description.casefold(),
            item.resource_name.casefold(),
            item.shift_id,
        ),
    ):
        if not row.shift_id or row.shift_id in seen_shifts:
            raise ValueError("Identité Shift manquante ou dupliquée dans la communication projet")
        seen_shifts.add(row.shift_id)
        if row.workforce_request_id is None and (row.request_number or row.site_contact_name or row.site_contact_phone):
            raise ValueError("Contact chantier sans demande source")
        project = projects.setdefault(
            row.project_id,
            {
                "number": row.project_number,
                "name": row.project_name,
                "manager": row.project_manager,
                "co_managers": row.project_co_managers,
                "requesters": {},
                "days": {},
                "diagnostics": [],
            },
        )
        project_diagnostics = project["diagnostics"]
        assert isinstance(project_diagnostics, list)
        project_diagnostics.extend(row.project_manager.diagnostics)
        for co_manager in row.project_co_managers:
            project_diagnostics.extend(co_manager.diagnostics)
        if row.requester is not None:
            requester_key = (
                row.requester.contact_id or "",
                row.requester.user_id or "",
                (row.requester.email or "").strip().casefold(),
            )
            project["requesters"].setdefault(requester_key, row.requester)
            project_diagnostics.extend(row.requester.diagnostics)
        project_diagnostics.extend(row.diagnostics)
        global_diagnostics.extend(row.diagnostics)

        days = project["days"]
        assert isinstance(days, dict)
        day = days.setdefault(row.day, {})

        task_key = row.task_description.strip().casefold() or "travaux planifiés"
        task = day.setdefault(
            task_key,
            {
                "description": row.task_description,
                "task_ids": set(),
                "task_codes": set(),
                "responsibles": {},
                "resources": {},
                "responsibility_assignments": [],
                "request_assignments": [],
                "diagnostics": [],
            },
        )
        if row.task_id:
            task["task_ids"].add(row.task_id)
        if row.task_code:
            task["task_codes"].add(row.task_code)
        task["responsibles"][_resolution_key(row.operational_responsible)] = row.operational_responsible
        task["request_assignments"].append(
            ProjectCommunicationRequestAssignment(
                shift_id=row.shift_id,
                requirement_id=row.requirement_id,
                workforce_request_id=row.workforce_request_id,
                request_number=row.request_number,
                resource_id=row.resource_id,
                resource_name=row.resource_name,
                hours=row.hours,
                site_contact_name=row.site_contact_name,
                site_contact_phone=row.site_contact_phone,
            )
        )
        task["responsibility_assignments"].append(
            ProjectCommunicationResponsibilityAssignment(
                shift_id=row.shift_id,
                resource_id=row.resource_id,
                resource_name=row.resource_name,
                operational_responsible=row.operational_responsible,
            )
        )
        task["diagnostics"].extend(row.operational_responsible.diagnostics)
        task["diagnostics"].extend(row.diagnostics)

        resources = task["resources"]
        resource = resources.get(row.resource_id)
        if resource is None:
            resource = {
                "name": row.resource_name,
                "contact": row.resource_contact,
                "hours": 0.0,
                "shift_ids": [],
                "allocation_types": [],
                "confirmations": [],
                "outside_schedule": False,
                "assets": {},
                "diagnostics": [],
            }
            resources[row.resource_id] = resource
        resource["hours"] = round(float(resource["hours"]) + float(row.hours), 2)
        resource["shift_ids"].append(row.shift_id)
        resource["allocation_types"].append(row.allocation_type)
        resource["confirmations"].append(row.confirmation)
        resource["outside_schedule"] = bool(resource["outside_schedule"]) or bool(row.outside_schedule)
        for asset in row.assets:
            resource["assets"].setdefault(asset.allocation_id, asset)
        resource["diagnostics"].extend(row.resource_contact.diagnostics)
        resource["diagnostics"].extend(row.diagnostics)

    projected_projects: list[ProjectCommunicationProject] = []
    for project_id, project in projects.items():
        projected_days: list[ProjectCommunicationDay] = []
        days = project["days"]
        assert isinstance(days, dict)
        for day_value in sorted(days):
            projected_tasks: list[ProjectCommunicationTask] = []
            tasks = days[day_value]
            for task in sorted(tasks.values(), key=lambda value: str(value["description"]).casefold()):
                projected_resources: list[ProjectCommunicationResource] = []
                for resource_id, resource in sorted(
                    task["resources"].items(),
                    key=lambda item: (str(item[1]["name"]).casefold(), str(item[0])),
                ):
                    projected_resources.append(
                        ProjectCommunicationResource(
                            resource_id=str(resource_id),
                            resource_name=str(resource["name"]),
                            contact=resource["contact"],
                            hours=round(float(resource["hours"]), 2),
                            shift_ids=tuple(dict.fromkeys(resource["shift_ids"])),
                            allocation_types=_unique(resource["allocation_types"]),
                            confirmations=_unique(resource["confirmations"]),
                            outside_schedule=bool(resource["outside_schedule"]),
                            assets=tuple(
                                resource["assets"][key]
                                for key in sorted(
                                    resource["assets"],
                                    key=lambda value: (
                                        resource["assets"][value].asset_type_label.casefold(),
                                        resource["assets"][value].asset_label.casefold(),
                                        resource["assets"][value].asset_code.casefold(),
                                        value,
                                    ),
                                )
                            ),
                            diagnostics=_unique(resource["diagnostics"]),
                        )
                    )
                projected_tasks.append(
                    ProjectCommunicationTask(
                        task_description=str(task["description"]),
                        task_ids=tuple(sorted(task["task_ids"])),
                        task_codes=tuple(sorted(task["task_codes"])),
                        operational_responsibles=tuple(
                            task["responsibles"][key]
                            for key in sorted(
                                task["responsibles"],
                                key=lambda value: tuple("" if part is None else str(part) for part in value),
                            )
                        ),
                        resources=tuple(projected_resources),
                        responsibility_assignments=tuple(
                            sorted(
                                task["responsibility_assignments"],
                                key=lambda value: (
                                    value.resource_name.casefold(),
                                    value.resource_id,
                                    value.shift_id,
                                ),
                            )
                        ),
                        request_assignments=tuple(sorted(
                            task["request_assignments"],
                            key=lambda value: (
                                value.workforce_request_id or "",
                                value.resource_name.casefold(),
                                value.resource_id,
                                value.shift_id,
                            ),
                        )),
                        diagnostics=_unique(task["diagnostics"]),
                    )
                )
            projected_days.append(ProjectCommunicationDay(day=day_value, tasks=tuple(projected_tasks)))
        projected_projects.append(
            ProjectCommunicationProject(
                project_id=project_id,
                project_number=str(project["number"]),
                project_name=str(project["name"]),
                project_manager=project["manager"],
                days=tuple(projected_days),
                project_co_managers=tuple(project["co_managers"]),
                requesters=tuple(
                    project["requesters"][key]
                    for key in sorted(project["requesters"])
                ),
                diagnostics=_unique(project["diagnostics"]),
            )
        )

    projected_projects.sort(
        key=lambda row: (row.project_number.casefold(), row.project_name.casefold(), row.project_id)
    )
    return ProjectCommunicationProjection(
        week_start=week_start,
        week_end=week_end,
        projects=tuple(projected_projects),
        request_provenance_version=1,
        request_provenance_complete=True,
        diagnostics=_unique(global_diagnostics),
    )
