from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, select, true, update
from sqlalchemy.orm import Session

from ...application.competency_catalog import (
    CompetencyCatalogRepositoryPort,
    CompetencyReadModel,
    CompetencyResourceClassMutationResult,
    CompetencyResourceClassReadModel,
)
from ...application.errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
)
from .base import new_id
from .models import (
    Competency,
    CompetencyResourceClassAudit,
    RequestLine,
    RequestLineCompetency,
    Resource,
    ResourceCompetency,
    ResourceRequirement,
    ResourceRequirementCompetency,
    WorkforceRequest,
    WorkforceRequestCompetency,
)
from .resource_class_models import ResourceClassConfig


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


def _model(
    row: Competency,
    resource_class: ResourceClassConfig | None = None,
) -> CompetencyReadModel:
    return CompetencyReadModel(
        id=row.id,
        name=row.name,
        description=_optional_text(row.description),
        active=bool(row.active),
        sort_order=int(row.sort_order or 0),
        resource_class_code=_optional_text(row.resource_class_code),
        resource_class_label=(
            _optional_text(resource_class.label) if resource_class is not None else None
        ),
        resource_class_active=(
            bool(resource_class.active) if resource_class is not None else None
        ),
        resource_class_version=int(row.resource_class_version or 1),
    )


def _competency_with_class_statement():
    return select(Competency, ResourceClassConfig).outerjoin(
        ResourceClassConfig,
        Competency.resource_class_code == ResourceClassConfig.code,
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
        statement = _competency_with_class_statement()
        if active_only:
            statement = statement.where(Competency.active == true())
        rows = self._session.execute(
            statement.order_by(Competency.sort_order, Competency.name, Competency.id)
        ).all()
        wanted = _text(query).casefold()
        if wanted:
            rows = [
                pair
                for pair in rows
                if wanted
                in f"{_text(pair[0].name)} {_text(pair[0].description)}".casefold()
            ]
        return tuple(
            _model(row, resource_class)
            for row, resource_class in rows[: max(int(limit), 1)]
        )

    def get_competency(self, competency_id: str) -> CompetencyReadModel | None:
        pair = self._session.execute(
            _competency_with_class_statement().where(
                Competency.id == _text(competency_id)
            )
        ).one_or_none()
        return _model(pair[0], pair[1]) if pair is not None else None

    def find_by_name(self, name: str) -> CompetencyReadModel | None:
        wanted = _text(name).casefold()
        if not wanted:
            return None
        rows = self._session.execute(
            _competency_with_class_statement().order_by(
                Competency.sort_order,
                Competency.name,
            )
        ).all()
        pair = next(
            (item for item in rows if _text(item[0].name).casefold() == wanted),
            None,
        )
        return _model(pair[0], pair[1]) if pair is not None else None

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

    def get_resource_class(
        self,
        code: str,
    ) -> CompetencyResourceClassReadModel | None:
        row = self._session.get(ResourceClassConfig, _text(code))
        if row is None:
            return None
        return CompetencyResourceClassReadModel(
            code=row.code,
            label=row.label,
            active=bool(row.active),
        )

    def set_competency_resource_class(
        self,
        competency_id: str,
        resource_class_code: str | None,
        *,
        actor_user_id: str,
        expected_version: int,
    ) -> CompetencyResourceClassMutationResult:
        competency_key = _text(competency_id)
        class_code = _optional_text(resource_class_code)
        with self._session.begin_nested():
            competency = self._session.get(Competency, competency_key)
            if competency is None:
                raise ApplicationNotFoundError(
                    f"Compétence {competency_key} introuvable.",
                    code="competency_not_found",
                    context={"competency_id": competency_key},
                )
            old_class_code = _optional_text(competency.resource_class_code)
            result = self._session.execute(
                update(Competency)
                .where(
                    Competency.id == competency_key,
                    Competency.resource_class_version == expected_version,
                )
                .values(
                    resource_class_code=class_code,
                    resource_class_version=Competency.resource_class_version + 1,
                )
            )
            if int(result.rowcount or 0) != 1:
                current = self._session.scalar(
                    select(Competency.resource_class_version).where(
                        Competency.id == competency_key
                    )
                )
                if current is None:
                    raise ApplicationNotFoundError(
                        f"Compétence {competency_key} introuvable.",
                        code="competency_not_found",
                        context={"competency_id": competency_key},
                    )
                raise ApplicationConflictError(
                    "Le rattachement de classe a été modifié depuis sa lecture.",
                    code="competency_resource_class_version_conflict",
                    context={
                        "competency_id": competency_key,
                        "expected_version": expected_version,
                        "current_version": int(current),
                    },
                )
            resulting_version = expected_version + 1
            action = (
                "COMPETENCY_RESOURCE_CLASS_CLEARED"
                if class_code is None
                else "COMPETENCY_RESOURCE_CLASS_SET"
            )
            self._session.add(
                CompetencyResourceClassAudit(
                    competency_id=competency_key,
                    actor_user_id=_text(actor_user_id),
                    action=action,
                    old_resource_class_code=old_class_code,
                    new_resource_class_code=class_code,
                    resulting_version=resulting_version,
                )
            )
            self._session.flush()
        return CompetencyResourceClassMutationResult(
            competency_id=competency_key,
            resource_class_code=class_code,
            version=resulting_version,
            action=action,
        )

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
