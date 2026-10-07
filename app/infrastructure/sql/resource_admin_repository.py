from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, select, true
from sqlalchemy.orm import Session

from ...application.query_models import ResourceAvailabilityRuleReadModel, ResourceReadModel
from ...application.resource_admin import ResourceAdminRepositoryPort
from .base import new_id
from .models import (
    AvailabilityRuleResourceClass,
    PlanningResourceUserOrder,
    Resource,
    ResourceAvailabilityRule,
    ResourceCompetency,
)
from .resource_class_models import ResourceClassConfig


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    text = _text(value)
    return text or None


def _resource_model(row: Resource, competency_ids: tuple[str, ...] = ()) -> ResourceReadModel:
    return ResourceReadModel(
        id=row.id,
        name=row.name,
        email=_optional_text(row.email),
        resource_class=_optional_text(row.resource_class),
        competencies=_optional_text(row.competencies),
        competency_ids=competency_ids,
        note=_optional_text(row.note),
        active=bool(row.active),
        sort_order=int(row.sort_order or 0),
        external_id=_optional_text(row.external_id),
        erp_status=_optional_text(row.erp_status),
        erp_active=bool(row.erp_active),
        erp_department_description=_optional_text(row.erp_department_description),
        erp_department_code=_optional_text(row.erp_department_code),
        erp_employee_class=_optional_text(row.erp_employee_class),
        erp_supervisor_external_id=_optional_text(row.erp_supervisor_external_id),
        erp_phone=_optional_text(row.erp_phone),
        erp_branch_code=_optional_text(row.erp_branch_code),
        erp_contact_id=row.erp_contact_id,
    )


def _rule_model(
    row: ResourceAvailabilityRule,
    resource: Resource | None = None,
    resource_class_codes: tuple[str, ...] = (),
) -> ResourceAvailabilityRuleReadModel:
    return ResourceAvailabilityRuleReadModel(
        id=row.id,
        availability_type=row.availability_type,
        resource_id=row.resource_id,
        resource_name=resource.name if resource is not None else None,
        resource_class_codes=resource_class_codes,
        start_date=row.start_date,
        end_date=row.end_date,
        weekdays=_optional_text(row.weekdays),
        start_time=row.start_time,
        end_time=row.end_time,
        note=_optional_text(row.note),
        active=bool(row.active),
    )


