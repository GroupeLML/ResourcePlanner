from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...application.errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
)
from ...application.idempotency import normalize_idempotency_key, request_fingerprint
from ...application.project_co_managers import (
    PROJECT_CO_MANAGER_ADDED,
    PROJECT_CO_MANAGER_REMOVED,
    ProjectCoManagerMutationResult,
    ProjectCoManagerRecord,
    ProjectCoManagerRepositoryPort,
)
from .business_contact_models import BusinessContact
from .idempotency import SqlCommandIdempotencyAdapter
from .identity_models import AppUser
from .models import Project
from .project_manager_models import (
    PROJECT_MANAGER_SOURCE_RP,
    ProjectCoManager,
    ProjectManagerAudit,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _snapshot(*, business_contact_id: str, assigned: bool) -> dict[str, object]:
    return {
        "business_contact_id": business_contact_id,
        "assigned": bool(assigned),
    }


def _result_payload(result: ProjectCoManagerMutationResult) -> dict[str, Any]:
    return {
        "project_id": result.project_id,
        "business_contact_id": result.business_contact_id,
        "version": result.version,
        "action": result.action,
    }


def _result_from_payload(payload: Mapping[str, Any]) -> ProjectCoManagerMutationResult:
    return ProjectCoManagerMutationResult(
        project_id=_text(payload.get("project_id")),
        business_contact_id=_text(payload.get("business_contact_id")),
        version=int(payload.get("version") or 0),
        action=_text(payload.get("action")),
    )


class SqlProjectCoManagerRepository(ProjectCoManagerRepositoryPort):
    """Persist project co-manager nominations inside the caller-owned transaction.

    Each mutation acquires the project's dedicated co-manager collection version with
    a SQL compare-and-swap before validating or changing the association. Unkeyed
    mutations run in a SAVEPOINT so a caller that catches a validation/conflict error
    cannot accidentally commit the version acquisition without the association/audit.
    Keyed mutations reuse the shared durable idempotency adapter, whose SAVEPOINT
    contains the CAS, business mutation, audit and receipt.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _normalize_id(value: object, *, field: str) -> str:
        normalized = _text(value)
        if not normalized:
            raise ApplicationValidationError(
                f"{field} est requis.",
                code="project_co_manager_identifier_required",
                context={"field": field},
            )
        return normalized

    def _require_actor(self, actor_user_id: str) -> AppUser:
        actor = self._session.get(AppUser, actor_user_id)
        if actor is None:
            raise ApplicationNotFoundError(
                "Utilisateur acteur introuvable.",
                code="project_co_manager_actor_not_found",
                context={"actor_user_id": actor_user_id},
            )
        return actor

    def _acquire_version(self, project_id: str, expected_version: int) -> int:
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ApplicationValidationError(
                "La version attendue des co-chargés est invalide.",
                code="project_co_managers_version_invalid",
            ) from exc
        if expected < 1:
            raise ApplicationValidationError(
                "La version attendue des co-chargés doit être positive.",
                code="project_co_managers_version_invalid",
                context={"expected_version": expected},
            )

        result = self._session.execute(
            update(Project)
            .where(
                Project.id == project_id,
                Project.co_managers_version == expected,
            )
            .values(co_managers_version=Project.co_managers_version + 1)
        )
        if int(result.rowcount or 0) == 1:
            return expected + 1

        current = self._session.scalar(
            select(Project.co_managers_version).where(Project.id == project_id)
        )
        if current is None:
            raise ApplicationNotFoundError(
                "Projet introuvable.",
                code="project_not_found",
                context={"project_id": project_id},
            )
        raise ApplicationConflictError(
            "Les co-chargés du projet ont été modifiés depuis leur lecture.",
            code="project_co_managers_version_conflict",
            context={
                "project_id": project_id,
                "expected_version": expected,
                "current_version": int(current),
            },
        )

    def _ensure_not_primary(
        self,
        *,
        project: Project,
        business_contact_id: str,
    ) -> None:
        principal_external_id = _text(project.project_manager_external_id)
        if not principal_external_id:
            return
        principal_user_id = self._session.scalar(
            select(AppUser.id).where(
                AppUser.employee_external_id == principal_external_id,
                AppUser.business_contact_id == business_contact_id,
            )
        )
        if principal_user_id is not None:
            raise ApplicationValidationError(
                "Le chargé principal ERP ne doit pas être persisté comme co-chargé.",
                code="project_co_manager_primary_forbidden",
                context={
                    "project_id": project.id,
                    "business_contact_id": business_contact_id,
                },
            )

    def _append_audit(
        self,
        *,
        project_id: str,
        business_contact_id: str,
        actor_user_id: str,
        action: str,
        resulting_version: int,
        before_assigned: bool,
        after_assigned: bool,
    ) -> None:
        self._session.add(
            ProjectManagerAudit(
                project_id=project_id,
                action=action,
                source=PROJECT_MANAGER_SOURCE_RP,
                actor_user_id=actor_user_id,
                before_json=json.dumps(
                    _snapshot(
                        business_contact_id=business_contact_id,
                        assigned=before_assigned,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                after_json=json.dumps(
                    _snapshot(
                        business_contact_id=business_contact_id,
                        assigned=after_assigned,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                resulting_version=resulting_version,
            )
        )

    def list_co_managers(self, project_id: str) -> tuple[ProjectCoManagerRecord, ...]:
        wanted_project_id = self._normalize_id(project_id, field="project_id")
        project_exists = self._session.scalar(
            select(Project.id).where(Project.id == wanted_project_id)
        )
        if project_exists is None:
            raise ApplicationNotFoundError(
                "Projet introuvable.",
                code="project_not_found",
                context={"project_id": wanted_project_id},
            )
        rows = self._session.scalars(
            select(ProjectCoManager)
            .where(ProjectCoManager.project_id == wanted_project_id)
            .order_by(
                ProjectCoManager.created_at,
                ProjectCoManager.business_contact_id,
            )
        ).all()
        return tuple(
            ProjectCoManagerRecord(
                project_id=row.project_id,
                business_contact_id=row.business_contact_id,
                created_at=row.created_at,
                created_by_user_id=row.created_by_user_id,
            )
            for row in rows
        )

    def _add(
        self,
        *,
        project_id: str,
        business_contact_id: str,
        actor_user_id: str,
        expected_version: int,
    ) -> ProjectCoManagerMutationResult:
        resulting_version = self._acquire_version(project_id, expected_version)
        self._require_actor(actor_user_id)
        project = self._session.get(Project, project_id)
        if project is None:
            raise ApplicationNotFoundError(
                "Projet introuvable.",
                code="project_not_found",
                context={"project_id": project_id},
            )
        contact = self._session.get(BusinessContact, business_contact_id)
        if contact is None:
            raise ApplicationNotFoundError(
                "Contact métier introuvable.",
                code="business_contact_not_found",
                context={"business_contact_id": business_contact_id},
            )
        if not contact.active:
            raise ApplicationValidationError(
                "Un contact métier inactif ne peut pas être nommé co-chargé.",
                code="project_co_manager_contact_inactive",
                context={"business_contact_id": business_contact_id},
            )
        if self._session.get(
            ProjectCoManager,
            (project_id, business_contact_id),
        ) is not None:
            raise ApplicationConflictError(
                "Ce contact est déjà co-chargé du projet.",
                code="project_co_manager_exists",
                context={
                    "project_id": project_id,
                    "business_contact_id": business_contact_id,
                },
            )
        self._ensure_not_primary(
            project=project,
            business_contact_id=business_contact_id,
        )

        self._session.add(
            ProjectCoManager(
                project_id=project_id,
                business_contact_id=business_contact_id,
                created_by_user_id=actor_user_id,
            )
        )
        self._append_audit(
            project_id=project_id,
            business_contact_id=business_contact_id,
            actor_user_id=actor_user_id,
            action=PROJECT_CO_MANAGER_ADDED,
            resulting_version=resulting_version,
            before_assigned=False,
            after_assigned=True,
        )
        self._session.flush()
        return ProjectCoManagerMutationResult(
            project_id=project_id,
            business_contact_id=business_contact_id,
            version=resulting_version,
            action=PROJECT_CO_MANAGER_ADDED,
        )

    def _remove(
        self,
        *,
        project_id: str,
        business_contact_id: str,
        actor_user_id: str,
        expected_version: int,
    ) -> ProjectCoManagerMutationResult:
        resulting_version = self._acquire_version(project_id, expected_version)
        self._require_actor(actor_user_id)
        row = self._session.get(
            ProjectCoManager,
            (project_id, business_contact_id),
        )
        if row is None:
            raise ApplicationNotFoundError(
                "Co-chargé introuvable sur ce projet.",
                code="project_co_manager_not_found",
                context={
                    "project_id": project_id,
                    "business_contact_id": business_contact_id,
                },
            )

        self._session.delete(row)
        self._append_audit(
            project_id=project_id,
            business_contact_id=business_contact_id,
            actor_user_id=actor_user_id,
            action=PROJECT_CO_MANAGER_REMOVED,
            resulting_version=resulting_version,
            before_assigned=True,
            after_assigned=False,
        )
        self._session.flush()
        return ProjectCoManagerMutationResult(
            project_id=project_id,
            business_contact_id=business_contact_id,
            version=resulting_version,
            action=PROJECT_CO_MANAGER_REMOVED,
        )

    def _execute(
        self,
        *,
        scope: str,
        idempotency_key: str | None,
        actor_user_id: str,
        request_payload: Mapping[str, Any],
        action: Callable[[], ProjectCoManagerMutationResult],
    ) -> ProjectCoManagerMutationResult:
        key = normalize_idempotency_key(idempotency_key)
        if key is None:
            with self._session.begin_nested():
                return action()

        payload = SqlCommandIdempotencyAdapter(
            self._session,
            actor_name=actor_user_id,
        ).replay_or_execute(
            scope=scope,
            key=key,
            request_fingerprint=request_fingerprint(request_payload),
            action=lambda: _result_payload(action()),
        )
        return _result_from_payload(payload)

    def add_co_manager(
        self,
        project_id: str,
        business_contact_id: str,
        *,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> ProjectCoManagerMutationResult:
        project = self._normalize_id(project_id, field="project_id")
        contact = self._normalize_id(
            business_contact_id,
            field="business_contact_id",
        )
        actor = self._normalize_id(actor_user_id, field="actor_user_id")
        request_payload = {
            "project_id": project,
            "business_contact_id": contact,
            "expected_version": int(expected_version),
        }
        return self._execute(
            scope="project_co_manager.add",
            idempotency_key=idempotency_key,
            actor_user_id=actor,
            request_payload=request_payload,
            action=lambda: self._add(
                project_id=project,
                business_contact_id=contact,
                actor_user_id=actor,
                expected_version=expected_version,
            ),
        )

    def remove_co_manager(
        self,
        project_id: str,
        business_contact_id: str,
        *,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> ProjectCoManagerMutationResult:
        project = self._normalize_id(project_id, field="project_id")
        contact = self._normalize_id(
            business_contact_id,
            field="business_contact_id",
        )
        actor = self._normalize_id(actor_user_id, field="actor_user_id")
        request_payload = {
            "project_id": project,
            "business_contact_id": contact,
            "expected_version": int(expected_version),
        }
        return self._execute(
            scope="project_co_manager.remove",
            idempotency_key=idempotency_key,
            actor_user_id=actor,
            request_payload=request_payload,
            action=lambda: self._remove(
                project_id=project,
                business_contact_id=contact,
                actor_user_id=actor,
                expected_version=expected_version,
            ),
        )
