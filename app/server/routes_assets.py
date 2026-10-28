"""Explicit API for the physical asset catalogue and reservation commands."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
import json
from typing import Callable

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..application.security import (
    PERMISSION_APPROVE_DEMANDS,
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_MANAGER,
    normalize_roles,
    permissions_for_roles,
)
from ..domain.reservable_assets import ResourcePeriodMode, continuous_assignment_state
from ..infrastructure.sql.asset_models import (
    Asset,
    AssetAllocation,
    AssetApprover,
    AssetRequirement,
    AssetType,
    AssetUnavailability,
)
from ..infrastructure.sql.asset_qualification import (
    QUALIFICATION_POLICY_ANY_ASSIGNED_WORKFORCE,
    evaluate_asset_qualification,
    evaluate_asset_qualifications,
    required_competencies,
)
from ..infrastructure.sql.asset_service import SqlAssetService
from ..infrastructure.sql.identity_models import AppUser
from ..infrastructure.sql.planning_version import SqlPlanningMutationVersionRepository
from ..infrastructure.sql.models import Resource


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TypeCreate(StrictBody):
    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=255)
    category: str
    metadata: dict | None = None


class AssetCreate(StrictBody):
    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=255)
    asset_type_id: str
    metadata: dict | None = None


class ActiveUpdate(StrictBody):
    active: bool
    expected_planning_version: int = Field(ge=1)


class TypeUpdate(StrictBody):
    expected_planning_version: int = Field(ge=1)
    code: str | None = None
    label: str | None = None
    category: str | None = None
    active: bool | None = None
    metadata: dict | None = None


class AssetUpdate(StrictBody):
    expected_planning_version: int = Field(ge=1)
    code: str | None = None
    label: str | None = None
    asset_type_id: str | None = None
    active: bool | None = None
    metadata: dict | None = None


class UnavailabilityCreate(StrictBody):
    start_date: date
    end_date: date
    reason: str | None = None
    expected_planning_version: int = Field(ge=1)


class ReservationChange(StrictBody):
    asset_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    expected_planning_version: int = Field(ge=1)


class ProjectDirectReservationCreate(StrictBody):
    project_id: str
    asset_type_id: str
    asset_id: str
    start_date: date
    end_date: date
    operator_resource_id: str | None = None
    expected_planning_version: int = Field(ge=1)


class ProjectDirectReservationUpdate(StrictBody):
    asset_id: str
    start_date: date
    end_date: date
    operator_resource_id: str | None = None
    expected_planning_version: int = Field(ge=1)


class ResourcePeriodReservationCreate(StrictBody):
    resource_id: str
    project_id: None = None
    asset_type_id: str
    asset_id: str
    start_date: date
    end_date: date
    expected_planning_version: int = Field(ge=1)


class ResourcePeriodReservationUpdate(StrictBody):
    project_id: None = None
    asset_id: str
    start_date: date
    end_date: date
    expected_planning_version: int = Field(ge=1)


class ContinuousVehicleAssignmentCreate(StrictBody):
    resource_id: str
    asset_id: str
    start_date: date
    expected_planning_version: int = Field(ge=1)


class ContinuousVehicleRevocation(StrictBody):
    effective_from: date
    expected_planning_version: int = Field(ge=1)


class ContinuousVehicleReschedule(StrictBody):
    start_date: date
    expected_planning_version: int = Field(ge=1)


class ContinuousVehicleTransfer(StrictBody):
    resource_id: str
    asset_id: str
    effective_from: date
    expected_planning_version: int = Field(ge=1)


class ContinuousVehicleRevocationReschedule(StrictBody):
    effective_from: date
    expected_planning_version: int = Field(ge=1)


class SegmentReservationCreate(StrictBody):
    segment_id: str
    asset_type_id: str
    asset_id: str
    start_date: date
    end_date: date
    operator_resource_id: str | None = None
    expected_planning_version: int = Field(ge=1)


class SegmentReservationUpdate(StrictBody):
    asset_id: str
    start_date: date
    end_date: date
    operator_resource_id: str | None = None
    expected_planning_version: int = Field(ge=1)


class ShiftAssetAssignmentChange(StrictBody):
    asset_id: str | None = None
    asset_requirement_id: str | None = None
    asset_allocation_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    expected_planning_version: int = Field(ge=1)


class TypeQualificationUpdate(StrictBody):
    competency_ids: list[str] = Field(default_factory=list)
    qualification_policy: str = QUALIFICATION_POLICY_ANY_ASSIGNED_WORKFORCE
    expected_planning_version: int = Field(ge=1)


class OperatorChange(StrictBody):
    operator_resource_id: str | None = None
    expected_planning_version: int = Field(ge=1)


def build_asset_router(session_dependency: Callable[[], Iterator[Session]]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/assets", tags=["assets"])

    def service(session: Session, request: Request) -> SqlAssetService:
        principal = getattr(request.state, "auth_principal", None)
        actor = str(getattr(principal, "local_user_id", None) or "api")
        return SqlAssetService(session, actor=actor)

    @router.get("/catalog")
    def catalog(session: Session = Depends(session_dependency)) -> dict:
        type_rows = tuple(session.scalars(select(AssetType).order_by(AssetType.code)))
        asset_rows = tuple(session.scalars(select(Asset).order_by(Asset.code)))
        approvers_by_asset: dict[str, list[str]] = {}
        for asset_id, user_id in session.execute(
            select(AssetApprover.asset_id, AssetApprover.app_user_id)
            .order_by(AssetApprover.asset_id, AssetApprover.app_user_id)
        ).all():
            approvers_by_asset.setdefault(asset_id, []).append(user_id)
        approver_candidates = []
        for user in session.scalars(select(AppUser).order_by(AppUser.display_name, AppUser.id)).all():
            try:
                roles = normalize_roles(
                    tuple(str(value) for value in json.loads(user.roles_json or "[]"))
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                roles = ()
            if user.active and PERMISSION_APPROVE_DEMANDS in permissions_for_roles(roles):
                approver_candidates.append(
                    {"id": user.id, "display_name": user.display_name}
                )
        return {
            "types": [
                {
                    "id": row.id,
                    "code": row.code,
                    "label": row.label,
                    "category": row.category,
                    "active": row.active,
                    "occupancy_policy": row.occupancy_policy,
                    "qualification_policy": row.qualification_policy,
                    "required_competencies": [
                        {"id": competency.id, "name": competency.name}
                        for competency in required_competencies(session, row.id)
                    ],
                    "metadata": json.loads(row.metadata_json or "{}"),
                }
                for row in type_rows
            ],
            "assets": [
                {
                    "id": row.id,
                    "code": row.code,
                    "label": row.label,
                    "asset_type_id": row.asset_type_id,
                    "active": row.active,
                    "approver_user_ids": approvers_by_asset.get(row.id, []),
                    "metadata": json.loads(row.metadata_json or "{}"),
                }
                for row in asset_rows
            ],
            "approver_candidates": approver_candidates,
            "planning_version": SqlPlanningMutationVersionRepository(session).current_version(),
        }

    @router.post("/types", status_code=201)
    def create_type(body: TypeCreate, request: Request, session: Session = Depends(session_dependency)) -> dict:
        row = service(session, request).create_type(**body.model_dump())
        return {"id": row.id, "code": row.code}

    @router.post("", status_code=201)
    def create_asset(body: AssetCreate, request: Request, session: Session = Depends(session_dependency)) -> dict:
        row = service(session, request).create_asset(**body.model_dump())
        return {"id": row.id, "code": row.code}

    @router.patch("/types/{identifier}/active")
    def activate_type(identifier: str, body: ActiveUpdate, request: Request, session: Session = Depends(session_dependency)) -> dict:
        return service(session, request).set_active(AssetType, identifier, body.active, body.expected_planning_version)

    @router.patch("/types/{identifier}")
    def update_type(identifier: str, body: TypeUpdate, request: Request, session: Session = Depends(session_dependency)) -> dict:
        return service(session, request).update_catalog(AssetType, identifier,
            body.model_dump(exclude_unset=True, exclude={"expected_planning_version"}), body.expected_planning_version)

    @router.put("/types/{identifier}/qualification")
    def update_type_qualification(
        identifier: str,
        body: TypeQualificationUpdate,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).set_type_qualification(
            asset_type_id=identifier,
            competency_ids=body.competency_ids,
            qualification_policy=body.qualification_policy,
            expected_version=body.expected_planning_version,
        )

    @router.patch("/{identifier}/active")
    def activate_asset(identifier: str, body: ActiveUpdate, request: Request, session: Session = Depends(session_dependency)) -> dict:
        return service(session, request).set_active(Asset, identifier, body.active, body.expected_planning_version)

    @router.patch("/{identifier}")
    def update_asset(identifier: str, body: AssetUpdate, request: Request, session: Session = Depends(session_dependency)) -> dict:
        return service(session, request).update_catalog(Asset, identifier,
            body.model_dump(exclude_unset=True, exclude={"expected_planning_version"}), body.expected_planning_version)

    @router.put("/{identifier}/approvers/{user_id}")
    def assign_asset_approver(
        identifier: str,
        user_id: str,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).set_approver(
            asset_id=identifier,
            user_id=user_id,
            assigned=True,
        )

    @router.delete("/{identifier}/approvers/{user_id}")
    def remove_asset_approver(
        identifier: str,
        user_id: str,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).set_approver(
            asset_id=identifier,
            user_id=user_id,
            assigned=False,
        )

    @router.post("/{identifier}/unavailability", status_code=201)
    def add_unavailability(identifier: str, body: UnavailabilityCreate, request: Request,
                           session: Session = Depends(session_dependency)) -> dict:
        return service(session, request).add_unavailability(asset_id=identifier, **body.model_dump(exclude={"expected_planning_version"}),
                                                            expected_version=body.expected_planning_version)

    @router.delete("/{identifier}/unavailability/{unavailability_id}")
    def remove_unavailability(identifier: str, unavailability_id: str, expected_planning_version: int,
                              request: Request, session: Session = Depends(session_dependency)) -> dict:
        return service(session, request).remove_unavailability(
            asset_id=identifier, identifier=unavailability_id, expected_version=expected_planning_version)

    @router.get("/requirements")
    def requirements(session: Session = Depends(session_dependency)) -> dict:
        requirement_rows = tuple(session.scalars(select(AssetRequirement)))
        allocation_rows = tuple(session.scalars(select(AssetAllocation)))
        requirements_by_id = {row.id: row for row in requirement_rows}
        return {
            "requirements": [
                {
                    "id": row.id,
                    "origin": row.origin,
                    "request_id": row.workforce_request_id,
                    "request_line_id": row.source_request_line_id,
                    "project_id": row.project_id,
                    "resource_requirement_id": row.resource_requirement_id,
                    "shift_id": row.shift_id,
                    "context_resource_id": row.context_resource_id,
                    "asset_type_id": row.asset_type_id,
                    "start_date": row.start_date,
                    "end_date": row.end_date,
                    "resource_period_mode": row.resource_period_mode,
                    "effective_state": (
                        continuous_assignment_state(
                            start_date=row.start_date,
                            end_date=row.end_date,
                            as_of=date.today(),
                        ).value
                        if row.resource_period_mode == ResourcePeriodMode.CONTINUOUS.value
                        else None
                    ),
                    "usage_hours": row.usage_hours,
                    "status": row.status,
                    "approved_entry_key": row.approved_entry_key,
                }
                for row in requirement_rows
            ],
            "allocations": [
                {
                    "id": row.id,
                    "requirement_id": row.asset_requirement_id,
                    "asset_id": row.asset_id,
                    "operator_resource_id": row.operator_resource_id,
                    "start_date": row.start_date,
                    "end_date": row.end_date,
                    "locked": row.locked,
                    "qualification_state": evaluate_asset_qualification(
                        session,
                        requirement=requirements_by_id[row.asset_requirement_id],
                        allocation=row,
                    ).state,
                }
                for row in allocation_rows
            ],
            "unavailability": [{"id": row.id, "asset_id": row.asset_id, "start_date": row.start_date,
                                "end_date": row.end_date, "reason": row.reason}
                               for row in session.scalars(select(AssetUnavailability))],
            "planning_version": SqlPlanningMutationVersionRepository(session).current_version(),
        }

    @router.put("/requirements/{identifier}/reservation")
    def reserve(identifier: str, body: ReservationChange, request: Request,
                idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
                session: Session = Depends(session_dependency)) -> dict:
        return service(session, request).reserve(requirement_id=identifier, asset_id=body.asset_id,
                                                 start_date=body.start_date, end_date=body.end_date,
                                                 expected_version=body.expected_planning_version,
                                                 idempotency_key=idempotency_key)

    @router.post("/project-reservations", status_code=201)
    def create_project_direct_reservation(
        body: ProjectDirectReservationCreate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).create_project_direct_reservation(
            project_id=body.project_id,
            asset_type_id=body.asset_type_id,
            asset_id=body.asset_id,
            start_date=body.start_date,
            end_date=body.end_date,
            operator_resource_id=body.operator_resource_id,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.put("/project-reservations/{identifier}")
    def update_project_direct_reservation(
        identifier: str,
        body: ProjectDirectReservationUpdate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).update_project_direct_reservation(
            requirement_id=identifier,
            asset_id=body.asset_id,
            start_date=body.start_date,
            end_date=body.end_date,
            operator_resource_id=body.operator_resource_id,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.delete("/project-reservations/{identifier}")
    def release_project_direct_reservation(
        identifier: str,
        expected_planning_version: int,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).release_project_direct_reservation(
            requirement_id=identifier,
            expected_version=expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.post("/resource-period-reservations", status_code=201)
    def create_resource_period_reservation(
        body: ResourcePeriodReservationCreate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).create_resource_period_reservation(
            resource_id=body.resource_id,
            asset_type_id=body.asset_type_id,
            asset_id=body.asset_id,
            start_date=body.start_date,
            end_date=body.end_date,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.put("/resource-period-reservations/{identifier}")
    def update_resource_period_reservation(
        identifier: str,
        body: ResourcePeriodReservationUpdate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).update_resource_period_reservation(
            requirement_id=identifier,
            asset_id=body.asset_id,
            start_date=body.start_date,
            end_date=body.end_date,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.delete("/resource-period-reservations/{identifier}")
    def release_resource_period_reservation(
        identifier: str,
        expected_planning_version: int,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).release_resource_period_reservation(
            requirement_id=identifier,
            expected_version=expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.get("/continuous-resource-assignments")
    def continuous_vehicle_assignments(
        resource_id: str,
        as_of: date | None = None,
        session: Session = Depends(session_dependency),
    ) -> dict:
        business_date = as_of or date.today()
        rows = session.execute(
            select(AssetRequirement, AssetAllocation, Asset, AssetType)
            .join(
                AssetAllocation,
                AssetAllocation.asset_requirement_id == AssetRequirement.id,
            )
            .join(Asset, Asset.id == AssetAllocation.asset_id)
            .join(AssetType, AssetType.id == Asset.asset_type_id)
            .where(
                AssetRequirement.origin
                == "RESOURCE_PERIOD",
                AssetRequirement.resource_period_mode
                == ResourcePeriodMode.CONTINUOUS.value,
                AssetRequirement.context_resource_id == resource_id,
            )
            .order_by(AssetRequirement.start_date, AssetRequirement.id)
        ).all()
        return {
            "resource_id": resource_id,
            "as_of": business_date,
            "assignments": [
                {
                    "requirement_id": requirement.id,
                    "allocation_id": allocation.id,
                    "asset_id": asset.id,
                    "asset_code": asset.code,
                    "asset_label": asset.label,
                    "asset_type_id": asset_type.id,
                    "asset_type_code": asset_type.code,
                    "asset_type_label": asset_type.label,
                    "resource_period_mode": requirement.resource_period_mode,
                    "effective_state": continuous_assignment_state(
                        start_date=requirement.start_date,
                        end_date=requirement.end_date,
                        as_of=business_date,
                    ).value,
                    "start_date": requirement.start_date,
                    "end_date": requirement.end_date,
                    "operator_resource_id": allocation.operator_resource_id,
                    "qualification_state": evaluate_asset_qualification(
                        session,
                        requirement=requirement,
                        allocation=allocation,
                    ).state,
                }
                for requirement, allocation, asset, asset_type in rows
            ],
            "planning_version": SqlPlanningMutationVersionRepository(
                session
            ).current_version(),
        }

    @router.get("/continuous-vehicle-units/{asset_id}")
    def continuous_vehicle_unit_state(
        asset_id: str,
        request: Request,
        as_of: date | None = None,
        session: Session = Depends(session_dependency),
    ) -> dict:
        business_date = as_of or date.today()
        asset = session.get(Asset, asset_id)
        version = SqlPlanningMutationVersionRepository(session).current_version()
        if asset is None:
            return {
                "asset_id": asset_id,
                "asset_category": None,
                "as_of": business_date,
                "has_continuous_assignment": False,
                "assignment": None,
                "scheduled_assignment": None,
                "assignment_details_visible": False,
                "scheduled_assignment_details_visible": False,
                "has_ended_history": False,
                "assignment_authority": {
                    "allowed": False,
                    "can_assign": False,
                    "status": "NO_ACTIVE_SCOPE",
                    "code": "asset_not_found",
                    "message": "Unité d’actif introuvable.",
                    "diagnostics": [],
                },
                "planning_version": version,
            }

        asset_type = session.get(AssetType, asset.asset_type_id)
        rows = session.execute(
            select(AssetRequirement, AssetAllocation, Resource)
            .join(
                AssetAllocation,
                AssetAllocation.asset_requirement_id == AssetRequirement.id,
            )
            .join(Resource, Resource.id == AssetRequirement.context_resource_id)
            .where(
                AssetRequirement.origin == "RESOURCE_PERIOD",
                AssetRequirement.resource_period_mode
                == ResourcePeriodMode.CONTINUOUS.value,
                AssetAllocation.asset_id == asset_id,
            )
            .order_by(AssetRequirement.start_date, AssetRequirement.id)
        ).all()

        projected: list[tuple[AssetRequirement, AssetAllocation, Resource, str]] = []
        for requirement, allocation, resource in rows:
            projected.append(
                (
                    requirement,
                    allocation,
                    resource,
                    continuous_assignment_state(
                        start_date=requirement.start_date,
                        end_date=requirement.end_date,
                        as_of=business_date,
                    ).value,
                )
            )

        qualifications = evaluate_asset_qualifications(
            session,
            pairs=tuple(
                (requirement, allocation)
                for requirement, allocation, _resource, _state in projected
            ),
        )

        active = next((row for row in projected if row[3] == "ACTIVE"), None)
        scheduled_rows = [row for row in projected if row[3] == "SCHEDULED"]
        scheduled = scheduled_rows[0] if scheduled_rows else None
        primary = active or scheduled
        upcoming = scheduled if active is not None else None

        principal = request.state.auth_principal
        global_details = bool(
            {ROLE_ADMIN, ROLE_COORDINATOR, ROLE_MANAGER}.intersection(
                set(principal.roles)
            )
        )

        def details_visible(resource: Resource | None) -> bool:
            if resource is None:
                return False
            return global_details or bool(
                principal.employee_external_id
                and resource.external_id == principal.employee_external_id
            )

        def serialize(
            row: tuple[AssetRequirement, AssetAllocation, Resource, str] | None,
        ) -> tuple[dict | None, bool]:
            if row is None:
                return None, False
            requirement, allocation, resource, state = row
            visible = details_visible(resource)
            return (
                {
                    "requirement_id": requirement.id,
                    "allocation_id": allocation.id,
                    "asset_id": asset.id,
                    "asset_code": asset.code,
                    "asset_label": asset.label,
                    "asset_type_id": (
                        asset_type.id if asset_type is not None else asset.asset_type_id
                    ),
                    "asset_type_code": asset_type.code if asset_type is not None else "",
                    "asset_type_label": asset_type.label if asset_type is not None else "",
                    "resource_period_mode": requirement.resource_period_mode,
                    "effective_state": state,
                    "start_date": requirement.start_date,
                    "end_date": requirement.end_date,
                    "operator_resource_id": (
                        allocation.operator_resource_id if visible else None
                    ),
                    "qualification_state": qualifications[
                        (requirement.id, allocation.id)
                    ].state,
                },
                visible,
            )

        assignment, assignment_visible = serialize(primary)
        scheduled_assignment, scheduled_visible = serialize(upcoming)
        authority = service(session, request).assignment_authority_capability(asset_id)
        return {
            "asset_id": asset_id,
            "asset_category": asset_type.category if asset_type is not None else None,
            "as_of": business_date,
            "has_continuous_assignment": primary is not None,
            "assignment": assignment,
            "scheduled_assignment": scheduled_assignment,
            "assignment_details_visible": assignment_visible,
            "scheduled_assignment_details_visible": scheduled_visible,
            "has_ended_history": any(row[3] == "ENDED" for row in projected),
            "assignment_authority": authority,
            "planning_version": version,
        }

    @router.post("/continuous-resource-assignments", status_code=201)
    def create_continuous_vehicle_assignment(
        body: ContinuousVehicleAssignmentCreate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).create_continuous_vehicle_assignment(
            resource_id=body.resource_id,
            asset_id=body.asset_id,
            start_date=body.start_date,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.post("/continuous-resource-assignments/{identifier}/revoke")
    def revoke_continuous_vehicle_assignment(
        identifier: str,
        body: ContinuousVehicleRevocation,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).revoke_continuous_vehicle_assignment(
            requirement_id=identifier,
            effective_from=body.effective_from,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.delete("/continuous-resource-assignments/{identifier}")
    def cancel_continuous_vehicle_assignment(
        identifier: str,
        expected_planning_version: int,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).cancel_continuous_vehicle_assignment(
            requirement_id=identifier,
            expected_version=expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.put("/continuous-resource-assignments/{identifier}/schedule")
    def reschedule_continuous_vehicle_assignment(
        identifier: str,
        body: ContinuousVehicleReschedule,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).reschedule_continuous_vehicle_assignment(
            requirement_id=identifier,
            start_date=body.start_date,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.delete("/continuous-resource-assignments/{identifier}/revocation")
    def cancel_continuous_vehicle_revocation(
        identifier: str,
        expected_planning_version: int,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).cancel_continuous_revocation(
            requirement_id=identifier,
            expected_version=expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.put("/continuous-resource-assignments/{identifier}/revocation")
    def reschedule_continuous_vehicle_revocation(
        identifier: str,
        body: ContinuousVehicleRevocationReschedule,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).reschedule_continuous_revocation(
            requirement_id=identifier,
            effective_from=body.effective_from,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.post("/continuous-resource-assignments/{identifier}/transfer", status_code=201)
    def transfer_continuous_vehicle_assignment(
        identifier: str,
        body: ContinuousVehicleTransfer,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).transfer_continuous_vehicle_assignment(
            requirement_id=identifier,
            resource_id=body.resource_id,
            asset_id=body.asset_id,
            effective_from=body.effective_from,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.post("/segment-reservations", status_code=201)
    def create_segment_reservation(
        body: SegmentReservationCreate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).create_segment_reservation(
            segment_id=body.segment_id,
            asset_type_id=body.asset_type_id,
            asset_id=body.asset_id,
            start_date=body.start_date,
            end_date=body.end_date,
            operator_resource_id=body.operator_resource_id,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.put("/segment-reservations/{identifier}")
    def update_segment_reservation(
        identifier: str,
        body: SegmentReservationUpdate,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).update_segment_reservation(
            requirement_id=identifier,
            asset_id=body.asset_id,
            start_date=body.start_date,
            end_date=body.end_date,
            operator_resource_id=body.operator_resource_id,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.delete("/segment-reservations/{identifier}")
    def release_segment_reservation(
        identifier: str,
        expected_planning_version: int,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).release_segment_reservation(
            requirement_id=identifier,
            expected_version=expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.get("/shifts/{identifier}/assignment/candidates")
    def shift_asset_candidates(
        identifier: str,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).shift_asset_candidates(identifier)

    @router.put("/shifts/{identifier}/assignment")
    def set_shift_asset(
        identifier: str,
        body: ShiftAssetAssignmentChange,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).set_shift_asset(
            shift_id=identifier,
            asset_id=body.asset_id,
            requirement_id=body.asset_requirement_id,
            allocation_id=body.asset_allocation_id,
            start_date=body.start_date,
            end_date=body.end_date,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    @router.get("/requirements/{identifier}/operator-candidates")
    def operator_candidates(
        identifier: str,
        request: Request,
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).operator_candidates(identifier)

    @router.put("/requirements/{identifier}/operator")
    def set_operator(
        identifier: str,
        body: OperatorChange,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return service(session, request).set_operator(
            requirement_id=identifier,
            operator_resource_id=body.operator_resource_id,
            expected_version=body.expected_planning_version,
            idempotency_key=idempotency_key,
        )

    return router