class SqlResourceAdminRepository(ResourceAdminRepositoryPort):
    def __init__(self, session: Session) -> None:
        self._session = session

    def _resource_class_codes_by_rule(
        self,
        rule_ids: Sequence[str],
    ) -> dict[str, tuple[str, ...]]:
        identifiers = tuple(str(value) for value in rule_ids if str(value))
        if not identifiers:
            return {}
        grouped: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
        rows = self._session.execute(
            select(
                AvailabilityRuleResourceClass.availability_rule_id,
                AvailabilityRuleResourceClass.resource_class_code,
            )
            .where(AvailabilityRuleResourceClass.availability_rule_id.in_(identifiers))
            .order_by(
                AvailabilityRuleResourceClass.availability_rule_id,
                AvailabilityRuleResourceClass.resource_class_code,
            )
        ).all()
        for rule_id, code in rows:
            grouped.setdefault(rule_id, []).append(code)
        return {rule_id: tuple(codes) for rule_id, codes in grouped.items()}

    def _replace_resource_class_codes(
        self,
        rule_id: str,
        codes: Sequence[str],
    ) -> None:
        self._session.execute(
            delete(AvailabilityRuleResourceClass).where(
                AvailabilityRuleResourceClass.availability_rule_id == rule_id
            )
        )
        self._session.add_all(
            AvailabilityRuleResourceClass(
                availability_rule_id=rule_id,
                resource_class_code=str(code),
            )
            for code in codes
        )

    def resource_class_states(self, codes: Sequence[str]) -> Mapping[str, bool]:
        identifiers = tuple(dict.fromkeys(_text(value) for value in codes if _text(value)))
        if not identifiers:
            return {}
        rows = self._session.execute(
            select(ResourceClassConfig.code, ResourceClassConfig.active).where(
                ResourceClassConfig.code.in_(identifiers)
            )
        ).all()
        return {str(code): bool(active) for code, active in rows}

    def _competency_ids_by_resource(
        self,
        resource_ids: Sequence[str],
    ) -> dict[str, tuple[str, ...]]:
        identifiers = tuple(str(value) for value in resource_ids if str(value))
        if not identifiers:
            return {}
        grouped: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
        rows = self._session.execute(
            select(
                ResourceCompetency.resource_id,
                ResourceCompetency.competency_id,
            )
            .where(ResourceCompetency.resource_id.in_(identifiers))
            .order_by(
                ResourceCompetency.resource_id,
                ResourceCompetency.competency_id,
            )
        ).all()
        for resource_id, competency_id in rows:
            grouped.setdefault(resource_id, []).append(competency_id)
        return {
            resource_id: tuple(competency_ids)
            for resource_id, competency_ids in grouped.items()
        }

    def list_resources(self, *, active_only: bool = False) -> tuple[ResourceReadModel, ...]:
        statement = select(Resource)
        if active_only:
            statement = statement.where(
                Resource.active == true(),
                Resource.erp_active == true(),
            )
        rows = self._session.scalars(statement.order_by(Resource.sort_order, Resource.name)).all()
        competency_ids = self._competency_ids_by_resource(tuple(row.id for row in rows))
        return tuple(
            _resource_model(row, competency_ids.get(row.id, ()))
            for row in rows
        )

    def get_resource(self, resource_id: str) -> ResourceReadModel | None:
        row = self._session.get(Resource, _text(resource_id))
        if row is None:
            return None
        competency_ids = self._competency_ids_by_resource((row.id,))
        return _resource_model(row, competency_ids.get(row.id, ()))

    def find_resource_by_name(self, name: str) -> ResourceReadModel | None:
        wanted = _text(name).casefold()
        if not wanted:
            return None
        rows = self._session.scalars(select(Resource).order_by(Resource.name)).all()
        row = next((item for item in rows if _text(item.name).casefold() == wanted), None)
        return _resource_model(row) if row is not None else None

    def create_resource(self, values: Mapping[str, Any]) -> str:
        row = Resource(
            id=new_id(),
            name=_text(values.get("name")),
            email=_optional_text(values.get("email")),
            resource_class=_optional_text(values.get("resource_class")),
            competencies=_optional_text(values.get("competencies")),
            note=_optional_text(values.get("note")),
            active=bool(values.get("active", True)),
            sort_order=int(values.get("sort_order") or 0),
            external_id=_optional_text(values.get("external_id")),
        )
        self._session.add(row)
        self._session.flush()
        return row.id

    def update_resource(self, resource_id: str, values: Mapping[str, Any]) -> str:
        row = self._session.get(Resource, _text(resource_id))
        if row is None:
            raise KeyError(f"Ressource {resource_id} introuvable")
        for field in (
            "name",
            "email",
            "resource_class",
            "competencies",
            "note",
            "active",
            "sort_order",
            "external_id",
        ):
            if field not in values:
                continue
            value = values[field]
            if field == "name":
                value = _text(value)
            elif field in {"email", "resource_class", "competencies", "note", "external_id"}:
                value = _optional_text(value)
            elif field == "active":
                value = bool(value)
            elif field == "sort_order":
                value = int(value or 0)
            setattr(row, field, value)
        self._session.flush()
        return row.id

    def replace_resource_sort_order(self, ordered_resource_ids: Sequence[str]) -> None:
        identifiers = tuple(_text(value) for value in ordered_resource_ids if _text(value))
        if not identifiers:
            return
        rows = {
            row.id: row
            for row in self._session.scalars(
                select(Resource).where(Resource.id.in_(identifiers))
            ).all()
        }
        missing = tuple(identifier for identifier in identifiers if identifier not in rows)
        if missing:
            raise KeyError(f"Ressources introuvables pour réordonnancement: {', '.join(missing)}")
        for position, identifier in enumerate(identifiers, start=1):
            rows[identifier].sort_order = position * 10
        self._session.flush()

    def resource_user_sort_positions(
        self,
        user_id: str,
        resource_ids: Sequence[str],
    ) -> Mapping[str, int]:
        owner = _text(user_id)
        identifiers = tuple(
            dict.fromkeys(_text(value) for value in resource_ids if _text(value))
        )
        if not owner or not identifiers:
            return {}
        rows = self._session.execute(
            select(
                PlanningResourceUserOrder.resource_id,
                PlanningResourceUserOrder.position,
            ).where(
                PlanningResourceUserOrder.user_id == owner,
                PlanningResourceUserOrder.resource_id.in_(identifiers),
            )
        ).all()
        return {str(resource_id): int(position) for resource_id, position in rows}

    def replace_user_resource_sort_order(
        self,
        user_id: str,
        ordered_resource_ids: Sequence[str],
    ) -> None:
        owner = _text(user_id)
        identifiers = tuple(
            dict.fromkeys(_text(value) for value in ordered_resource_ids if _text(value))
        )
        if not owner or not identifiers:
            return
        resource_ids = set(
            self._session.scalars(
                select(Resource.id).where(Resource.id.in_(identifiers))
            ).all()
        )
        missing = tuple(identifier for identifier in identifiers if identifier not in resource_ids)
        if missing:
            raise KeyError(
                f"Ressources introuvables pour réordonnancement personnel: {', '.join(missing)}"
            )
        existing = {
            row.resource_id: row
            for row in self._session.scalars(
                select(PlanningResourceUserOrder).where(
                    PlanningResourceUserOrder.user_id == owner,
                    PlanningResourceUserOrder.resource_id.in_(identifiers),
                )
            ).all()
        }
        for position, identifier in enumerate(identifiers, start=1):
            row = existing.get(identifier)
            if row is None:
                row = PlanningResourceUserOrder(
                    user_id=owner,
                    resource_id=identifier,
                    position=position * 10,
                )
                self._session.add(row)
            else:
                row.position = position * 10
        self._session.flush()

    def list_availability_rules(
        self,
        *,
        resource_id: str | None = None,
        include_global: bool = True,
        active_only: bool = False,
    ) -> tuple[ResourceAvailabilityRuleReadModel, ...]:
        statement = (
            select(ResourceAvailabilityRule, Resource)
            .outerjoin(Resource, ResourceAvailabilityRule.resource_id == Resource.id)
            .order_by(
                ResourceAvailabilityRule.start_date,
                ResourceAvailabilityRule.availability_type,
                ResourceAvailabilityRule.id,
            )
        )
        if active_only:
            statement = statement.where(ResourceAvailabilityRule.active == true())
        wanted = _text(resource_id)
        if wanted:
            if include_global:
                statement = statement.where(
                    (ResourceAvailabilityRule.resource_id == wanted)
                    | (ResourceAvailabilityRule.resource_id.is_(None))
                )
            else:
                statement = statement.where(ResourceAvailabilityRule.resource_id == wanted)
        elif not include_global:
            statement = statement.where(ResourceAvailabilityRule.resource_id.is_not(None))
        rows = self._session.execute(statement).all()
        class_codes = self._resource_class_codes_by_rule(tuple(rule.id for rule, _ in rows))
        return tuple(
            _rule_model(rule, resource, class_codes.get(rule.id, ()))
            for rule, resource in rows
        )

    def get_availability_rule(self, rule_id: str) -> ResourceAvailabilityRuleReadModel | None:
        row = self._session.execute(
            select(ResourceAvailabilityRule, Resource)
            .outerjoin(Resource, ResourceAvailabilityRule.resource_id == Resource.id)
            .where(ResourceAvailabilityRule.id == _text(rule_id))
        ).first()
        if row is None:
            return None
        rule, resource = row
        class_codes = self._resource_class_codes_by_rule((rule.id,))
        return _rule_model(rule, resource, class_codes.get(rule.id, ()))

    def create_availability_rule(self, values: Mapping[str, Any]) -> str:
        row = ResourceAvailabilityRule(
            id=new_id(),
            resource_id=_optional_text(values.get("resource_id")),
            availability_type=_text(values.get("availability_type")),
            start_date=values.get("start_date"),
            end_date=values.get("end_date"),
            weekdays=_optional_text(values.get("weekdays")),
            start_time=values.get("start_time"),
            end_time=values.get("end_time"),
            note=_optional_text(values.get("note")),
            active=bool(values.get("active", True)),
        )
        self._session.add(row)
        self._session.flush()
        self._replace_resource_class_codes(
            row.id,
            tuple(values.get("resource_class_codes") or ()),
        )
        self._session.flush()
        return row.id

    def update_availability_rule(self, rule_id: str, values: Mapping[str, Any]) -> str:
        row = self._session.get(ResourceAvailabilityRule, _text(rule_id))
        if row is None:
            raise KeyError(f"Règle de disponibilité {rule_id} introuvable")
        for field in (
            "resource_id",
            "availability_type",
            "start_date",
            "end_date",
            "weekdays",
            "start_time",
            "end_time",
            "note",
            "active",
        ):
            if field not in values:
                continue
            value = values[field]
            if field == "resource_id":
                value = _optional_text(value)
            elif field == "availability_type":
                value = _text(value)
            elif field in {"weekdays", "note"}:
                value = _optional_text(value)
            elif field == "active":
                value = bool(value)
            setattr(row, field, value)
        if "resource_class_codes" in values:
            self._replace_resource_class_codes(
                row.id,
                tuple(values.get("resource_class_codes") or ()),
            )
        self._session.flush()
        return row.id
