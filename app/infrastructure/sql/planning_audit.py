from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
import json
from typing import Any

from sqlalchemy import DateTime, Index, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from ...application.command_ports import AllocationCommandPort, ApprovedDemandSyncPort
from ...application.repository_ports import SegmentRepositoryPort
from ...application.read_models import SegmentReadModel
from .base import Base, new_id, utc_now
from ...domain.reservable_assets import AssetRequirementOrigin
from .asset_models import Asset, AssetAllocation, AssetRequirement
from .models import Project, Resource, ResourceRequirement, Shift, WorkforceRequest


ENTITY_SEGMENT = "SEGMENT"
ENTITY_SHIFT = "SHIFT"


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()  # type: ignore[union-attr]
        except (TypeError, ValueError):
            pass
    return value


def _details(before: Mapping[str, object] | None, after: Mapping[str, object] | None) -> str:
    before_map = {key: _json_value(value) for key, value in (before or {}).items()}
    after_map = {key: _json_value(value) for key, value in (after or {}).items()}
    if before is None:
        payload: dict[str, object] = {"after": after_map}
    elif after is None:
        payload = {"before": before_map}
    else:
        changes = {
            key: {"before": before_map.get(key), "after": after_map.get(key)}
            for key in sorted(set(before_map) | set(after_map))
            if before_map.get(key) != after_map.get(key)
        }
        payload = {"changes": changes}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PlanningChangeHistory(Base):
    """Durable business audit for explicit segment/manual-shift changes.

    Entity ids deliberately are not foreign keys: a deleted manual shift must keep a
    readable history after the operational row disappears.
    """

    __tablename__ = "planning_change_history"
    __table_args__ = (
        Index("ix_planning_change_history_entity_time", "entity_type", "entity_id", "occurred_at"),
        Index("ix_planning_change_history_reference_time", "entity_type", "entity_reference", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_reference: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(96), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlPlanningAuditJournal:
    def __init__(self, session: Session, *, actor_name: str = "") -> None:
        self._session = session
        self._actor_name = _text(actor_name)

    def _requirement_row(self, identifier: str):
        wanted = _text(identifier)
        return self._session.execute(
            select(ResourceRequirement, Project, WorkforceRequest, Resource)
            .join(Project, ResourceRequirement.project_id == Project.id)
            .outerjoin(WorkforceRequest, ResourceRequirement.workforce_request_id == WorkforceRequest.id)
            .outerjoin(Resource, ResourceRequirement.assigned_resource_id == Resource.id)
            .where(
                (ResourceRequirement.id == wanted)
                | (ResourceRequirement.legacy_segment_id == wanted)
            )
        ).one_or_none()

    def requirement_snapshot(self, identifier: str) -> tuple[str, str, dict[str, object]] | None:
        row = self._requirement_row(identifier)
        if row is None:
            return None
        requirement, project, request, resource = row
        reference = _optional_text(requirement.legacy_segment_id) or requirement.id
        snapshot: dict[str, object] = {
            "segment_id": reference,
            "demand_number": (
                (_optional_text(request.legacy_demand_number) or request.id)
                if request is not None
                else None
            ),
            "project_number": project.number,
            "source_request_line_id": requirement.source_request_line_id,
            "source_effort_id": requirement.source_effort_id,
            "technician": resource.name if resource is not None else None,
            "start_date": requirement.start_date,
            "end_date": requirement.end_date,
            "planned_hours": requirement.planned_hours,
            "status": requirement.status,
            "description": requirement.description,
            "required_resource_class": requirement.required_resource_class,
            "required_competency": requirement.required_competency,
            "planning_type": requirement.planning_type,
            "priority": requirement.priority,
            "outside_standard_hours": bool(requirement.outside_standard_hours_allowed),
            "confirmation": requirement.confirmation,
            "confirmation_overridden": bool(requirement.confirmation_overridden),
            "origin": requirement.origin,
            "operational_responsible_override_contact_id": (
                requirement.operational_responsible_override_contact_id
            ),
        }
        return requirement.id, reference, snapshot

    def shift_snapshot(self, identifier: str) -> tuple[str, str, str | None, dict[str, object]] | None:
        wanted = _text(identifier)
        row = self._session.execute(
            select(Shift, ResourceRequirement, Resource)
            .join(ResourceRequirement, Shift.resource_requirement_id == ResourceRequirement.id)
            .join(Resource, Shift.resource_id == Resource.id)
            .where((Shift.id == wanted) | (Shift.legacy_allocation_id == wanted))
        ).one_or_none()
        if row is None:
            return None
        shift, requirement, resource = row
        reference = _optional_text(shift.legacy_allocation_id) or shift.id
        parent = _optional_text(requirement.legacy_segment_id) or requirement.id
        snapshot: dict[str, object] = {
            "allocation_id": reference,
            "segment_id": parent,
            "resource_id": resource.id,
            "technician": resource.name,
            "work_date": shift.work_date,
            "hours": shift.hours,
            "allocation_type": shift.allocation_type,
            "source": shift.source,
            "locked": bool(shift.locked),
            "outside_standard_hours": bool(shift.outside_standard_hours),
            "confirmation": shift.confirmation,
            "note": shift.note,
            "operational_responsible_override_contact_id": (
                shift.operational_responsible_override_contact_id
            ),
        }
        asset_row = self._session.execute(
            select(AssetRequirement, AssetAllocation, Asset)
            .outerjoin(
                AssetAllocation,
                AssetAllocation.asset_requirement_id == AssetRequirement.id,
            )
            .outerjoin(Asset, Asset.id == AssetAllocation.asset_id)
            .where(
                AssetRequirement.origin
                == AssetRequirementOrigin.SHIFT_AD_HOC.value,
                AssetRequirement.shift_id == shift.id,
            )
        ).one_or_none()
        if asset_row is None:
            snapshot["asset_assignment"] = None
        else:
            asset_requirement, asset_allocation, asset = asset_row
            snapshot["asset_assignment"] = {
                "requirement_id": asset_requirement.id,
                "allocation_id": (
                    asset_allocation.id if asset_allocation is not None else None
                ),
                "origin": asset_requirement.origin,
                "asset_id": (
                    asset_allocation.asset_id
                    if asset_allocation is not None
                    else None
                ),
                "asset_code": asset.code if asset is not None else None,
                "operator_resource_id": (
                    asset_allocation.operator_resource_id
                    if asset_allocation is not None
                    else None
                ),
                "start_date": (
                    asset_allocation.start_date.isoformat()
                    if asset_allocation is not None
                    else asset_requirement.start_date.isoformat()
                ),
                "end_date": (
                    asset_allocation.end_date.isoformat()
                    if asset_allocation is not None
                    else asset_requirement.end_date.isoformat()
                ),
            }
        return shift.id, reference, parent, snapshot

    def request_requirements(self, demand_number: str) -> dict[str, tuple[str, dict[str, object]]]:
        wanted = _text(demand_number)
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.id == wanted)
                | (WorkforceRequest.legacy_demand_number == wanted)
            )
        )
        if request is None:
            return {}
        requirements = self._session.scalars(
            select(ResourceRequirement).where(ResourceRequirement.workforce_request_id == request.id)
        ).all()
        result: dict[str, tuple[str, dict[str, object]]] = {}
        for requirement in requirements:
            snapshot = self.requirement_snapshot(requirement.id)
            if snapshot is not None:
                entity_id, reference, values = snapshot
                result[entity_id] = (reference, values)
        return result

    def append(
        self,
        *,
        entity_type: str,
        entity_id: str,
        entity_reference: str,
        action: str,
        before: Mapping[str, object] | None = None,
        after: Mapping[str, object] | None = None,
        parent_reference: str | None = None,
    ) -> None:
        if before is not None and after is not None and _details(before, after) == '{"changes":{}}':
            return
        self._session.add(
            PlanningChangeHistory(
                entity_type=entity_type,
                entity_id=entity_id,
                entity_reference=entity_reference,
                parent_reference=_optional_text(parent_reference),
                action=action,
                details=_details(before, after),
                actor_name=self._actor_name or None,
                occurred_at=utc_now(),
            )
        )
        self._session.flush()


