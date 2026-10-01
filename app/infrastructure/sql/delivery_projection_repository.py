from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.delivery_contracts import (
    ApprovedPlanningCapacitySourceReadModel,
    AssetReservedCapacityReadModel,
    WorkPackageDeliveryReferenceReadModel,
    WorkPackagePlanningCapacityReadModel,
)
from app.application.query_models import work_package_resource_class_diagnostic
from app.domain.approval_envelope import approval_envelope_from_snapshot_payload

from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from .asset_models import AssetAllocation, AssetRequirement
from .models import (
    ResourceRequirement,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkPackage,
)
from .resource_class_models import ResourceClassConfig
from .planning_version import SqlPlanningMutationVersionRepository


@dataclass(frozen=True, slots=True)
class _ActiveRevisionContext:
    revision_id: str
    request_id: str
    demand_reference: str
    request_version: int
    target_entry_keys: frozenset[str]


def _float(value: Decimal | float | int | None) -> float | None:
    return None if value is None else float(value)


class SqlDeliveryPlanningReadRepository:
    """Read-only bridge from active approved Planning into Delivery."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_work_package_delivery_reference(
        self,
        work_package_id: str,
    ) -> WorkPackageDeliveryReferenceReadModel | None:
        work_package = self._session.get(WorkPackage, str(work_package_id).strip())
        if work_package is None:
            return None
        reference = (
            str(work_package.code or "").strip()
            or str(work_package.legacy_effort_id or "").strip()
            or work_package.id
        )
        resource_class = (
            self._session.get(ResourceClassConfig, work_package.resource_class_code)
            if work_package.resource_class_code is not None
            else None
        )
        task = (
            self._session.get(TaskCatalogEntry, work_package.task_catalog_item_id)
            if work_package.task_catalog_item_id is not None
            else None
        )
        task_resource_class_code = (
            str(task.resource_class_code or "").strip() or None
            if task is not None
            else None
        )
        resource_class_code = (
            str(work_package.resource_class_code or "").strip() or None
        )
        return WorkPackageDeliveryReferenceReadModel(
            work_package_id=work_package.id,
            reference=reference,
            name=work_package.name,
            status=work_package.status,
            reference_hours=_float(work_package.planned_hours),
            resource_class_code=resource_class_code,
            resource_class_label=(
                str(resource_class.label or "").strip() or None
                if resource_class is not None
                else None
            ),
            resource_class_active=(
                bool(resource_class.active)
                if resource_class is not None
                else None
            ),
            task_resource_class_code=task_resource_class_code,
            resource_class_diagnostic=work_package_resource_class_diagnostic(
                resource_class_code,
                task_resource_class_code,
            ),
        )

    def _active_revision_contexts(
        self,
        work_package_id: str,
    ) -> tuple[_ActiveRevisionContext, ...]:
        rows = self._session.execute(
            select(
                RequestApprovalReference,
                RequestApprovalRevision,
                WorkforceRequest,
            )
            .join(
                RequestApprovalRevision,
                RequestApprovalReference.active_revision_id
                == RequestApprovalRevision.id,
            )
            .join(
                WorkforceRequest,
                RequestApprovalReference.workforce_request_id
                == WorkforceRequest.id,
            )
            .where(
                RequestApprovalReference.status
                == APPROVAL_REFERENCE_CAPTURED,
                RequestApprovalReference.active_revision_id.is_not(None),
            )
        ).all()

        contexts: list[_ActiveRevisionContext] = []
        for _reference, revision, request in rows:
            payload = json.loads(revision.payload_text)
            authorization = payload.get("authorization")
            if not isinstance(authorization, Mapping):
                raise ValueError(
                    "La révision approuvée active ne contient pas d'enveloppe valide."
                )
            envelope = approval_envelope_from_snapshot_payload(authorization)
            target_keys = frozenset(
                entry.identity.stable_key
                for entry in envelope.entries
                if entry.work_package_ref == work_package_id
            )
            if not target_keys:
                continue
            contexts.append(
                _ActiveRevisionContext(
                    revision_id=revision.id,
                    request_id=request.id,
                    demand_reference=(
                        str(request.legacy_demand_number or "").strip()
                        or request.id
                    ),
                    request_version=int(revision.request_version),
                    target_entry_keys=target_keys,
                )
            )
        return tuple(contexts)

    def get_work_package_planning_capacity(
        self,
        work_package_id: str,
    ) -> WorkPackagePlanningCapacityReadModel | None:
        work_package = self.get_work_package_delivery_reference(work_package_id)
        if work_package is None:
            return None

        contexts = self._active_revision_contexts(work_package.work_package_id)
        keys_by_revision = {
            context.revision_id: context.target_entry_keys
            for context in contexts
        }
        revision_ids = tuple(keys_by_revision)

        requirements = (
            tuple(
                self._session.scalars(
                    select(ResourceRequirement).where(
                        ResourceRequirement.approval_revision_id.in_(revision_ids),
                        ResourceRequirement.approval_reference_status
                        == APPROVAL_REFERENCE_CAPTURED,
                        ResourceRequirement.status != "Annulé",
                    )
                ).all()
            )
            if revision_ids
            else ()
        )
        requirements = tuple(
            requirement
            for requirement in requirements
            if requirement.approved_entry_key
            in keys_by_revision.get(requirement.approval_revision_id or "", ())
        )

        requirement_ids = tuple(requirement.id for requirement in requirements)
        shifts = (
            tuple(
                self._session.scalars(
                    select(Shift).where(
                        Shift.resource_requirement_id.in_(requirement_ids)
                    )
                ).all()
            )
            if requirement_ids
            else ()
        )

        asset_requirements = (
            tuple(
                self._session.scalars(
                    select(AssetRequirement).where(
                        AssetRequirement.approval_revision_id.in_(revision_ids),
                        AssetRequirement.status != "Annulé",
                    )
                ).all()
            )
            if revision_ids
            else ()
        )
        asset_requirements = tuple(
            requirement
            for requirement in asset_requirements
            if requirement.approved_entry_key
            in keys_by_revision.get(requirement.approval_revision_id or "", ())
        )
        asset_requirement_ids = tuple(
            requirement.id for requirement in asset_requirements
        )
        asset_allocations = (
            tuple(
                self._session.scalars(
                    select(AssetAllocation).where(
                        AssetAllocation.asset_requirement_id.in_(
                            asset_requirement_ids
                        )
                    )
                ).all()
            )
            if asset_requirement_ids
            else ()
        )

        reserved_hours = round(
            sum(float(shift.hours) for shift in shifts),
            2,
        )

        asset_type_by_requirement = {
            requirement.id: requirement.asset_type_id
            for requirement in asset_requirements
        }
        reserved_days_by_type: dict[str, int] = defaultdict(int)
        for allocation in asset_allocations:
            asset_type_id = asset_type_by_requirement.get(
                allocation.asset_requirement_id
            )
            if asset_type_id is None:
                continue
            reserved_days_by_type[asset_type_id] += (
                allocation.end_date - allocation.start_date
            ).days + 1

        window_starts: list[date] = [
            requirement.start_date for requirement in requirements
        ]
        window_starts.extend(
            requirement.start_date for requirement in asset_requirements
        )
        window_ends: list[date] = [
            requirement.end_date for requirement in requirements
        ]
        window_ends.extend(
            requirement.end_date for requirement in asset_requirements
        )

        used_keys_by_revision: dict[str, set[str]] = defaultdict(set)
        for requirement in requirements:
            if requirement.approval_revision_id and requirement.approved_entry_key:
                used_keys_by_revision[requirement.approval_revision_id].add(
                    requirement.approved_entry_key
                )
        for requirement in asset_requirements:
            if requirement.approval_revision_id and requirement.approved_entry_key:
                used_keys_by_revision[requirement.approval_revision_id].add(
                    requirement.approved_entry_key
                )

        approved_sources = tuple(
            ApprovedPlanningCapacitySourceReadModel(
                demand_reference=context.demand_reference,
                approval_revision_id=context.revision_id,
                approved_request_version=context.request_version,
                approved_entry_keys=tuple(
                    sorted(used_keys_by_revision.get(context.revision_id, ()))
                ),
            )
            for context in contexts
            if used_keys_by_revision.get(context.revision_id)
        )

        return WorkPackagePlanningCapacityReadModel(
            work_package_id=work_package.work_package_id,
            work_package_reference=work_package.reference,
            window_start=min(window_starts) if window_starts else None,
            window_end=max(window_ends) if window_ends else None,
            human_reserved_hours=reserved_hours,
            total_reserved_hours=reserved_hours,
            observed_planning_version=SqlPlanningMutationVersionRepository(
                self._session
            ).current_version(),
            approved_sources=approved_sources,
            asset_reserved_capacity=tuple(
                AssetReservedCapacityReadModel(
                    asset_type_id=asset_type_id,
                    reserved_days=reserved_days,
                )
                for asset_type_id, reserved_days in sorted(
                    reserved_days_by_type.items()
                )
            ),
        )
