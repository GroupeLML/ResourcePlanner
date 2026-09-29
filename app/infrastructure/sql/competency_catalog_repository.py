from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, select, true
from sqlalchemy.orm import Session

from ...application.competency_catalog import (
    CompetencyCatalogRepositoryPort,
    CompetencyReadModel,
)
from .base import new_id
from .models import (
    Competency,
    RequestLine,
    RequestLineCompetency,
    Resource,
    ResourceCompetency,
    ResourceRequirement,
    ResourceRequirementCompetency,
    WorkforceRequest,
    WorkforceRequestCompetency,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


def _model(row: Competency) -> CompetencyReadModel:
    return CompetencyReadModel(
        id=row.id,
        name=row.name,
        description=_optional_text(row.description),
        active=bool(row.active),
        sort_order=int(row.sort_order or 0),
    )


class SqlCompetencyCatalogRepository(CompetencyCatalogRepositoryPort):
    """SQL implementation of the local competency master-data boundary."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_competencies(
        self,
        *,
        query: str | None = None,
        active_only: bool = True,
        limit: int = 200,
    ) -> tuple[CompetencyReadModel, ...]:
        statement = select(Competency)
        if active_only:
            statement = statement.where(Competency.active == true())
        rows = self._session.scalars(
            statement.order_by(Competency.sort_order, Competency.name, Competency.id)
        ).all()
        wanted = _text(query).casefold()
        if wanted:
            rows = [
                row
                for row in rows
                if wanted
                in f"{_text(row.name)} {_text(row.description)}".casefold()
            ]
        return tuple(_model(row) for row in rows[: max(int(limit), 1)])

    def get_competency(self, competency_id: str) -> CompetencyReadModel | None:
        row = self._session.get(Competency, _text(competency_id))
        return _model(row) if row is not None else None

    def find_by_name(self, name: str) -> CompetencyReadModel | None:
        wanted = _text(name).casefold()
        if not wanted:
            return None
        rows = self._session.scalars(
            select(Competency).order_by(Competency.sort_order, Competency.name)
        ).all()
        row = next((item for item in rows if _text(item.name).casefold() == wanted), None)
        return _model(row) if row is not None else None

    def create_competency(self, values: Mapping[str, Any]) -> str:
        row = Competency(
            id=new_id(),
            name=_text(values.get("name")),
            description=_optional_text(values.get("description")),
            active=bool(values.get("active", True)),
            sort_order=int(values.get("sort_order") or 0),
        )
        self._session.add(row)
        self._session.flush()
        return row.id

    def update_competency(self, competency_id: str, values: Mapping[str, Any]) -> str:
        row = self._session.get(Competency, _text(competency_id))
        if row is None:
            raise KeyError(f"Compétence {competency_id} introuvable")
        renamed = "name" in values and _text(values.get("name")) != row.name
        if "name" in values:
            row.name = _text(values.get("name"))
        if "description" in values:
            row.description = _optional_text(values.get("description"))
        if "active" in values:
            row.active = bool(values.get("active"))
        if "sort_order" in values:
            row.sort_order = int(values.get("sort_order") or 0)
        self._session.flush()
        if renamed:
            self._synchronize_linked_snapshots(row.id)
        return row.id

    def _ordered_competencies(self, competency_ids: Sequence[str]) -> tuple[Competency, ...]:
        result: list[Competency] = []
        for competency_id in competency_ids:
            row = self._session.get(Competency, _text(competency_id))
            if row is None:
                raise KeyError(f"Compétence {competency_id} introuvable")
            result.append(row)
        return tuple(result)

    @staticmethod
    def _snapshot(rows: Sequence[Competency]) -> str | None:
        text = "; ".join(row.name for row in rows)
        return text or None

    def _resource_competencies(self, resource_id: str) -> tuple[Competency, ...]:
        rows = self._session.scalars(
            select(Competency)
            .join(ResourceCompetency, ResourceCompetency.competency_id == Competency.id)
            .where(ResourceCompetency.resource_id == resource_id)
            .order_by(Competency.sort_order, Competency.name, Competency.id)
        ).all()
        return tuple(rows)

    def _request_competencies(self, request_id: str) -> tuple[Competency, ...]:
        rows = self._session.scalars(
            select(Competency)
            .join(
                WorkforceRequestCompetency,
                WorkforceRequestCompetency.competency_id == Competency.id,
            )
            .where(WorkforceRequestCompetency.workforce_request_id == request_id)
            .order_by(Competency.sort_order, Competency.name, Competency.id)
        ).all()
        return tuple(rows)

    def set_resource_competencies(
        self,
        resource_id: str,
        competency_ids: Sequence[str],
    ) -> None:
        identifier = _text(resource_id)
        resource = self._session.get(Resource, identifier)
        if resource is None:
            raise KeyError(f"Ressource {identifier} introuvable")
        rows = self._ordered_competencies(competency_ids)
        self._session.execute(
            delete(ResourceCompetency).where(ResourceCompetency.resource_id == identifier)
        )
        self._session.add_all(
            [
                ResourceCompetency(resource_id=identifier, competency_id=row.id)
                for row in rows
            ]
        )
        resource.competencies = self._snapshot(rows)
        self._session.flush()

    def set_demand_competencies(
        self,
        demand_number: str,
        competency_ids: Sequence[str],
    ) -> None:
        wanted = _text(demand_number)
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        )
        if request is None:
            raise KeyError(f"Demande {wanted} introuvable")
        rows = self._ordered_competencies(competency_ids)
        self._session.execute(
            delete(WorkforceRequestCompetency).where(
                WorkforceRequestCompetency.workforce_request_id == request.id
            )
        )
        self._session.add_all(
            [
                WorkforceRequestCompetency(
                    workforce_request_id=request.id,
                    competency_id=row.id,
                )
                for row in rows
            ]
        )
        request.required_competencies = self._snapshot(rows)
        line = self._session.get(RequestLine, request.id)
        if line is not None:
            self._session.execute(
                delete(RequestLineCompetency).where(
                    RequestLineCompetency.request_line_id == line.id
                )
            )
            self._session.add_all(
                [
                    RequestLineCompetency(
                        request_line_id=line.id,
                        competency_id=row.id,
                    )
                    for row in rows
                ]
            )
            line.required_competencies_snapshot = request.required_competencies
        self._session.flush()

    def set_segment_competency(
        self,
        segment_id: str,
        competency_id: str | None,
    ) -> None:
        wanted = _text(segment_id)
        requirement = self._session.scalar(
            select(ResourceRequirement).where(
                (ResourceRequirement.legacy_segment_id == wanted)
                | (ResourceRequirement.id == wanted)
            )
        )
        if requirement is None:
            raise KeyError(f"Segment {wanted} introuvable")
        normalized = _text(competency_id)
        if not normalized:
            requirement.required_competency_id = None
            requirement.required_competency = None
        else:
            row = self._session.get(Competency, normalized)
            if row is None:
                raise KeyError(f"Compétence {normalized} introuvable")
            requirement.required_competency_id = row.id
            requirement.required_competency = row.name
        self._session.execute(
            delete(ResourceRequirementCompetency).where(
                ResourceRequirementCompetency.resource_requirement_id == requirement.id
            )
        )
        if requirement.required_competency_id is not None:
            self._session.add(
                ResourceRequirementCompetency(
                    resource_requirement_id=requirement.id,
                    competency_id=requirement.required_competency_id,
                )
            )
        self._session.flush()

    def _synchronize_linked_snapshots(self, competency_id: str) -> None:
        resource_ids = self._session.scalars(
            select(ResourceCompetency.resource_id).where(
                ResourceCompetency.competency_id == competency_id
            )
        ).all()
        for resource_id in resource_ids:
            resource = self._session.get(Resource, resource_id)
            if resource is not None:
                resource.competencies = self._snapshot(
                    self._resource_competencies(resource_id)
                )

        request_ids = self._session.scalars(
            select(WorkforceRequestCompetency.workforce_request_id).where(
                WorkforceRequestCompetency.competency_id == competency_id
            )
        ).all()
        for request_id in request_ids:
            request = self._session.get(WorkforceRequest, request_id)
            if request is not None:
                request.required_competencies = self._snapshot(
                    self._request_competencies(request_id)
                )
                line = self._session.get(RequestLine, request_id)
                if line is not None:
                    line.required_competencies_snapshot = request.required_competencies

        competency = self._session.get(Competency, competency_id)
        if competency is not None:
            requirements = self._session.scalars(
                select(ResourceRequirement).where(
                    ResourceRequirement.required_competency_id == competency_id
                )
            ).all()
            for requirement in requirements:
                requirement.required_competency = competency.name
        self._session.flush()
