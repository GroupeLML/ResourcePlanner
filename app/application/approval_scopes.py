from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from ..domain.approval_routing import (
    ApprovalLineResolution,
    ApprovalRoutingUser,
    ApprovalScopeCandidate,
    DIAGNOSTIC_RESOURCE_CLASS_INACTIVE,
    DIAGNOSTIC_RESOURCE_CLASS_MISSING,
    DIAGNOSTIC_RESOURCE_CLASS_NOT_FOUND,
    resolve_asset_line_approvers,
    resolve_line_approvers,
    suggested_approval_scope_code,
)
from .errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
    call_application_port,
)
from .security import PERMISSION_APPROVE_DEMANDS


@dataclass(frozen=True, slots=True)
class ApprovalScopeRecord:
    id: str
    code: str
    label: str
    active: bool
    version: int
    approver_user_ids: tuple[str, ...] = ()
    resource_class_codes: tuple[str, ...] = ()
    asset_type_ids: tuple[str, ...] = ()
    task_catalog_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalTaskRecord:
    id: str
    code: str
    active: bool
    resource_class_code: str | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalResourceClassRecord:
    code: str
    active: bool


@dataclass(frozen=True, slots=True)
class ApprovalAssetTypeRecord:
    id: str
    code: str
    label: str
    active: bool


@dataclass(frozen=True, slots=True)
class ApprovalAssetRecord:
    id: str
    code: str
    label: str
    asset_type_id: str
    active: bool


@dataclass(frozen=True, slots=True)
class ApprovalRequestLineRecord:
    id: str
    active: bool
    task_catalog_item_id: str | None
    kind: str = "WORKFORCE"
    asset_type_id: str | None = None
    proposed_asset_id: str | None = None
    position: int = 0
    erp_task_code: str | None = None
    erp_task_label: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalUserRecord:
    user_id: str
    active: bool
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequestLineApprovalResolution:
    request_line_id: str
    task_catalog_item_id: str | None
    suggested_scope_code: str | None
    resolution: ApprovalLineResolution
    line_position: int | None = None
    task_code: str | None = None
    task_label: str | None = None
    effective_resource_class: str | None = None
    approval_scope_candidates: tuple[ApprovalScopeRecord, ...] = ()
    line_kind: str = "WORKFORCE"
    asset_type_id: str | None = None
    asset_type_code: str | None = None
    asset_type_label: str | None = None
    proposed_asset_id: str | None = None
    proposed_asset_code: str | None = None
    proposed_asset_label: str | None = None


