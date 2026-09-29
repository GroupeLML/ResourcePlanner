from __future__ import annotations

from typing import Any

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application.repository_ports import PlanningReadRepositoryPort
from ...domain.planning_snapshot import PlanningSnapshot
from .models import (
    Project,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _identifier(legacy: object, internal: object) -> str:
    return _text(legacy) or _text(internal)


def _number(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


class SqlPlanningReadRepository(PlanningReadRepositoryPort):
    """Capture one coherent planning snapshot from the relational SQL model.

    The repository intentionally projects SQL entities into the compatibility row
    vocabulary consumed by ``project_planning_snapshot``. That vocabulary remains an
    infrastructure detail; the pure engine continues to receive typed inputs and does
    not know whether the source was Excel or SQL.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _segments(self) -> list[dict[str, Any]]:
        rows = self._session.execute(
            select(ResourceRequirement, Project, WorkforceRequest, Resource)
            .join(Project, ResourceRequirement.project_id == Project.id)
            .outerjoin(
                WorkforceRequest,
                ResourceRequirement.workforce_request_id == WorkforceRequest.id,
            )
            .outerjoin(Resource, ResourceRequirement.assigned_resource_id == Resource.id)
            .order_by(ResourceRequirement.start_date, ResourceRequirement.id)
        ).all()
        return [
            {
                "IDSegment": _identifier(requirement.legacy_segment_id, requirement.id),
                "NoDemande": (
                    _identifier(request.legacy_demand_number, request.id)
                    if request is not None
                    else None
                ),
                "NumeroProjet": project.number,
                "NomProjet": project.name,
                "Technicien": resource.name if resource is not None else None,
                "DateDebut": requirement.start_date,
                "DateFin": requirement.end_date,
                "HeuresPrevues": float(requirement.planned_hours),
                "JoursActifsCibles": requirement.desired_active_days,
                "ProfilCharge": requirement.load_profile,
                "Statut": requirement.status,
                "Description": requirement.description,
                "SourceEffortID": requirement.source_effort_id,
                "DateCreation": requirement.created_at,
                "DateModification": requirement.updated_at,
                "CompetenceRequise": requirement.required_competency,
                "ClasseRessourceRequise": requirement.required_resource_class,
                "TypePlanification": requirement.planning_type,
                "Priorite": requirement.priority,
                "HorsHoraireAutorise": requirement.outside_standard_hours_allowed,
                "OrigineSegment": requirement.origin,
            }
            for requirement, project, request, resource in rows
        ]

    def _demands(self) -> list[dict[str, Any]]:
        rows = self._session.execute(
            select(WorkforceRequest, Project, Resource)
            .join(Project, WorkforceRequest.project_id == Project.id)
            .outerjoin(Resource, WorkforceRequest.proposed_resource_id == Resource.id)
            .order_by(WorkforceRequest.desired_start, WorkforceRequest.id)
        ).all()
        return [
            {
                "NoDemande": _identifier(request.legacy_demand_number, request.id),
                "NumeroProjet": project.number,
                "NomProjet": project.name,
                "Client": project.client,
                "Demandeur": request.requester_name,
                "ChargeProjet": project.project_manager_name,
                "TypeDemande": request.request_type,
                "Priorite": request.priority,
                "Confirmation": request.confirmation,
                "DateDebutSouhaitee": request.desired_start,
                "DateFinSouhaitee": request.desired_end,
                "Description": request.description,
                "Statut": request.status,
                "SiteClient": request.site_client,
                "Lieu": request.location,
                "NombreRessources": request.resource_count,
                "CompetencesRequises": request.required_competencies,
                "TempsEstimeHeures": _number(request.estimated_hours),
                "TempsEstimeJours": _number(request.estimated_days),
                "TechnicienPropose": resource.name if resource is not None else None,
                "TaskCode": request.erp_task_code,
                "TaskLabel": request.erp_task_label,
                "DateCreation": request.created_at,
                "DateModification": request.updated_at,
                "ApprouvePar": request.approved_by_name,
                "DateApprobation": request.approved_at,
                "CommentaireApprobation": request.approval_comment,
            }
            for request, project, resource in rows
        ]

    def _allocations(self) -> list[dict[str, Any]]:
        rows = self._session.execute(
            select(Shift, ResourceRequirement, Resource)
            .join(
                ResourceRequirement,
                Shift.resource_requirement_id == ResourceRequirement.id,
            )
            .join(Resource, Shift.resource_id == Resource.id)
            .order_by(Shift.work_date, Shift.id)
        ).all()
        return [
            {
                "IDAllocation": _identifier(shift.legacy_allocation_id, shift.id),
                "IDSegment": _identifier(
                    requirement.legacy_segment_id,
                    requirement.id,
                ),
                "Technicien": resource.name,
                "Date": shift.work_date,
                "Heures": float(shift.hours),
                "TypeAllocation": shift.allocation_type,
                "Verrouillee": shift.locked,
                "HorsHoraire": shift.outside_standard_hours,
                "Confirmation": shift.confirmation,
                "Note": shift.note,
            }
            for shift, requirement, resource in rows
        ]

    def _availability(self) -> list[dict[str, Any]]:
        rows = self._session.execute(
            select(ResourceAvailabilityRule, Resource)
            .outerjoin(Resource, ResourceAvailabilityRule.resource_id == Resource.id)
            .order_by(
                ResourceAvailabilityRule.start_date,
                ResourceAvailabilityRule.id,
            )
        ).all()
        return [
            {
                "ID": _identifier(rule.legacy_id, rule.id),
                "Technicien": resource.name if resource is not None else "",
                "Type": rule.availability_type,
                "DateDebut": rule.start_date,
                "DateFin": rule.end_date,
                "JoursSemaine": rule.weekdays,
                "HeureDebut": rule.start_time,
                "HeureFin": rule.end_time,
                "Note": rule.note,
                "Actif": rule.active,
            }
            for rule, resource in rows
        ]

    def _technicians(self) -> list[dict[str, Any]]:
        resources = self._session.scalars(
            select(Resource)
            .where(
                Resource.active == true(),
                Resource.erp_active == true(),
            )
            .order_by(Resource.sort_order, Resource.name)
        ).all()
        return [
            {
                "name": resource.name,
                "description": "",
                "team": resource.resource_class or "",
                "capacity": 0.0,
            }
            for resource in resources
        ]

    def capture(self) -> PlanningSnapshot:
        return PlanningSnapshot.capture(
            segments=self._segments(),
            demands=self._demands(),
            allocations=self._allocations(),
            availability=self._availability(),
            technicians=self._technicians(),
        )