class AuditedSegmentRepository(SegmentRepositoryPort):
    def __init__(self, delegate: SegmentRepositoryPort, journal: SqlPlanningAuditJournal) -> None:
        self._delegate = delegate
        self._journal = journal

    def list(self, *, include_cancelled: bool = True) -> Sequence[SegmentReadModel]:
        return self._delegate.list(include_cancelled=include_cancelled)

    def get(self, segment_id: str) -> SegmentReadModel | None:
        return self._delegate.get(segment_id)

    def create(self, values: Mapping[str, Any]) -> str:
        reference = self._delegate.create(values)
        current = self._journal.requirement_snapshot(reference)
        if current is not None:
            entity_id, entity_reference, after = current
            self._journal.append(
                entity_type=ENTITY_SEGMENT,
                entity_id=entity_id,
                entity_reference=entity_reference,
                action="Création segment",
                after=after,
            )
        return reference

    def update(self, segment_id: str, updates: Mapping[str, Any]) -> None:
        previous = self._journal.requirement_snapshot(segment_id)
        self._delegate.update(segment_id, updates)
        current = self._journal.requirement_snapshot(segment_id)
        if previous is None or current is None:
            return
        entity_id, entity_reference, before = previous
        _, _, after = current
        self._journal.append(
            entity_type=ENTITY_SEGMENT,
            entity_id=entity_id,
            entity_reference=entity_reference,
            action="Modification segment",
            before=before,
            after=after,
        )


