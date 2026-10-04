from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...application.errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
)
from ...application.idempotency import normalize_idempotency_key, request_fingerprint
from ...application.operational_responsibility_mutations import (
    OperationalResponsibilityMutationPort,
    OperationalResponsibilityOverrideMutationResult,
)
from .business_contact_models import BusinessContact
from .command_adapters import SqlPlanningCommandAdapter
from .idempotency import SqlCommandIdempotencyAdapter
from .models import Project, ResourceRequirement, Shift
from .planning_audit import ENTITY_SHIFT, SqlPlanningAuditJournal
from .planning_version import SqlPlanningMutationVersionRepository


ENTITY_PROJECT = "PROJECT"
ENTITY_SEGMENT = "SEGMENT"


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


def _payload(
    result: OperationalResponsibilityOverrideMutationResult,
) -> dict[str, Any]:
    return {
        "entity_type": result.entity_type,
        "entity_id": result.entity_id,
        "override_contact_id": result.override_contact_id,
        "project_override_version": result.project_override_version,
        "planning_version": result.planning_version,
        "auto_source_converted": result.auto_source_converted,
    }


def _result(
    payload: Mapping[str, Any],
) -> OperationalResponsibilityOverrideMutationResult:
    project_version = payload.get("project_override_version")
    planning_version = payload.get("planning_version")
    return OperationalResponsibilityOverrideMutationResult(
        entity_type=_text(payload.get("entity_type")),
        entity_id=_text(payload.get("entity_id")),
        override_contact_id=_optional_text(payload.get("override_contact_id")),
        project_override_version=(
            int(project_version) if project_version is not None else None
        ),
        planning_version=(
            int(planning_version) if planning_version is not None else None
        ),
        auto_source_converted=bool(payload.get("auto_source_converted")),
    )


