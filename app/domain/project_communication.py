from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from .operational_contacts import ContactResolution


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
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectCommunicationResponsibilityAssignment:
    shift_id: str
    resource_id: str
    resource_name: str
    operational_responsible: ContactResolution


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
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectCommunicationProjection:
    week_start: date
    week_end: date
    projects: tuple[ProjectCommunicationProject, ...]
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
        project = projects.setdefault(
            row.project_id,
            {
                "number": row.project_number,
                "name": row.project_name,
                "manager": row.project_manager,
                "days": {},
                "diagnostics": [],
            },
        )
        project_diagnostics = project["diagnostics"]
        assert isinstance(project_diagnostics, list)
        project_diagnostics.extend(row.project_manager.diagnostics)
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
                "diagnostics": [],
            },
        )
        if row.task_id:
            task["task_ids"].add(row.task_id)
        if row.task_code:
            task["task_codes"].add(row.task_code)
        task["responsibles"][_resolution_key(row.operational_responsible)] = row.operational_responsible
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
                "diagnostics": [],
            }
            resources[row.resource_id] = resource
        resource["hours"] = round(float(resource["hours"]) + float(row.hours), 2)
        resource["shift_ids"].append(row.shift_id)
        resource["allocation_types"].append(row.allocation_type)
        resource["confirmations"].append(row.confirmation)
        resource["outside_schedule"] = bool(resource["outside_schedule"]) or bool(row.outside_schedule)
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
        diagnostics=_unique(global_diagnostics),
    )