class AuditedAllocationCommandAdapter(AllocationCommandPort):
    def __init__(self, delegate: AllocationCommandPort, journal: SqlPlanningAuditJournal) -> None:
        self._delegate = delegate
        self._journal = journal

    def create_manual(
        self,
        segment_id: str,
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        confirmation: str | None = None,
    ) -> str:
        reference = self._delegate.create_manual(
            segment_id,
            technician,
            day_value,
            hours_value,
            hors_horaire,
            note,
            confirmation,
        )
        current = self._journal.shift_snapshot(reference)
        if current is not None:
            entity_id, entity_reference, parent, after = current
            self._journal.append(
                entity_type=ENTITY_SHIFT,
                entity_id=entity_id,
                entity_reference=entity_reference,
                parent_reference=parent,
                action="Création quart manuel",
                after=after,
            )
        return reference

    def update_manual(
        self,
        allocation_id: str,
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        confirmation: str | None = None,
    ) -> None:
        previous = self._journal.shift_snapshot(allocation_id)
        self._delegate.update_manual(
            allocation_id,
            technician,
            day_value,
            hours_value,
            hors_horaire,
            note,
            confirmation,
        )
        current = self._journal.shift_snapshot(allocation_id)
        if previous is None or current is None:
            return
        entity_id, entity_reference, parent, before = previous
        _, _, _, after = current
        self._journal.append(
            entity_type=ENTITY_SHIFT,
            entity_id=entity_id,
            entity_reference=entity_reference,
            parent_reference=parent,
            action="Modification quart manuel",
            before=before,
            after=after,
        )

    def move_manual(
        self,
        allocation_id: str,
        technician: str,
        day_value: Any,
        outside_standard_hours: bool = False,
    ) -> None:
        previous = self._journal.shift_snapshot(allocation_id)
        self._delegate.move_manual(
            allocation_id,
            technician,
            day_value,
            outside_standard_hours,
        )
        current = self._journal.shift_snapshot(allocation_id)
        if previous is None or current is None:
            return
        entity_id, entity_reference, parent, before = previous
        _, _, _, after = current
        self._journal.append(
            entity_type=ENTITY_SHIFT,
            entity_id=entity_id,
            entity_reference=entity_reference,
            parent_reference=parent,
            action="Déplacement quart",
            before=before,
            after=after,
        )

    def release_manual(self, allocation_id: str) -> None:
        previous = self._journal.shift_snapshot(allocation_id)
        self._delegate.release_manual(allocation_id)
        if previous is None:
            return
        entity_id, entity_reference, parent, before = previous
        current = self._journal.shift_snapshot(allocation_id)
        after = current[3] if current is not None else None
        self._journal.append(
            entity_type=ENTITY_SHIFT,
            entity_id=entity_id,
            entity_reference=entity_reference,
            parent_reference=parent,
            action="Libération quart manuel",
            before=before,
            after=after,
        )

    def delete_manual(self, allocation_id: str) -> None:
        previous = self._journal.shift_snapshot(allocation_id)
        self._delegate.delete_manual(allocation_id)
        if previous is None:
            return
        entity_id, entity_reference, parent, before = previous
        self._journal.append(
            entity_type=ENTITY_SHIFT,
            entity_id=entity_id,
            entity_reference=entity_reference,
            parent_reference=parent,
            action="Suppression quart manuel",
            before=before,
        )

    def assign_segment(self, segment_id: str, technician: str) -> Mapping[str, Any]:
        previous = self._journal.requirement_snapshot(segment_id)
        result = self._delegate.assign_segment(segment_id, technician)
        current = self._journal.requirement_snapshot(segment_id)
        if previous is not None and current is not None:
            entity_id, entity_reference, before = previous
            _, _, after = current
            self._journal.append(
                entity_type=ENTITY_SEGMENT,
                entity_id=entity_id,
                entity_reference=entity_reference,
                action="Affectation segment",
                before=before,
                after=after,
            )
        return result


class AuditedApprovedDemandSyncAdapter(ApprovedDemandSyncPort):
    def __init__(self, delegate: ApprovedDemandSyncPort, journal: SqlPlanningAuditJournal) -> None:
        self._delegate = delegate
        self._journal = journal

    def sync_approved(self, demand_number: str) -> None:
        before = self._journal.request_requirements(demand_number)
        self._delegate.sync_approved(demand_number)
        after = self._journal.request_requirements(demand_number)

        for entity_id in sorted(set(before) | set(after)):
            previous = before.get(entity_id)
            current = after.get(entity_id)
            reference = (current or previous)[0]  # type: ignore[index]
            before_values = previous[1] if previous is not None else None
            after_values = current[1] if current is not None else None
            action = (
                "Création segment depuis demande approuvée"
                if previous is None
                else "Synchronisation segment depuis demande approuvée"
            )
            self._journal.append(
                entity_type=ENTITY_SEGMENT,
                entity_id=entity_id,
                entity_reference=reference,
                action=action,
                before=before_values,
                after=after_values,
            )
