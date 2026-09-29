from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ...application.approval_scopes import (
    ApprovalRequestLineRecord,
    ApprovalResourceClassRecord,
    ApprovalScopeRecord,
    ApprovalScopeRepositoryPort,
    ApprovalTaskRecord,
    ApprovalUserRecord,
)
from ...application.errors import ApplicationConflictError
from ...application.security import normalize_roles, permissions_for_roles
from .approval_scope_models import (
    ApprovalScope,
    ApprovalScopeApprover,
    ResourceClassApprovalScopeMapping,
    TaskApprovalScopeMapping,
)
from .base import new_id
from .identity_models import AppUser
from .models import RequestLine, TaskCatalogEntry
from .resource_class_models import ResourceClassConfig


def _text(value: object) -> str:
    return str(value or "").strip()


class SqlApprovalScopeRepository(ApprovalScopeRepositoryPort):
    def __init__(self, session: Session) -> None:
        self._session = session

    def _record(self, row: ApprovalScope) -> ApprovalScopeRecord:
        approvers = tuple(
            self._session.scalars(
                select(ApprovalScopeApprover.app_user_id)
                .where(ApprovalScopeApprover.approval_scope_id == row.id)
                .order_by(ApprovalScopeApprover.app_user_id)
            ).all()
        )
        resource_classes = tuple(
            self._session.scalars(
                select(ResourceClassApprovalScopeMapping.resource_class_code)
                .where(
                    ResourceClassApprovalScopeMapping.approval_scope_id
                    == row.id
                )
                .order_by(
                    ResourceClassApprovalScopeMapping.resource_class_code
                )
            ).all()
        )
        tasks = tuple(
            self._session.scalars(
                select(TaskApprovalScopeMapping.task_catalog_item_id)
                .where(TaskApprovalScopeMapping.approval_scope_id == row.id)
                .order_by(TaskApprovalScopeMapping.task_catalog_item_id)
            ).all()
        )
        return ApprovalScopeRecord(
            id=row.id,
            code=row.code,
            label=row.label,
            active=bool(row.active),
            version=int(row.version or 1),
            approver_user_ids=approvers,
            resource_class_codes=resource_classes,
            task_catalog_item_ids=tasks,
        )

    def list_scopes(self) -> tuple[ApprovalScopeRecord, ...]:
        rows = self._session.scalars(
            select(ApprovalScope).order_by(ApprovalScope.code, ApprovalScope.id)
        ).all()
        return tuple(self._record(row) for row in rows)

    def get_scope(self, scope_id: str) -> ApprovalScopeRecord | None:
        row = self._session.get(ApprovalScope, _text(scope_id))
        return self._record(row) if row is not None else None

    def find_scope_by_code(self, code: str) -> ApprovalScopeRecord | None:
        row = self._session.scalar(
            select(ApprovalScope).where(ApprovalScope.code == _text(code).upper())
        )
        return self._record(row) if row is not None else None

    def create_scope(
        self,
        *,
        code: str,
        label: str,
        active: bool,
    ) -> ApprovalScopeRecord:
        row = ApprovalScope(
            id=new_id(),
            code=_text(code).upper(),
            label=_text(label),
            active=bool(active),
            version=1,
        )
        self._session.add(row)
        self._session.flush()
        return self._record(row)

    def _acquire_scope_version(
        self,
        scope_id: str,
        expected_version: int,
    ) -> ApprovalScope:
        identifier = _text(scope_id)
        current = self._session.get(ApprovalScope, identifier)
        if current is None:
            raise KeyError(
                f"Périmètre d'approbation {identifier} introuvable"
            )
        expected = int(expected_version)
        result = self._session.execute(
            update(ApprovalScope)
            .where(
                ApprovalScope.id == identifier,
                ApprovalScope.version == expected,
            )
            .values(version=expected + 1)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            refreshed = self._session.get(ApprovalScope, identifier)
            raise ApplicationConflictError(
                "Le périmètre d'approbation a été modifié depuis sa lecture.",
                code="approval_scope_version_conflict",
                context={
                    "approval_scope_id": identifier,
                    "expected_version": expected,
                    "current_version": (
                        int(refreshed.version or 1)
                        if refreshed is not None
                        else None
                    ),
                },
            )
        self._session.flush()
        self._session.expire(current)
        return current

    def update_scope(
        self,
        scope_id: str,
        *,
        values: Mapping[str, object],
        expected_version: int,
    ) -> ApprovalScopeRecord:
        row = self._acquire_scope_version(scope_id, expected_version)
        if "label" in values:
            row.label = _text(values["label"])
        if "active" in values:
            row.active = bool(values["active"])
        self._session.flush()
        return self._record(row)

    def set_scope_approver(
        self,
        scope_id: str,
        user_id: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord:
        row = self._acquire_scope_version(scope_id, expected_version)
        user_identifier = _text(user_id)
        if self._session.get(AppUser, user_identifier) is None:
            raise KeyError(f"Utilisateur {user_identifier} introuvable")
        if assigned:
            existing = self._session.get(
                ApprovalScopeApprover,
                (row.id, user_identifier),
            )
            if existing is None:
                self._session.add(
                    ApprovalScopeApprover(
                        approval_scope_id=row.id,
                        app_user_id=user_identifier,
                    )
                )
        else:
            self._session.execute(
                delete(ApprovalScopeApprover).where(
                    ApprovalScopeApprover.approval_scope_id == row.id,
                    ApprovalScopeApprover.app_user_id == user_identifier,
                )
            )
        self._session.flush()
        return self._record(row)

    def set_task_scope(
        self,
        scope_id: str,
        task_catalog_item_id: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord:
        row = self._acquire_scope_version(scope_id, expected_version)
        task_id = _text(task_catalog_item_id)
        if self._session.get(TaskCatalogEntry, task_id) is None:
            raise KeyError(f"Tâche {task_id} introuvable")
        if assigned:
            existing = self._session.get(
                TaskApprovalScopeMapping,
                (task_id, row.id),
            )
            if existing is None:
                self._session.add(
                    TaskApprovalScopeMapping(
                        task_catalog_item_id=task_id,
                        approval_scope_id=row.id,
                    )
                )
        else:
            self._session.execute(
                delete(TaskApprovalScopeMapping).where(
                    TaskApprovalScopeMapping.task_catalog_item_id == task_id,
                    TaskApprovalScopeMapping.approval_scope_id == row.id,
                )
            )
        self._session.flush()
        return self._record(row)

    def set_resource_class_scope(
        self,
        scope_id: str,
        resource_class_code: str,
        *,
        assigned: bool,
        expected_version: int,
    ) -> ApprovalScopeRecord:
        row = self._acquire_scope_version(scope_id, expected_version)
        class_code = _text(resource_class_code)
        if self._session.get(ResourceClassConfig, class_code) is None:
            raise KeyError(f"Classe {class_code} introuvable")
        if assigned:
            existing = self._session.get(
                ResourceClassApprovalScopeMapping,
                (class_code, row.id),
            )
            if existing is None:
                self._session.add(
                    ResourceClassApprovalScopeMapping(
                        resource_class_code=class_code,
                        approval_scope_id=row.id,
                    )
                )
        else:
            self._session.execute(
                delete(ResourceClassApprovalScopeMapping).where(
                    ResourceClassApprovalScopeMapping.resource_class_code
                    == class_code,
                    ResourceClassApprovalScopeMapping.approval_scope_id
                    == row.id,
                )
            )
        self._session.flush()
        return self._record(row)

    def get_request_line(
        self,
        line_id: str,
    ) -> ApprovalRequestLineRecord | None:
        row = self._session.get(RequestLine, _text(line_id))
        if row is None:
            return None
        return ApprovalRequestLineRecord(
            id=row.id,
            active=bool(row.active),
            task_catalog_item_id=row.task_catalog_item_id,
        )

    def get_task(self, task_id: str) -> ApprovalTaskRecord | None:
        row = self._session.get(TaskCatalogEntry, _text(task_id))
        if row is None:
            return None
        return ApprovalTaskRecord(
            id=row.id,
            code=row.task_code,
            active=bool(row.active),
            resource_class_code=_text(row.resource_class_code) or None,
        )

    def get_resource_class(
        self,
        class_code: str,
    ) -> ApprovalResourceClassRecord | None:
        row = self._session.get(ResourceClassConfig, _text(class_code))
        if row is None:
            return None
        return ApprovalResourceClassRecord(
            code=row.code,
            active=bool(row.active),
        )

    def list_task_scopes(
        self,
        task_id: str,
    ) -> tuple[ApprovalScopeRecord, ...]:
        rows = self._session.scalars(
            select(ApprovalScope)
            .join(
                TaskApprovalScopeMapping,
                TaskApprovalScopeMapping.approval_scope_id
                == ApprovalScope.id,
            )
            .where(
                TaskApprovalScopeMapping.task_catalog_item_id
                == _text(task_id)
            )
            .order_by(ApprovalScope.code, ApprovalScope.id)
        ).all()
        return tuple(self._record(row) for row in rows)

    def list_resource_class_scopes(
        self,
        class_code: str,
    ) -> tuple[ApprovalScopeRecord, ...]:
        rows = self._session.scalars(
            select(ApprovalScope)
            .join(
                ResourceClassApprovalScopeMapping,
                ResourceClassApprovalScopeMapping.approval_scope_id
                == ApprovalScope.id,
            )
            .where(
                ResourceClassApprovalScopeMapping.resource_class_code
                == _text(class_code)
            )
            .order_by(ApprovalScope.code, ApprovalScope.id)
        ).all()
        return tuple(self._record(row) for row in rows)

    def list_scope_approver_ids(self, scope_id: str) -> tuple[str, ...]:
        return tuple(
            self._session.scalars(
                select(ApprovalScopeApprover.app_user_id)
                .where(
                    ApprovalScopeApprover.approval_scope_id
                    == _text(scope_id)
                )
                .order_by(ApprovalScopeApprover.app_user_id)
            ).all()
        )

    @staticmethod
    def _user_record(row: AppUser) -> ApprovalUserRecord:
        roles = normalize_roles(
            tuple(
                str(value)
                for value in json.loads(row.roles_json or "[]")
            )
        )
        return ApprovalUserRecord(
            user_id=row.id,
            active=bool(row.active),
            permissions=permissions_for_roles(roles),
        )

    def get_users(
        self,
        user_ids: Sequence[str],
    ) -> tuple[ApprovalUserRecord, ...]:
        identifiers = tuple(
            dict.fromkeys(
                _text(value)
                for value in user_ids
                if _text(value)
            )
        )
        if not identifiers:
            return ()
        rows = self._session.scalars(
            select(AppUser)
            .where(AppUser.id.in_(identifiers))
            .order_by(AppUser.id)
        ).all()
        return tuple(self._user_record(row) for row in rows)

    def get_user(self, user_id: str) -> ApprovalUserRecord | None:
        row = self._session.get(AppUser, _text(user_id))
        return self._user_record(row) if row is not None else None