class ApprovalScopeRepositoryPort(Protocol):
    def list_scopes(self) -> tuple[ApprovalScopeRecord, ...]: ...
    def get_scope(self, scope_id: str) -> ApprovalScopeRecord | None: ...
    def find_scope_by_code(self, code: str) -> ApprovalScopeRecord | None: ...
    def create_scope(self, *, code: str, label: str, active: bool) -> ApprovalScopeRecord: ...
    def update_scope(
        self,
        scope_id: str,
        *,
        values: Mapping[str, object],
        expected_version: int,
    ) -> ApprovalScopeRecord: ...
    def set_scope_approver(
        self,
        scope_id: str,
        user_id: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord: ...
    def set_task_scope(
        self,
        scope_id: str,
        task_catalog_item_id: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord: ...
    def set_resource_class_scope(
        self,
        scope_id: str,
        resource_class_code: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord: ...
    def set_asset_type_scope(
        self,
        scope_id: str,
        asset_type_id: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord: ...
    def get_request_line(self, line_id: str) -> ApprovalRequestLineRecord | None: ...
    def get_task(self, task_id: str) -> ApprovalTaskRecord | None: ...
    def get_resource_class(
        self,
        class_code: str,
    ) -> ApprovalResourceClassRecord | None: ...
    def get_asset_type(self, asset_type_id: str) -> ApprovalAssetTypeRecord | None: ...
    def get_asset(self, asset_id: str) -> ApprovalAssetRecord | None: ...
    def list_task_scopes(self, task_id: str) -> tuple[ApprovalScopeRecord, ...]: ...
    def list_resource_class_scopes(
        self,
        class_code: str,
    ) -> tuple[ApprovalScopeRecord, ...]: ...
    def list_asset_type_scopes(
        self,
        asset_type_id: str,
    ) -> tuple[ApprovalScopeRecord, ...]: ...
    def list_scope_approver_ids(self, scope_id: str) -> tuple[str, ...]: ...
    def list_asset_approver_ids(self, asset_id: str) -> tuple[str, ...]: ...
    def get_users(self, user_ids: Sequence[str]) -> tuple[ApprovalUserRecord, ...]: ...
    def get_user(self, user_id: str) -> ApprovalUserRecord | None: ...


def _required(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ApplicationValidationError(
            f"{field} est requis.",
            code="approval_scope_field_required",
            context={"field": field},
        )
    return normalized


def _code(value: object) -> str:
    normalized = _required(value, "code").upper()
    if len(normalized) > 64:
        raise ApplicationValidationError(
            "Le code du périmètre d'approbation est trop long.",
            code="approval_scope_code_invalid",
        )
    return normalized


class ApprovalScopeService:
    def __init__(self, repository: ApprovalScopeRepositoryPort) -> None:
        self._repository = repository

    def list_scopes(self) -> tuple[ApprovalScopeRecord, ...]:
        return call_application_port(
            self._repository.list_scopes,
            code_prefix="approval_scope_list",
        )

    def create_scope(
        self,
        *,
        code: str,
        label: str,
        active: bool = True,
    ) -> ApprovalScopeRecord:
        normalized_code = _code(code)
        normalized_label = _required(label, "label")
        existing = call_application_port(
            lambda: self._repository.find_scope_by_code(normalized_code),
            code_prefix="approval_scope_read",
            context={"code": normalized_code},
        )
        if existing is not None:
            raise ApplicationConflictError(
                "Ce code de périmètre d'approbation existe déjà.",
                code="approval_scope_code_conflict",
                context={
                    "code": normalized_code,
                    "approval_scope_id": existing.id,
                },
            )
        return call_application_port(
            lambda: self._repository.create_scope(
                code=normalized_code,
                label=normalized_label,
                active=bool(active),
            ),
            code_prefix="approval_scope_create",
        )

    def update_scope(
        self,
        scope_id: str,
        *,
        expected_version: int,
        label: str | None = None,
        active: bool | None = None,
    ) -> ApprovalScopeRecord:
        identifier = _required(scope_id, "approval_scope_id")
        values: dict[str, object] = {}
        if label is not None:
            values["label"] = _required(label, "label")
        if active is not None:
            values["active"] = bool(active)
        if not values:
            raise ApplicationValidationError(
                "Au moins une modification du périmètre doit être fournie.",
                code="approval_scope_update_empty",
            )
        return call_application_port(
            lambda: self._repository.update_scope(
                identifier,
                values=values,
                expected_version=int(expected_version),
            ),
            code_prefix="approval_scope_update",
            context={"approval_scope_id": identifier},
        )

    def set_approver(
        self,
        scope_id: str,
        user_id: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord:
        identifier = _required(scope_id, "approval_scope_id")
        user_identifier = _required(user_id, "app_user_id")
        user = call_application_port(
            lambda: self._repository.get_user(user_identifier),
            code_prefix="approval_scope_user_read",
            context={"app_user_id": user_identifier},
        )
        if user is None:
            raise ApplicationNotFoundError(
                "Utilisateur RessourcePlanner introuvable.",
                code="approval_scope_user_not_found",
                context={"app_user_id": user_identifier},
            )
        if assigned and (
            not user.active
            or PERMISSION_APPROVE_DEMANDS not in user.permissions
        ):
            raise ApplicationValidationError(
                "Un approbateur doit être actif et posséder approve_demands.",
                code="approval_scope_user_not_admissible",
                context={"app_user_id": user_identifier},
            )
        return call_application_port(
            lambda: self._repository.set_scope_approver(
                identifier,
                user_identifier,
                assigned=bool(assigned),
                expected_version=int(expected_version),
            ),
            code_prefix="approval_scope_approver_update",
            context={
                "approval_scope_id": identifier,
                "app_user_id": user_identifier,
            },
        )

    def set_task(
        self,
        scope_id: str,
        task_catalog_item_id: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord:
        identifier = _required(scope_id, "approval_scope_id")
        task_id = _required(task_catalog_item_id, "task_catalog_item_id")
        task = call_application_port(
            lambda: self._repository.get_task(task_id),
            code_prefix="approval_scope_task_read",
            context={"task_catalog_item_id": task_id},
        )
        if task is None:
            raise ApplicationNotFoundError(
                "Tâche ERP introuvable.",
                code="approval_scope_task_not_found",
                context={"task_catalog_item_id": task_id},
            )
        return call_application_port(
            lambda: self._repository.set_task_scope(
                identifier,
                task_id,
                assigned=bool(assigned),
                expected_version=int(expected_version),
            ),
            code_prefix="approval_scope_task_update",
            context={
                "approval_scope_id": identifier,
                "task_catalog_item_id": task_id,
            },
        )

    def set_resource_class(
        self,
        scope_id: str,
        resource_class_code: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord:
        identifier = _required(scope_id, "approval_scope_id")
        class_code = _required(resource_class_code, "resource_class_code")
        resource_class = call_application_port(
            lambda: self._repository.get_resource_class(class_code),
            code_prefix="approval_scope_resource_class_read",
            context={"resource_class_code": class_code},
        )
        if resource_class is None:
            raise ApplicationNotFoundError(
                "Classe de ressource introuvable.",
                code="approval_scope_resource_class_not_found",
                context={"resource_class_code": class_code},
            )
        return call_application_port(
            lambda: self._repository.set_resource_class_scope(
                identifier,
                class_code,
                assigned=bool(assigned),
                expected_version=int(expected_version),
            ),
            code_prefix="approval_scope_resource_class_update",
            context={
                "approval_scope_id": identifier,
                "resource_class_code": class_code,
            },
        )


    def set_asset_type(
        self,
        scope_id: str,
        asset_type_id: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord:
        identifier = _required(scope_id, "approval_scope_id")
        type_id = _required(asset_type_id, "asset_type_id")
        asset_type = call_application_port(
            lambda: self._repository.get_asset_type(type_id),
            code_prefix="approval_scope_asset_type_read",
            context={"asset_type_id": type_id},
        )
        if asset_type is None:
            raise ApplicationNotFoundError(
                "Type d'actif introuvable.",
                code="approval_scope_asset_type_not_found",
                context={"asset_type_id": type_id},
            )
        return call_application_port(
            lambda: self._repository.set_asset_type_scope(
                identifier,
                type_id,
                assigned=bool(assigned),
                expected_version=int(expected_version),
            ),
            code_prefix="approval_scope_asset_type_update",
            context={
                "approval_scope_id": identifier,
                "asset_type_id": type_id,
            },
        )

    @staticmethod
    def _blocked_resolution(
        *,
        request_line_id: str,
        task_catalog_item_id: str | None,
        suggested_scope_code: str | None,
        diagnostic: str,
        line_position: int | None = None,
        task: ApprovalTaskRecord | None = None,
        approval_scope_candidates: Sequence[ApprovalScopeRecord] = (),
    ) -> RequestLineApprovalResolution:
        return RequestLineApprovalResolution(
            request_line_id=request_line_id,
            task_catalog_item_id=task_catalog_item_id,
            suggested_scope_code=suggested_scope_code,
            resolution=ApprovalLineResolution(
                approval_scope_id=None,
                eligible_approvers=(),
                diagnostics=(diagnostic,),
                blocked=True,
            ),
            line_position=line_position,
            task_code=task.code if task is not None else None,
            task_label=task.label if task is not None else None,
            effective_resource_class=(
                task.resource_class_code if task is not None else None
            ),
            approval_scope_candidates=tuple(approval_scope_candidates),
        )


    def _resolve_asset_request_line(
        self,
        line: ApprovalRequestLineRecord,
    ) -> RequestLineApprovalResolution:
        asset_type_id = str(line.asset_type_id or "").strip() or None
        asset_type = (
            call_application_port(
                lambda: self._repository.get_asset_type(asset_type_id),
                code_prefix="approval_routing_asset_type_read",
                context={"asset_type_id": asset_type_id},
            )
            if asset_type_id is not None
            else None
        )
        scopes = (
            call_application_port(
                lambda: self._repository.list_asset_type_scopes(asset_type_id),
                code_prefix="approval_routing_scope_read",
                context={"asset_type_id": asset_type_id},
            )
            if asset_type_id is not None and asset_type is not None
            else ()
        )
        proposed_asset_id = str(line.proposed_asset_id or "").strip() or None
        proposed_asset = (
            call_application_port(
                lambda: self._repository.get_asset(proposed_asset_id),
                code_prefix="approval_routing_asset_read",
                context={"proposed_asset_id": proposed_asset_id},
            )
            if proposed_asset_id is not None
            else None
        )

        scope_approver_ids: tuple[str, ...] = ()
        if len(scopes) == 1:
            scope_approver_ids = call_application_port(
                lambda: self._repository.list_scope_approver_ids(scopes[0].id),
                code_prefix="approval_routing_approver_read",
                context={"approval_scope_id": scopes[0].id},
            )
        asset_approver_ids: tuple[str, ...] = ()
        if proposed_asset is not None:
            asset_approver_ids = call_application_port(
                lambda: self._repository.list_asset_approver_ids(proposed_asset.id),
                code_prefix="approval_routing_asset_approver_read",
                context={"proposed_asset_id": proposed_asset.id},
            )

        all_user_ids = tuple(
            dict.fromkeys(tuple(scope_approver_ids) + tuple(asset_approver_ids))
        )
        user_rows = (
            call_application_port(
                lambda: self._repository.get_users(all_user_ids),
                code_prefix="approval_routing_user_read",
            )
            if all_user_ids
            else ()
        )
        users = {
            row.user_id: ApprovalRoutingUser(
                user_id=row.user_id,
                active=row.active,
                permissions=row.permissions,
            )
            for row in user_rows
        }
        resolution = resolve_asset_line_approvers(
            line_active=line.active,
            asset_type_id=asset_type_id,
            asset_type_exists=asset_type is not None,
            asset_type_active=bool(asset_type.active) if asset_type is not None else False,
            scope_candidates=tuple(
                ApprovalScopeCandidate(scope_id=scope.id, active=scope.active)
                for scope in scopes
            ),
            scope_approver_user_ids=scope_approver_ids,
            users=users,
            proposed_asset_id=proposed_asset_id,
            proposed_asset_exists=proposed_asset is not None,
            proposed_asset_active=(
                bool(proposed_asset.active) if proposed_asset is not None else False
            ),
            proposed_asset_type_matches=(
                proposed_asset is None
                or asset_type_id is None
                or proposed_asset.asset_type_id == asset_type_id
            ),
            asset_approver_user_ids=asset_approver_ids,
        )
        return RequestLineApprovalResolution(
            request_line_id=line.id,
            task_catalog_item_id=line.task_catalog_item_id,
            suggested_scope_code=None,
            resolution=resolution,
            line_position=line.position,
            task_code=line.erp_task_code,
            task_label=line.erp_task_label,
            approval_scope_candidates=tuple(scopes),
            line_kind=line.kind,
            asset_type_id=asset_type_id,
            asset_type_code=asset_type.code if asset_type is not None else None,
            asset_type_label=asset_type.label if asset_type is not None else None,
            proposed_asset_id=proposed_asset_id,
            proposed_asset_code=(
                proposed_asset.code if proposed_asset is not None else None
            ),
            proposed_asset_label=(
                proposed_asset.label if proposed_asset is not None else None
            ),
        )

    def resolve_request_line(
        self,
        line_id: str,
        *,
        resource_approver_user_ids: Sequence[str] = (),
    ) -> RequestLineApprovalResolution:
        identifier = _required(line_id, "request_line_id")
        line = call_application_port(
            lambda: self._repository.get_request_line(identifier),
            code_prefix="approval_routing_line_read",
            context={"request_line_id": identifier},
        )
        if line is None:
            raise ApplicationNotFoundError(
                "Ligne de demande introuvable.",
                code="approval_routing_line_not_found",
                context={"request_line_id": identifier},
            )
        if str(line.kind or "").strip().upper() == "ASSET":
            return self._resolve_asset_request_line(line)

        task_id = str(line.task_catalog_item_id or "").strip() or None
        task = (
            call_application_port(
                lambda: self._repository.get_task(task_id),
                code_prefix="approval_routing_task_read",
                context={"task_catalog_item_id": task_id},
            )
            if task_id is not None
            else None
        )
        task_scopes = (
            call_application_port(
                lambda: self._repository.list_task_scopes(task_id),
                code_prefix="approval_routing_scope_read",
                context={"task_catalog_item_id": task_id},
            )
            if task_id is not None and task is not None
            else ()
        )

        scopes: tuple[ApprovalScopeRecord, ...] = ()
        if task is not None and task.active and line.active:
            active_task_scopes = tuple(
                scope for scope in task_scopes if scope.active
            )
            if len(active_task_scopes) == 1:
                scopes = active_task_scopes
            elif active_task_scopes:
                scopes = active_task_scopes
            elif task_scopes:
                scopes = task_scopes
            else:
                class_code = str(task.resource_class_code or "").strip()
                suggested = suggested_approval_scope_code(task.code)
                if not class_code:
                    return self._blocked_resolution(
                        request_line_id=identifier,
                        task_catalog_item_id=task_id,
                        suggested_scope_code=suggested,
                        diagnostic=DIAGNOSTIC_RESOURCE_CLASS_MISSING,
                        line_position=line.position,
                        task=task,
                    )
                resource_class = call_application_port(
                    lambda: self._repository.get_resource_class(class_code),
                    code_prefix="approval_routing_resource_class_read",
                    context={"resource_class_code": class_code},
                )
                if resource_class is None:
                    return self._blocked_resolution(
                        request_line_id=identifier,
                        task_catalog_item_id=task_id,
                        suggested_scope_code=suggested,
                        diagnostic=DIAGNOSTIC_RESOURCE_CLASS_NOT_FOUND,
                        line_position=line.position,
                        task=task,
                    )
                if not resource_class.active:
                    return self._blocked_resolution(
                        request_line_id=identifier,
                        task_catalog_item_id=task_id,
                        suggested_scope_code=suggested,
                        diagnostic=DIAGNOSTIC_RESOURCE_CLASS_INACTIVE,
                        line_position=line.position,
                        task=task,
                    )
                class_scopes = call_application_port(
                    lambda: self._repository.list_resource_class_scopes(
                        class_code
                    ),
                    code_prefix="approval_routing_scope_read",
                    context={"resource_class_code": class_code},
                )
                active_class_scopes = tuple(
                    scope for scope in class_scopes if scope.active
                )
                if len(active_class_scopes) == 1:
                    scopes = active_class_scopes
                elif active_class_scopes:
                    scopes = active_class_scopes
                elif class_scopes:
                    scopes = class_scopes

        scope_approver_ids: tuple[str, ...] = ()
        if len(scopes) == 1:
            scope_approver_ids = call_application_port(
                lambda: self._repository.list_scope_approver_ids(scopes[0].id),
                code_prefix="approval_routing_approver_read",
                context={"approval_scope_id": scopes[0].id},
            )
        all_user_ids = tuple(
            dict.fromkeys(
                tuple(scope_approver_ids)
                + tuple(
                    str(value or "").strip()
                    for value in resource_approver_user_ids
                    if str(value or "").strip()
                )
            )
        )
        user_rows = (
            call_application_port(
                lambda: self._repository.get_users(all_user_ids),
                code_prefix="approval_routing_user_read",
            )
            if all_user_ids
            else ()
        )
        users = {
            row.user_id: ApprovalRoutingUser(
                user_id=row.user_id,
                active=row.active,
                permissions=row.permissions,
            )
            for row in user_rows
        }
        resolution = resolve_line_approvers(
            line_active=line.active,
            task_catalog_item_id=task_id,
            task_exists=task is not None,
            task_active=bool(task.active) if task is not None else False,
            scope_candidates=tuple(
                ApprovalScopeCandidate(scope_id=scope.id, active=scope.active)
                for scope in scopes
            ),
            scope_approver_user_ids=scope_approver_ids,
            users=users,
            resource_approver_user_ids=resource_approver_user_ids,
        )
        return RequestLineApprovalResolution(
            request_line_id=identifier,
            task_catalog_item_id=task_id,
            suggested_scope_code=(
                suggested_approval_scope_code(task.code)
                if task is not None
                else None
            ),
            resolution=resolution,
            line_position=line.position,
            task_code=(
                task.code if task is not None else line.erp_task_code
            ),
            task_label=(
                task.label if task is not None else line.erp_task_label
            ),
            effective_resource_class=(
                task.resource_class_code if task is not None else None
            ),
            approval_scope_candidates=tuple(scopes),
        )