class SqlOperationalResponsibilityMutationRepository(
    OperationalResponsibilityMutationPort
):
    """Persist explicit responsibility overrides under their canonical concurrency guards.

    Project configuration owns its dedicated local version. Requirement and Shift
    mutations share ADR-006 planning_version. The caller owns the outer transaction;
    every command runs in a SAVEPOINT so CAS, mutation, audit and idempotency receipt
    roll back together on any failure.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._versioning = SqlPlanningMutationVersionRepository(session)
        self._planning = SqlPlanningCommandAdapter(
            session,
            versioning=self._versioning,
        )

    @staticmethod
    def _identifier(value: object, *, field: str) -> str:
        identifier = _text(value)
        if not identifier:
            raise ApplicationValidationError(
                f"{field} est requis.",
                code="operational_responsibility_identifier_required",
                context={"field": field},
            )
        return identifier

    @staticmethod
    def _actor(value: object) -> str:
        actor = _text(value)
        if not actor:
            raise ApplicationAuthorizationError(
                "Une identité authentifiée est requise pour modifier le responsable opérationnel.",
                code="operational_responsibility_actor_required",
            )
        return actor

    def _validated_contact_id(self, value: object) -> str | None:
        contact_id = _optional_text(value)
        if contact_id is None:
            return None
        contact = self._session.get(BusinessContact, contact_id)
        if contact is None:
            raise ApplicationNotFoundError(
                "Le contact de responsable opérationnel est introuvable.",
                code="operational_responsibility_contact_not_found",
                context={"business_contact_id": contact_id},
            )
        if not bool(contact.active):
            raise ApplicationValidationError(
                "Le contact de responsable opérationnel est inactif.",
                code="operational_responsibility_contact_inactive",
                context={"business_contact_id": contact_id},
            )
        return contact_id

    def _acquire_project_version(
        self,
        project_id: str,
        expected_version: int,
    ) -> int:
        try:
            expected = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ApplicationValidationError(
                "La version attendue du responsable opérationnel projet est invalide.",
                code="project_operational_responsibility_version_invalid",
            ) from exc
        if expected < 1:
            raise ApplicationValidationError(
                "La version attendue du responsable opérationnel projet doit être positive.",
                code="project_operational_responsibility_version_invalid",
                context={"expected_version": expected},
            )

        changed = self._session.execute(
            update(Project)
            .where(
                Project.id == project_id,
                Project.operational_responsible_override_version == expected,
            )
            .values(
                operational_responsible_override_version=(
                    Project.operational_responsible_override_version + 1
                )
            )
        )
        if int(changed.rowcount or 0) == 1:
            self._session.flush()
            return expected + 1

        current = self._session.scalar(
            select(Project.operational_responsible_override_version).where(
                Project.id == project_id
            )
        )
        if current is None:
            raise ApplicationNotFoundError(
                "Projet introuvable.",
                code="project_not_found",
                context={"project_id": project_id},
            )
        raise ApplicationConflictError(
            "Le responsable opérationnel du projet a été modifié depuis sa lecture.",
            code="project_operational_responsibility_version_conflict",
            context={
                "project_id": project_id,
                "expected_version": expected,
                "current_version": int(current),
            },
        )

    def _execute(
        self,
        *,
        scope: str,
        actor_user_id: str,
        idempotency_key: str | None,
        request_payload: Mapping[str, Any],
        action: Callable[[], OperationalResponsibilityOverrideMutationResult],
    ) -> OperationalResponsibilityOverrideMutationResult:
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
            action=lambda: _payload(action()),
        )
        return _result(payload)

    def _set_project_override(
        self,
        *,
        project_id: str,
        contact_id: str | None,
        actor_user_id: str,
        expected_version: int,
    ) -> OperationalResponsibilityOverrideMutationResult:
        resulting_version = self._acquire_project_version(
            project_id,
            expected_version,
        )
        project = self._session.get(Project, project_id)
        if project is None:
            raise ApplicationNotFoundError(
                "Projet introuvable.",
                code="project_not_found",
                context={"project_id": project_id},
            )

        previous_contact_id = project.operational_responsible_override_contact_id
        wanted_contact_id = self._validated_contact_id(contact_id)
        before = {
            "operational_responsible_override_contact_id": previous_contact_id,
            "operational_responsible_override_version": int(expected_version),
        }
        project.operational_responsible_override_contact_id = wanted_contact_id
        self._session.flush()
        after = {
            "operational_responsible_override_contact_id": (
                project.operational_responsible_override_contact_id
            ),
            "operational_responsible_override_version": resulting_version,
        }
        SqlPlanningAuditJournal(
            self._session,
            actor_name=actor_user_id,
        ).append(
            entity_type=ENTITY_PROJECT,
            entity_id=project.id,
            entity_reference=project.number,
            action="Modification responsable opérationnel projet",
            before=before,
            after=after,
        )
        return OperationalResponsibilityOverrideMutationResult(
            entity_type=ENTITY_PROJECT,
            entity_id=project.id,
            override_contact_id=project.operational_responsible_override_contact_id,
            project_override_version=resulting_version,
        )

    def set_project_override(
        self,
        project_id: str,
        contact_id: str | None,
        *,
        actor_user_id: str,
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> OperationalResponsibilityOverrideMutationResult:
        project = self._identifier(project_id, field="project_id")
        actor = self._actor(actor_user_id)
        request_payload = {
            "project_id": project,
            "contact_id": _optional_text(contact_id),
            "expected_version": int(expected_version),
        }
        return self._execute(
            scope="operational_responsibility.project_override",
            actor_user_id=actor,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            action=lambda: self._set_project_override(
                project_id=project,
                contact_id=contact_id,
                actor_user_id=actor,
                expected_version=expected_version,
            ),
        )

    def _set_requirement_override(
        self,
        *,
        requirement_id: str,
        contact_id: str | None,
        actor_user_id: str,
        expected_planning_version: int,
    ) -> OperationalResponsibilityOverrideMutationResult:
        planning_version = self._versioning.acquire(expected_planning_version)
        requirement = self._session.get(ResourceRequirement, requirement_id)
        if requirement is None:
            raise ApplicationNotFoundError(
                "Segment ou besoin introuvable.",
                code="resource_requirement_not_found",
                context={"requirement_id": requirement_id},
            )
        if requirement.status in {"Annulé", "Terminé"}:
            raise ApplicationConflictError(
                "Le responsable opérationnel d'un segment inactif ne peut pas être modifié.",
                code="resource_requirement_inactive",
                context={
                    "requirement_id": requirement_id,
                    "status": requirement.status,
                },
            )

        wanted_contact_id = self._validated_contact_id(contact_id)
        before = {
            "operational_responsible_override_contact_id": (
                requirement.operational_responsible_override_contact_id
            )
        }
        requirement.operational_responsible_override_contact_id = wanted_contact_id
        self._session.flush()
        after = {
            "operational_responsible_override_contact_id": (
                requirement.operational_responsible_override_contact_id
            ),
            "expected_planning_version": int(expected_planning_version),
            "planning_version": int(planning_version),
        }
        reference = _text(requirement.legacy_segment_id) or requirement.id
        SqlPlanningAuditJournal(
            self._session,
            actor_name=actor_user_id,
        ).append(
            entity_type=ENTITY_SEGMENT,
            entity_id=requirement.id,
            entity_reference=reference,
            action="Modification responsable opérationnel segment",
            before=before,
            after=after,
        )
        return OperationalResponsibilityOverrideMutationResult(
            entity_type=ENTITY_SEGMENT,
            entity_id=requirement.id,
            override_contact_id=requirement.operational_responsible_override_contact_id,
            planning_version=int(planning_version),
        )

    def set_requirement_override(
        self,
        requirement_id: str,
        contact_id: str | None,
        *,
        actor_user_id: str,
        expected_planning_version: int,
        idempotency_key: str | None = None,
    ) -> OperationalResponsibilityOverrideMutationResult:
        requirement = self._identifier(requirement_id, field="requirement_id")
        actor = self._actor(actor_user_id)
        request_payload = {
            "requirement_id": requirement,
            "contact_id": _optional_text(contact_id),
            "expected_planning_version": int(expected_planning_version),
        }
        return self._execute(
            scope="operational_responsibility.requirement_override",
            actor_user_id=actor,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            action=lambda: self._set_requirement_override(
                requirement_id=requirement,
                contact_id=contact_id,
                actor_user_id=actor,
                expected_planning_version=expected_planning_version,
            ),
        )

    def _set_shift_override(
        self,
        *,
        shift_id: str,
        contact_id: str | None,
        actor_user_id: str,
        expected_planning_version: int,
    ) -> OperationalResponsibilityOverrideMutationResult:
        planning_version = self._versioning.acquire(expected_planning_version)
        shift = self._session.get(Shift, shift_id)
        if shift is None:
            raise ApplicationNotFoundError(
                "Quart introuvable.",
                code="shift_not_found",
                context={"shift_id": shift_id},
            )

        wanted_contact_id = self._validated_contact_id(contact_id)
        previous_source = _text(shift.source).upper()
        previous_locked = bool(shift.locked)
        before = {
            "operational_responsible_override_contact_id": (
                shift.operational_responsible_override_contact_id
            ),
            "source": shift.source,
            "locked": previous_locked,
        }

        shift.operational_responsible_override_contact_id = wanted_contact_id
        stabilized_manual = wanted_contact_id is not None and (
            previous_source != "MANUAL" or not previous_locked
        )
        auto_source_converted = wanted_contact_id is not None and previous_source == "AUTO"
        if wanted_contact_id is not None:
            shift.source = "MANUAL"
            shift.locked = True

        self._session.flush()
        if stabilized_manual:
            self._planning.rebuild()

        persisted = self._session.get(Shift, shift.id)
        if persisted is None:
            raise RuntimeError(
                "Le recalcul a perdu le quart portant le responsable opérationnel."
            )
        if wanted_contact_id is not None and (
            persisted.operational_responsible_override_contact_id != wanted_contact_id
            or _text(persisted.source).upper() != "MANUAL"
            or not bool(persisted.locked)
        ):
            raise RuntimeError(
                "Le quart n'a pas conservé sa décision manuelle de responsable opérationnel."
            )

        after = {
            "operational_responsible_override_contact_id": (
                persisted.operational_responsible_override_contact_id
            ),
            "source": persisted.source,
            "locked": bool(persisted.locked),
            "expected_planning_version": int(expected_planning_version),
            "planning_version": int(planning_version),
            "auto_source_converted": auto_source_converted,
        }
        reference = _text(persisted.legacy_allocation_id) or persisted.id
        SqlPlanningAuditJournal(
            self._session,
            actor_name=actor_user_id,
        ).append(
            entity_type=ENTITY_SHIFT,
            entity_id=persisted.id,
            entity_reference=reference,
            parent_reference=persisted.resource_requirement_id,
            action="Modification responsable opérationnel quart",
            before=before,
            after=after,
        )
        return OperationalResponsibilityOverrideMutationResult(
            entity_type=ENTITY_SHIFT,
            entity_id=persisted.id,
            override_contact_id=persisted.operational_responsible_override_contact_id,
            planning_version=int(planning_version),
            auto_source_converted=auto_source_converted,
        )

    def set_shift_override(
        self,
        shift_id: str,
        contact_id: str | None,
        *,
        actor_user_id: str,
        expected_planning_version: int,
        idempotency_key: str | None = None,
    ) -> OperationalResponsibilityOverrideMutationResult:
        shift = self._identifier(shift_id, field="shift_id")
        actor = self._actor(actor_user_id)
        request_payload = {
            "shift_id": shift,
            "contact_id": _optional_text(contact_id),
            "expected_planning_version": int(expected_planning_version),
        }
        return self._execute(
            scope="operational_responsibility.shift_override",
            actor_user_id=actor,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            action=lambda: self._set_shift_override(
                shift_id=shift,
                contact_id=contact_id,
                actor_user_id=actor,
                expected_planning_version=expected_planning_version,
            ),
        )
