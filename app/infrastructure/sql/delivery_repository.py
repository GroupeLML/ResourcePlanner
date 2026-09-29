from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain.delivery import (
    DeliveryItem,
    DeliveryItemStatus,
    DeliveryItemType,
    DeliveryPlan,
    DeliveryPlanStatus,
)

from .base import utc_now
from .delivery_models import DeliveryChangeHistory, DeliveryItemRow, DeliveryPlanRow
from .identity_models import AppUser
from .models import WorkPackage


class DeliveryVersionConflict(RuntimeError):
    pass


def _hours(value: Decimal | float | int | None) -> float | None:
    return None if value is None else float(value)


def _plan_from_row(row: DeliveryPlanRow) -> DeliveryPlan:
    return DeliveryPlan(
        id=row.id,
        work_package_id=row.work_package_id,
        status=DeliveryPlanStatus(row.status),
        lead_user_id=row.lead_user_id,
        delivery_version=row.delivery_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        activated_at=row.activated_at,
        archived_at=row.archived_at,
    )


def _item_from_row(row: DeliveryItemRow) -> DeliveryItem:
    return DeliveryItem(
        id=row.id,
        delivery_plan_id=row.delivery_plan_id,
        item_type=DeliveryItemType(row.item_type),
        title=row.title,
        parent_id=row.parent_id,
        description=row.description,
        priority=row.priority,
        status=DeliveryItemStatus(row.status),
        assignee_user_id=row.assignee_user_id,
        current_estimate_hours=_hours(row.current_estimate_hours),
        reference_estimate_hours=_hours(row.reference_estimate_hours),
        remaining_hours=_hours(row.remaining_hours),
        due_date=row.due_date,
        position=row.position,
        sprint=row.sprint,
    )


class SqlDeliveryRepository:
    """Persistence primitives for Delivery without coupling to Planning CAS."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_plan(self, plan_id: str) -> DeliveryPlan | None:
        row = self._session.get(DeliveryPlanRow, str(plan_id).strip())
        return _plan_from_row(row) if row is not None else None

    def get_non_archived_plan_for_work_package(
        self, work_package_id: str
    ) -> DeliveryPlan | None:
        row = self._session.scalar(
            select(DeliveryPlanRow).where(
                DeliveryPlanRow.work_package_id == str(work_package_id).strip(),
                DeliveryPlanRow.status != DeliveryPlanStatus.ARCHIVED.value,
            )
        )
        return _plan_from_row(row) if row is not None else None

    def get_item(self, item_id: str) -> DeliveryItem | None:
        row = self._session.get(DeliveryItemRow, str(item_id).strip())
        return _item_from_row(row) if row is not None else None

    def list_items(self, plan_id: str) -> Sequence[DeliveryItem]:
        rows = self._session.scalars(
            select(DeliveryItemRow)
            .where(DeliveryItemRow.delivery_plan_id == str(plan_id).strip())
            .order_by(
                DeliveryItemRow.position,
                DeliveryItemRow.created_at,
                DeliveryItemRow.id,
            )
        ).all()
        return tuple(_item_from_row(row) for row in rows)

    def work_package_exists(self, work_package_id: str) -> bool:
        return (
            self._session.get(WorkPackage, str(work_package_id).strip()) is not None
        )

    def user_exists(self, user_id: str) -> bool:
        return self._session.get(AppUser, str(user_id).strip()) is not None

    def add_plan(self, plan: DeliveryPlan) -> None:
        row = DeliveryPlanRow(
            id=plan.id,
            work_package_id=plan.work_package_id,
            status=plan.status.value,
            lead_user_id=plan.lead_user_id,
            delivery_version=plan.delivery_version,
            activated_at=plan.activated_at,
            archived_at=plan.archived_at,
        )
        if plan.created_at is not None:
            row.created_at = plan.created_at
        if plan.updated_at is not None:
            row.updated_at = plan.updated_at
        self._session.add(row)
        self._session.flush()

    def add_item(self, item: DeliveryItem) -> None:
        self._session.add(
            DeliveryItemRow(
                id=item.id,
                delivery_plan_id=item.delivery_plan_id,
                parent_id=item.parent_id,
                item_type=item.item_type.value,
                title=item.title,
                description=item.description,
                priority=item.priority,
                status=item.status.value,
                assignee_user_id=item.assignee_user_id,
                current_estimate_hours=item.current_estimate_hours,
                reference_estimate_hours=item.reference_estimate_hours,
                remaining_hours=item.remaining_hours,
                due_date=item.due_date,
                position=item.position,
                sprint=item.sprint,
            )
        )
        self._session.flush()

    def save_plan(self, plan: DeliveryPlan) -> None:
        row = self._session.get(DeliveryPlanRow, plan.id)
        if row is None:
            raise KeyError(f"DeliveryPlan not found: {plan.id}")
        row.status = plan.status.value
        row.lead_user_id = plan.lead_user_id
        row.delivery_version = plan.delivery_version
        row.activated_at = plan.activated_at
        row.archived_at = plan.archived_at
        row.updated_at = plan.updated_at or utc_now()
        self._session.flush()

    def save_item(self, item: DeliveryItem) -> None:
        row = self._session.get(DeliveryItemRow, item.id)
        if row is None:
            raise KeyError(f"DeliveryItem not found: {item.id}")
        row.parent_id = item.parent_id
        row.title = item.title
        row.description = item.description
        row.priority = item.priority
        row.status = item.status.value
        row.assignee_user_id = item.assignee_user_id
        row.current_estimate_hours = item.current_estimate_hours
        row.reference_estimate_hours = item.reference_estimate_hours
        row.remaining_hours = item.remaining_hours
        row.due_date = item.due_date
        row.position = item.position
        row.sprint = item.sprint
        self._session.flush()

    def compare_and_increment_version(
        self, plan_id: str, *, expected_delivery_version: int
    ) -> int:
        if expected_delivery_version < 1:
            raise ValueError("expected_delivery_version must be at least 1")
        next_version = expected_delivery_version + 1
        result = self._session.execute(
            update(DeliveryPlanRow)
            .where(
                DeliveryPlanRow.id == str(plan_id).strip(),
                DeliveryPlanRow.delivery_version == expected_delivery_version,
            )
            .values(delivery_version=next_version, updated_at=utc_now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise DeliveryVersionConflict(
                f"DeliveryPlan version conflict for {plan_id}: "
                f"expected {expected_delivery_version}"
            )
        self._session.flush()
        return next_version

    def append_history(
        self,
        *,
        plan_id: str,
        action: str,
        delivery_version: int,
        item_id: str | None = None,
        actor_user_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> str:
        normalized_action = str(action or "").strip()
        if not normalized_action:
            raise ValueError("Delivery audit action is required")
        if delivery_version < 1:
            raise ValueError("delivery_version must be at least 1")
        row = DeliveryChangeHistory(
            delivery_plan_id=str(plan_id).strip(),
            delivery_item_id=str(item_id).strip() if item_id else None,
            actor_user_id=str(actor_user_id).strip() if actor_user_id else None,
            action=normalized_action,
            delivery_version=delivery_version,
            details_json=json.dumps(
                dict(details or {}),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ),
        )
        self._session.add(row)
        self._session.flush()
        return row.id
