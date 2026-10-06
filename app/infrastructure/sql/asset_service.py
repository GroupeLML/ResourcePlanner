"""Catalog and globally serialized physical asset reservations."""

from __future__ import annotations

from datetime import date
import hashlib
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...application.errors import (
    ApplicationAuthorizationError,
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
)
from ...application.security import (
    PERMISSION_ADMIN_SETTINGS,
    PERMISSION_APPROVE_DEMANDS,
    PERMISSION_MANAGE_PLANNING,
    normalize_roles,
    permissions_for_roles,
)
from ...domain.approval_routing import (
    ApprovalRoutingUser,
    ApprovalScopeCandidate,
    resolve_asset_line_approvers,
)
from ...domain.reservable_assets import (
    AssetOccupation,
    AssetRequirementOrigin,
    overlapping_asset_occupations,
)
from ...domain.approval_envelope import approval_envelope_from_snapshot_payload
from .approval_scope_models import (
    ApprovalScope,
    ApprovalScopeApprover,
    AssetTypeApprovalScopeMapping,
)
from .asset_models import (
    Asset,
    AssetAllocation,
    AssetApprover,
    AssetRequirement,
    AssetType,
    AssetTypeCompetency,
    AssetUnavailability,
)
from .asset_qualification import (
    QUALIFICATION_NO_OVERLAP,
    QUALIFICATION_POLICY_ANY_ASSIGNED_WORKFORCE,
    QUALIFICATION_SATISFIED,
    QUALIFICATION_SKILL_MISMATCH,
    eligible_operator_resources,
    evaluate_asset_qualification,
    required_competencies,
)
from .identity_models import AppUser
from .models import Competency, Project, Resource, ResourceCompetency, ResourceRequirement, Shift
from .approval_revision_models import RequestApprovalReference, RequestApprovalRevision
from .base import new_id
from .idempotency import SqlCommandIdempotencyAdapter
from .planning_audit import SqlPlanningAuditJournal
from .planning_version import SqlPlanningMutationVersionRepository
from .operational_choice_repository import SqlRequestOperationalChoiceRepository


class SqlAssetService:
    def __init__(self, session: Session, *, actor: str) -> None:
        self.session = session
        self.actor = actor
        self.version = SqlPlanningMutationVersionRepository(session)
        self.audit = SqlPlanningAuditJournal(session, actor_name=actor)

    @staticmethod
    def _user_permissions(user: AppUser) -> tuple[str, ...]:
        try:
            roles = normalize_roles(
                tuple(str(value) for value in json.loads(user.roles_json or "[]"))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        return permissions_for_roles(roles)

    def _require_actor_permission(
        self,
        permission: str,
        *,
        code: str,
        message: str,
    ) -> AppUser:
        actor_id = str(self.actor or "").strip()
        actor = self.session.get(AppUser, actor_id) if actor_id else None
        if (
            actor is None
            or not actor.active
            or permission not in self._user_permissions(actor)
        ):
            raise ApplicationAuthorizationError(
                message,
                code=code,
                context={"app_user_id": actor_id or None},
            )
        return actor

    def _require_assignment_authority(self, asset_id: str) -> dict[str, object]:
        actor = self._require_actor_permission(
            PERMISSION_MANAGE_PLANNING,
            code="asset_assignment_authority_required",
            message="L'attribution directe de cet actif n'est pas autorisée.",
        )
        asset = self.session.get(Asset, str(asset_id or "").strip())
        asset_type = (
            self.session.get(AssetType, asset.asset_type_id)
            if asset is not None
            else None
        )
        if asset is None:
            raise ApplicationNotFoundError(
                "Unité d'actif introuvable.",
                code="asset_not_found",
            )

        scopes = tuple(
            self.session.scalars(
                select(ApprovalScope)
                .join(
                    AssetTypeApprovalScopeMapping,
                    AssetTypeApprovalScopeMapping.approval_scope_id
                    == ApprovalScope.id,
                )
                .where(
                    AssetTypeApprovalScopeMapping.asset_type_id
                    == asset.asset_type_id
                )
                .order_by(ApprovalScope.id)
            ).all()
        )
        scope_ids = tuple(row.id for row in scopes)
        scope_approver_ids = (
            tuple(
                self.session.scalars(
                    select(ApprovalScopeApprover.app_user_id)
                    .where(
                        ApprovalScopeApprover.approval_scope_id.in_(scope_ids)
                    )
                    .order_by(ApprovalScopeApprover.app_user_id)
                ).all()
            )
            if scope_ids
            else ()
        )
        asset_approver_ids = tuple(
            self.session.scalars(
                select(AssetApprover.app_user_id)
                .where(AssetApprover.asset_id == asset.id)
                .order_by(AssetApprover.app_user_id)
            ).all()
        )
        user_ids = tuple(
            sorted(set(scope_approver_ids) | set(asset_approver_ids) | {actor.id})
        )
        users = {
            row.id: ApprovalRoutingUser(
                user_id=row.id,
                active=bool(row.active),
                permissions=self._user_permissions(row),
            )
            for row in self.session.scalars(
                select(AppUser).where(AppUser.id.in_(user_ids)).order_by(AppUser.id)
            ).all()
        }
        resolution = resolve_asset_line_approvers(
            line_active=True,
            asset_type_id=asset.asset_type_id,
            asset_type_exists=asset_type is not None,
            asset_type_active=bool(asset_type and asset_type.active),
            scope_candidates=tuple(
                ApprovalScopeCandidate(scope_id=row.id, active=bool(row.active))
                for row in scopes
            ),
            scope_approver_user_ids=scope_approver_ids,
            users=users,
            proposed_asset_id=asset.id,
            proposed_asset_exists=True,
            proposed_asset_active=bool(asset.active),
            proposed_asset_type_matches=bool(
                asset_type is not None and asset.asset_type_id == asset_type.id
            ),
            asset_approver_user_ids=asset_approver_ids,
        )
        eligible = next(
            (
                item
                for item in resolution.eligible_approvers
                if item.user_id == actor.id
            ),
            None,
        )
        if resolution.blocked or eligible is None:
            raise ApplicationAuthorizationError(
                "L'attribution directe de cet actif n'est pas autorisée.",
                code="asset_assignment_authority_required",
                context={
                    "asset_id": asset.id,
                    "approval_scope_id": resolution.approval_scope_id,
                    "diagnostics": list(resolution.diagnostics),
                },
            )
        return {
            "asset_id": asset.id,
            "app_user_id": actor.id,
            "approval_scope_id": resolution.approval_scope_id,
            "sources": list(eligible.sources),
        }

    def _require_assignment_authorities(
        self,
        *asset_ids: str | None,
    ) -> list[dict[str, object]]:
        normalized = sorted(
            {
                str(asset_id or "").strip()
                for asset_id in asset_ids
                if str(asset_id or "").strip()
            }
        )
        return [self._require_assignment_authority(asset_id) for asset_id in normalized]

    def create_type(self, *, code: str, label: str, category: str, metadata: dict | None = None) -> AssetType:
        if category not in {"VEHICLE", "EQUIPMENT", "TOOL", "WORKCENTER"}:
            raise ApplicationValidationError("Catégorie d'actif invalide.", code="asset_category_invalid")
        if not code.strip() or not label.strip():
            raise ApplicationValidationError("Code et libellé requis.", code="asset_catalog_fields_required")
        if self.session.scalar(select(AssetType.id).where(AssetType.code == code.strip())):
            raise ApplicationConflictError("Code de type déjà utilisé.", code="asset_type_code_conflict")
        row = AssetType(id=new_id(), code=code.strip(), label=label.strip(), category=category,
                        metadata_json=json.dumps(metadata or {}, sort_keys=True))
        self.session.add(row)
        self.session.flush()
        return row

    def create_asset(self, *, code: str, label: str, asset_type_id: str, metadata: dict | None = None) -> Asset:
        asset_type = self.session.get(AssetType, asset_type_id)
        if asset_type is None or not asset_type.active:
            raise ApplicationValidationError("Type d'actif introuvable ou inactif.", code="asset_type_unavailable")
        if not code.strip() or not label.strip():
            raise ApplicationValidationError("Code et libellé requis.", code="asset_catalog_fields_required")
        if self.session.scalar(select(Asset.id).where(Asset.code == code.strip())):
            raise ApplicationConflictError("Code d'actif déjà utilisé.", code="asset_code_conflict")
        row = Asset(id=new_id(), code=code.strip(), label=label.strip(), asset_type_id=asset_type_id,
                    metadata_json=json.dumps(metadata or {}, sort_keys=True))
        self.session.add(row)
        self.session.flush()
        return row

    def set_approver(
        self,
        *,
        asset_id: str,
        user_id: str,
        assigned: bool,
    ) -> dict:
        self.version.acquire()
        self._require_actor_permission(
            PERMISSION_ADMIN_SETTINGS,
            code="asset_authority_admin_required",
            message="La modification de l'autorité d'un actif exige admin_settings.",
        )
        asset = self.session.get(Asset, asset_id)
        if asset is None:
            raise ApplicationNotFoundError(
                "Unité d'actif introuvable.",
                code="asset_not_found",
            )
        existing = self.session.get(AssetApprover, (asset.id, user_id))
        if assigned:
            user = self.session.get(AppUser, user_id)
            if user is None:
                raise ApplicationNotFoundError(
                    "Utilisateur RessourcePlanner introuvable.",
                    code="asset_approver_user_not_found",
                )
            try:
                roles = normalize_roles(
                    tuple(str(value) for value in json.loads(user.roles_json or "[]"))
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                roles = ()
            if (
                not user.active
                or PERMISSION_APPROVE_DEMANDS not in permissions_for_roles(roles)
            ):
                raise ApplicationValidationError(
                    "Un approbateur d'actif doit être actif et posséder approve_demands.",
                    code="asset_approver_not_admissible",
                    context={"app_user_id": user_id},
                )
            if existing is None:
                self.session.add(
                    AssetApprover(asset_id=asset.id, app_user_id=user_id)
                )
        elif existing is not None:
            self.session.delete(existing)

        self.audit.append(
            entity_type="ASSET",
            entity_id=asset.id,
            entity_reference=asset.code,
            action="Approbateur spécifique",
            before={"app_user_id": user_id, "assigned": existing is not None},
            after={"app_user_id": user_id, "assigned": bool(assigned)},
        )
        self.session.flush()
        approver_ids = tuple(
            self.session.scalars(
                select(AssetApprover.app_user_id)
                .where(AssetApprover.asset_id == asset.id)
                .order_by(AssetApprover.app_user_id)
            ).all()
        )
        return {
            "id": asset.id,
            "approver_user_ids": list(approver_ids),
        }

    def set_active(self, model: type[Asset] | type[AssetType], identifier: str, active: bool, expected_version: int) -> dict:
        self.version.acquire(expected_version)
        row = self.session.get(model, identifier)
        if row is None:
            raise ApplicationNotFoundError("Actif ou type introuvable.", code="asset_not_found")
        previous = {"active": row.active}
        row.active = active
        self.audit.append(entity_type="ASSET" if model is Asset else "ASSET_TYPE", entity_id=row.id,
                          entity_reference=row.code, action="Statut du catalogue", before=previous, after={"active": active})
        self.session.flush()
        return {"id": row.id, "active": row.active, "planning_version": self.version.current_version()}

    def update_catalog(self, model: type[Asset] | type[AssetType], identifier: str,
                       updates: dict, expected_version: int) -> dict:
        self.version.acquire(expected_version)
        row = self.session.get(model, identifier)
        if row is None:
            raise ApplicationNotFoundError("Actif ou type introuvable.", code="asset_not_found")
        if "metadata" in updates:
            updates["metadata_json"] = json.dumps(updates.pop("metadata") or {}, sort_keys=True)
        before = {key: getattr(row, key) for key in updates}
        if model is AssetType:
            if updates.get("category", row.category) not in {"VEHICLE", "EQUIPMENT", "TOOL", "WORKCENTER"}:
                raise ApplicationValidationError("Catégorie invalide.", code="asset_category_invalid")
        elif (
            "asset_type_id" in updates
            and updates["asset_type_id"] != row.asset_type_id
        ):
            self._require_actor_permission(
                PERMISSION_ADMIN_SETTINGS,
                code="asset_authority_admin_required",
                message="Le changement de type d'une unité exige admin_settings.",
            )
            destination = self.session.get(AssetType, updates["asset_type_id"])
            if destination is None or not destination.active:
                raise ApplicationValidationError("Type d'actif indisponible.", code="asset_type_unavailable")
            existing = self.session.scalar(select(AssetAllocation.id).where(AssetAllocation.asset_id == identifier))
            if existing:
                raise ApplicationConflictError("Une réservation existe pour cet actif.", code="asset_type_change_blocked")
        for key, value in updates.items():
            if value is None:
                raise ApplicationValidationError("Ce champ ne peut pas être null.", code="asset_catalog_field_required")
            if key in {"code", "label"} and (not isinstance(value, str) or not value.strip()):
                raise ApplicationValidationError("Code et libellé requis.", code="asset_catalog_fields_required")
            if key == "code" and self.session.scalar(select(model.id).where(model.code == value.strip(), model.id != identifier)):
                raise ApplicationConflictError("Code déjà utilisé.", code="asset_code_conflict")
            setattr(row, key, value.strip() if key in {"code", "label"} else value)
        self.audit.append(entity_type="ASSET" if model is Asset else "ASSET_TYPE", entity_id=row.id,
                          entity_reference=row.code, action="Modification du catalogue",
                          before=before, after={key: getattr(row, key) for key in updates})
        self.session.flush()
        return {"id": row.id, "planning_version": self.version.current_version()}

    def set_type_qualification(
        self,
        *,
        asset_type_id: str,
        competency_ids: list[str],
        qualification_policy: str,
        expected_version: int,
    ) -> dict:
        self.version.acquire(expected_version)
        asset_type = self.session.get(AssetType, asset_type_id)
        if asset_type is None:
            raise ApplicationNotFoundError(
                "Type d'actif introuvable.",
                code="asset_type_not_found",
            )
        if qualification_policy != QUALIFICATION_POLICY_ANY_ASSIGNED_WORKFORCE:
            raise ApplicationValidationError(
                "Politique de qualification d'actif invalide.",
                code="asset_qualification_policy_invalid",
            )

        normalized = tuple(dict.fromkeys(str(value or "").strip() for value in competency_ids))
        normalized = tuple(value for value in normalized if value)
        competencies: list[Competency] = []
        for competency_id in normalized:
            competency = self.session.get(Competency, competency_id)
            if competency is None:
                raise ApplicationValidationError(
                    "Compétence de qualification introuvable.",
                    code="asset_qualification_competency_not_found",
                    context={"competency_id": competency_id},
                )
            if not competency.active:
                raise ApplicationValidationError(
                    "Une compétence inactive ne peut pas être ajoutée comme prérequis.",
                    code="asset_qualification_competency_inactive",
                    context={"competency_id": competency_id},
                )
            competencies.append(competency)

        previous_ids = tuple(
            self.session.scalars(
                select(AssetTypeCompetency.competency_id)
                .where(AssetTypeCompetency.asset_type_id == asset_type.id)
                .order_by(AssetTypeCompetency.competency_id)
            ).all()
        )
        before = {
            "qualification_policy": asset_type.qualification_policy,
            "competency_ids": previous_ids,
        }
        self.session.execute(
            delete(AssetTypeCompetency).where(
                AssetTypeCompetency.asset_type_id == asset_type.id
            )
        )
        self.session.add_all(
            [
                AssetTypeCompetency(
                    asset_type_id=asset_type.id,
                    competency_id=competency.id,
                )
                for competency in competencies
            ]
        )
        asset_type.qualification_policy = qualification_policy
        after = {
            "qualification_policy": asset_type.qualification_policy,
            "competency_ids": normalized,
        }
        self.audit.append(
            entity_type="ASSET_TYPE",
            entity_id=asset_type.id,
            entity_reference=asset_type.code,
            action="Prérequis de qualification",
            before=before,
            after=after,
        )
        self.session.flush()
        return {
            "id": asset_type.id,
            "qualification_policy": asset_type.qualification_policy,
            "competency_ids": list(normalized),
            "planning_version": self.version.current_version(),
        }

    def operator_candidates(self, requirement_id: str) -> dict:
        requirement = self.session.get(AssetRequirement, requirement_id)
        if requirement is None or requirement.status == "Annulé":
            raise ApplicationNotFoundError(
                "Besoin d'actif introuvable.",
                code="asset_requirement_not_found",
            )
        if requirement.origin == AssetRequirementOrigin.REQUEST.value:
            self._validate_request_authority(requirement)
        elif requirement.origin not in {
            AssetRequirementOrigin.PROJECT_DIRECT.value,
            AssetRequirementOrigin.SEGMENT.value,
        }:
            raise ApplicationConflictError(
                "Le contexte de réservation ne permet pas de choisir un opérateur.",
                code="asset_operator_context_unsupported",
            )
        allocation = self.session.scalar(
            select(AssetAllocation).where(
                AssetAllocation.asset_requirement_id == requirement.id
            )
        )
        if allocation is None:
            raise ApplicationConflictError(
                "Une réservation d'actif est requise avant de choisir un opérateur.",
                code="asset_operator_requires_reservation",
            )
        qualification = evaluate_asset_qualification(
            self.session,
            requirement=requirement,
            allocation=allocation,
        )
        candidates = eligible_operator_resources(
            self.session,
            requirement=requirement,
            allocation=allocation,
        )
        return {
            "requirement_id": requirement.id,
            "allocation_id": allocation.id,
            "qualification_state": qualification.state,
            "required_competency_ids": list(qualification.required_competency_ids),
            "required_competency_names": list(qualification.required_competency_names),
            "operator_resource_id": qualification.operator_resource_id,
            "operator_resource_name": qualification.operator_resource_name,
            "candidates": [
                {"resource_id": row.id, "resource_name": row.name}
                for row in candidates
            ],
            "planning_version": self.version.current_version(),
        }

    def set_operator(
        self,
        *,
        requirement_id: str,
        operator_resource_id: str | None,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "requirement_id": requirement_id,
            "operator_resource_id": operator_resource_id,
            "expected_version": expected_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return SqlCommandIdempotencyAdapter(
            self.session,
            actor_name=self.actor,
        ).replay_or_execute(
            scope="asset_operator_assignment",
            key=idempotency_key,
            request_fingerprint=fingerprint,
            action=lambda: self._set_operator(**payload),
        )


    def _set_operator(
        self,
        *,
        requirement_id: str,
        operator_resource_id: str | None,
        expected_version: int,
    ) -> dict:
        self.version.acquire(expected_version)
        requirement = self.session.get(AssetRequirement, requirement_id)
        if requirement is None or requirement.status == "Annulé":
            raise ApplicationNotFoundError(
                "Besoin d'actif introuvable.",
                code="asset_requirement_not_found",
            )
        self._validate_request_authority(requirement)
        allocation = self.session.scalar(
            select(AssetAllocation).where(
                AssetAllocation.asset_requirement_id == requirement.id
            )
        )
        if allocation is None:
            raise ApplicationConflictError(
                "Une réservation d'actif est requise avant de choisir un opérateur.",
                code="asset_operator_requires_reservation",
            )
        authority_proof = self._require_assignment_authorities(allocation.asset_id)

        proposed_operator_id = str(operator_resource_id or "").strip() or None
        before = {
            "asset_id": allocation.asset_id,
            "start_date": allocation.start_date.isoformat(),
            "end_date": allocation.end_date.isoformat(),
            "operator_resource_id": allocation.operator_resource_id,
        }
        qualification_state = self._validate_request_allocation_state(
            requirement=requirement,
            asset_id=allocation.asset_id,
            start_date=allocation.start_date,
            end_date=allocation.end_date,
            operator_resource_id=proposed_operator_id,
            allocation_id=allocation.id,
            source=allocation.source,
        )
        allocation.operator_resource_id = proposed_operator_id

        self.audit.append(
            entity_type="ASSET_ALLOCATION",
            entity_id=allocation.id,
            entity_reference=requirement.id,
            parent_reference=requirement.workforce_request_id,
            action="Opérateur qualifiant",
            before=before,
            after={
                "asset_id": allocation.asset_id,
                "start_date": allocation.start_date,
                "end_date": allocation.end_date,
                "operator_resource_id": allocation.operator_resource_id,
                "assignment_authority": authority_proof,
            },
        )
        self.session.flush()
        return {
            "allocation_id": allocation.id,
            "requirement_id": requirement.id,
            "operator_resource_id": allocation.operator_resource_id,
            "qualification_state": qualification_state,
            "planning_version": self.version.current_version(),
        }
    def _shift_context(
        self,
        identifier: str,
    ) -> tuple[Shift, ResourceRequirement]:
        wanted = str(identifier or "").strip()
        shift = self.session.scalar(
            select(Shift).where(
                (Shift.id == wanted) | (Shift.legacy_allocation_id == wanted)
            )
        )
        if shift is None:
            raise ApplicationNotFoundError(
                "Quart introuvable.",
                code="shift_not_found",
            )
        requirement = self.session.get(
            ResourceRequirement,
            shift.resource_requirement_id,
        )
        if requirement is None or requirement.status == "Annulé":
            raise ApplicationConflictError(
                "Le besoin humain propriétaire du quart n'est plus disponible.",
                code="shift_requirement_unavailable",
                context={"shift_id": shift.id},
            )
        return shift, requirement

    def _request_requirement_applies_to_shift(
        self,
        *,
        requirement: AssetRequirement,
        shift: Shift,
        human_requirement: ResourceRequirement,
    ) -> bool:
        return bool(
            requirement.origin == AssetRequirementOrigin.REQUEST.value
            and human_requirement.workforce_request_id
            and requirement.workforce_request_id
            == human_requirement.workforce_request_id
            and requirement.project_id == human_requirement.project_id
            and requirement.start_date <= shift.work_date <= requirement.end_date
            and requirement.status != "Annulé"
        )

    def _validate_request_authority(
        self,
        requirement: AssetRequirement,
    ) -> None:
        request_id = requirement.workforce_request_id
        if (
            requirement.origin != AssetRequirementOrigin.REQUEST.value
            or not request_id
        ):
            raise ApplicationConflictError(
                "Le besoin d'actif n'est pas relié à une provenance REQUEST.",
                code="asset_approval_revision_conflict",
            )
        reference = self.session.get(RequestApprovalReference, request_id)
        if (
            reference is None
            or requirement.approval_revision_id != reference.active_revision_id
        ):
            raise ApplicationConflictError(
                "Révision approuvée obsolète.",
                code="asset_approval_revision_conflict",
            )
        revision = self.session.get(
            RequestApprovalRevision,
            reference.active_revision_id,
        )
        choices = SqlRequestOperationalChoiceRepository(
            self.session
        ).state_for_request_id(request_id)
        if (
            revision is None
            or choices is None
            or choices.approval_revision_id != revision.id
        ):
            raise ApplicationConflictError(
                "Autorisation opérationnelle absente.",
                code="asset_approval_revision_conflict",
            )
        envelope = approval_envelope_from_snapshot_payload(
            json.loads(revision.payload_text)["authorization"]
        )
        approved = next(
            (
                entry
                for entry in envelope.entries
                if entry.identity.stable_key == requirement.approved_entry_key
                and entry.line_kind == "ASSET"
            ),
            None,
        )
        if (
            approved is None
            or approved.asset_type_id != requirement.asset_type_id
            or approved.project_id != requirement.project_id
            or requirement.start_date < approved.start_date
            or requirement.end_date > approved.end_date
            or (
                approved.group is not None
                and choices.selections.get(approved.group.stable_key)
                != approved.identity.stable_key
            )
        ):
            raise ApplicationConflictError(
                "Le besoin ne correspond plus à l'autorisation active.",
                code="asset_approval_entry_conflict",
            )

    def _active_asset(self, asset_id: str) -> tuple[Asset, AssetType]:
        asset = self.session.get(Asset, asset_id)
        asset_type = (
            self.session.get(AssetType, asset.asset_type_id)
            if asset is not None
            else None
        )
        if (
            asset is None
            or not asset.active
            or asset_type is None
            or not asset_type.active
        ):
            raise ApplicationValidationError(
                "Actif inactif ou incompatible.",
                code="asset_incompatible",
            )
        return asset, asset_type

    def _allocation_for_requirement(
        self,
        requirement_id: str,
    ) -> AssetAllocation | None:
        rows = self.session.scalars(
            select(AssetAllocation).where(
                AssetAllocation.asset_requirement_id == requirement_id
            )
        ).all()
        if len(rows) > 1:
            raise ApplicationConflictError(
                "Plusieurs allocations pour un slot.",
                code="asset_slot_conflict",
            )
        return rows[0] if rows else None

    def _assert_asset_available(
        self,
        *,
        asset_id: str,
        start_date: date,
        end_date: date,
        allocation_id: str,
    ) -> None:
        candidate = AssetOccupation(
            allocation_id,
            asset_id,
            start_date,
            end_date,
        )
        existing = self.session.scalars(
            select(AssetAllocation).where(
                AssetAllocation.asset_id == asset_id,
                AssetAllocation.start_date <= end_date,
                AssetAllocation.end_date >= start_date,
            )
        ).all()
        conflicts = overlapping_asset_occupations(
            candidate,
            (
                AssetOccupation(
                    row.id,
                    row.asset_id,
                    row.start_date,
                    row.end_date,
                    row.locked,
                )
                for row in existing
            ),
        )
        if conflicts:
            raise ApplicationConflictError(
                "Actif déjà réservé.",
                code="asset_double_booking",
                context={"allocation_ids": conflicts},
            )
        unavailable = self.session.scalar(
            select(AssetUnavailability.id).where(
                AssetUnavailability.asset_id == asset_id,
                AssetUnavailability.start_date <= end_date,
                AssetUnavailability.end_date >= start_date,
            )
        )
        if unavailable:
            raise ApplicationConflictError(
                "Actif indisponible.",
                code="asset_unavailable",
            )


    @staticmethod
    def _resolve_request_reservation_dates(
        *,
        requirement: AssetRequirement,
        previous: AssetAllocation | None,
        start_date: str | date | None,
        end_date: str | date | None,
    ) -> tuple[date, date]:
        if (start_date is None) != (end_date is None):
            raise ApplicationValidationError(
                "Les dates réelles de réservation doivent être fournies ensemble.",
                code="asset_reservation_dates_required",
            )
        if start_date is None:
            if previous is None:
                raise ApplicationValidationError(
                    "Les dates réelles de réservation sont requises.",
                    code="asset_reservation_dates_required",
                )
            return previous.start_date, previous.end_date

        begin = (
            date.fromisoformat(start_date)
            if isinstance(start_date, str)
            else start_date
        )
        end = (
            date.fromisoformat(end_date)
            if isinstance(end_date, str)
            else end_date
        )
        if begin > end:
            raise ApplicationValidationError(
                "La date de début doit précéder ou égaler la date de fin.",
                code="asset_reservation_window_invalid",
            )
        if not (
            requirement.start_date
            <= begin
            <= end
            <= requirement.end_date
        ):
            raise ApplicationValidationError(
                "Réservation hors fenêtre approuvée.",
                code="asset_outside_approved_window",
            )
        return begin, end

    @staticmethod
    def _request_operator_error(state: str) -> ApplicationValidationError:
        if state == QUALIFICATION_SKILL_MISMATCH:
            return ApplicationValidationError(
                "La ressource choisie n'est pas active ou ne possède pas les compétences requises.",
                code="asset_operator_skill_mismatch",
            )
        if state == QUALIFICATION_NO_OVERLAP:
            return ApplicationValidationError(
                "La ressource choisie n'a aucune affectation compatible sur cette réservation.",
                code="asset_operator_no_overlap",
            )
        return ApplicationValidationError(
            "La ressource choisie ne satisfait pas la qualification d'actif.",
            code="asset_operator_invalid",
        )

    def _validate_request_allocation_state(
        self,
        *,
        requirement: AssetRequirement,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str | None,
        allocation_id: str,
        source: str = "MANUAL",
    ) -> str:
        self._validate_request_authority(requirement)
        asset, _asset_type = self._active_asset(asset_id)
        if asset.asset_type_id != requirement.asset_type_id:
            raise ApplicationValidationError(
                "Actif inactif ou incompatible.",
                code="asset_incompatible",
            )
        if not (
            requirement.start_date
            <= start_date
            <= end_date
            <= requirement.end_date
        ):
            raise ApplicationValidationError(
                "Réservation hors fenêtre approuvée.",
                code="asset_outside_approved_window",
            )
        self._assert_asset_available(
            asset_id=asset.id,
            start_date=start_date,
            end_date=end_date,
            allocation_id=allocation_id,
        )
        candidate = AssetAllocation(
            id=allocation_id,
            asset_requirement_id=requirement.id,
            asset_id=asset.id,
            operator_resource_id=operator_resource_id,
            start_date=start_date,
            end_date=end_date,
            locked=True,
            source=source,
        )
        qualification = evaluate_asset_qualification(
            self.session,
            requirement=requirement,
            allocation=candidate,
        )
        if (
            operator_resource_id is not None
            and qualification.state != QUALIFICATION_SATISFIED
        ):
            raise self._request_operator_error(qualification.state)
        return qualification.state

    def _request_requirements_for_shift(
        self,
        *,
        shift: Shift,
        human_requirement: ResourceRequirement,
        asset_type_id: str | None = None,
    ) -> tuple[AssetRequirement, ...]:
        if not human_requirement.workforce_request_id:
            return ()
        statement = select(AssetRequirement).where(
            AssetRequirement.origin == AssetRequirementOrigin.REQUEST.value,
            AssetRequirement.workforce_request_id
            == human_requirement.workforce_request_id,
            AssetRequirement.project_id == human_requirement.project_id,
            AssetRequirement.start_date <= shift.work_date,
            AssetRequirement.end_date >= shift.work_date,
            AssetRequirement.status != "Annulé",
        )
        if asset_type_id is not None:
            statement = statement.where(
                AssetRequirement.asset_type_id == asset_type_id
            )
        return tuple(
            self.session.scalars(
                statement.order_by(
                    AssetRequirement.slot_index,
                    AssetRequirement.id,
                )
            ).all()
        )

    def _ambiguous_shift_requirement(
        self,
        *,
        shift: Shift,
        requirements: tuple[AssetRequirement, ...],
    ) -> ApplicationConflictError:
        return ApplicationConflictError(
            "Plusieurs besoins d'actif REQUEST sont applicables à ce quart.",
            code="shift_asset_requirement_ambiguous",
            context={
                "shift_id": shift.id,
                "requirement_ids": [row.id for row in requirements],
            },
        )

    def _request_requirement_for_asset(
        self,
        *,
        shift: Shift,
        human_requirement: ResourceRequirement,
        asset_id: str,
        asset_type_id: str,
    ) -> AssetRequirement | None:
        candidates = self._request_requirements_for_shift(
            shift=shift,
            human_requirement=human_requirement,
            asset_type_id=asset_type_id,
        )
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        linked: list[AssetRequirement] = []
        for requirement in candidates:
            allocation = self._allocation_for_requirement(requirement.id)
            if (
                allocation is not None
                and allocation.start_date <= shift.work_date <= allocation.end_date
                and (
                    allocation.operator_resource_id == shift.resource_id
                    or allocation.asset_id == asset_id
                )
            ):
                linked.append(requirement)
        if len(linked) == 1:
            return linked[0]
        raise self._ambiguous_shift_requirement(
            shift=shift,
            requirements=candidates,
        )

    def _request_requirement_for_release(
        self,
        *,
        shift: Shift,
        human_requirement: ResourceRequirement,
    ) -> AssetRequirement | None:
        candidates = self._request_requirements_for_shift(
            shift=shift,
            human_requirement=human_requirement,
        )
        occupied: list[tuple[AssetRequirement, AssetAllocation]] = []
        for requirement in candidates:
            allocation = self._allocation_for_requirement(requirement.id)
            if (
                allocation is not None
                and allocation.start_date <= shift.work_date <= allocation.end_date
            ):
                occupied.append((requirement, allocation))
        if not occupied:
            return None
        if len(occupied) == 1:
            return occupied[0][0]
        linked = [
            requirement
            for requirement, allocation in occupied
            if allocation.operator_resource_id == shift.resource_id
        ]
        if len(linked) == 1:
            return linked[0]
        raise self._ambiguous_shift_requirement(
            shift=shift,
            requirements=tuple(row[0] for row in occupied),
        )

    def _resolve_shift_asset_requirement(
        self,
        *,
        shift: Shift,
        human_requirement: ResourceRequirement,
        asset: Asset | None,
        explicit_requirement_id: str | None,
    ) -> AssetRequirement | None:
        if explicit_requirement_id:
            requirement = self.session.get(
                AssetRequirement,
                explicit_requirement_id,
            )
            if requirement is None or requirement.status == "Annulé":
                raise ApplicationNotFoundError(
                    "Besoin d'actif introuvable.",
                    code="asset_requirement_not_found",
                )
            if requirement.origin == AssetRequirementOrigin.SHIFT_AD_HOC.value:
                if requirement.shift_id != shift.id:
                    raise ApplicationConflictError(
                        "Le besoin ad hoc appartient à un autre quart.",
                        code="shift_asset_requirement_owner_conflict",
                    )
                return requirement
            if not self._request_requirement_applies_to_shift(
                requirement=requirement,
                shift=shift,
                human_requirement=human_requirement,
            ):
                raise ApplicationConflictError(
                    "Le besoin REQUEST n'est pas applicable à ce quart.",
                    code="shift_asset_requirement_scope_conflict",
                )
            return requirement

        ad_hoc = self.session.scalar(
            select(AssetRequirement).where(
                AssetRequirement.origin
                == AssetRequirementOrigin.SHIFT_AD_HOC.value,
                AssetRequirement.shift_id == shift.id,
            )
        )
        if ad_hoc is not None:
            if ad_hoc.status == "Annulé":
                raise ApplicationConflictError(
                    "Le besoin ad hoc du quart est dans un état invalide.",
                    code="shift_asset_requirement_state_conflict",
                )
            return ad_hoc

        if asset is not None:
            return self._request_requirement_for_asset(
                shift=shift,
                human_requirement=human_requirement,
                asset_id=asset.id,
                asset_type_id=asset.asset_type_id,
            )
        return self._request_requirement_for_release(
            shift=shift,
            human_requirement=human_requirement,
        )

    @staticmethod
    def _qualification_error(state: str) -> ApplicationValidationError:
        if state == QUALIFICATION_SKILL_MISMATCH:
            return ApplicationValidationError(
                "La ressource du quart ne possède pas les compétences requises.",
                code="asset_operator_skill_mismatch",
            )
        if state == QUALIFICATION_NO_OVERLAP:
            return ApplicationValidationError(
                "La ressource du quart n'a aucune affectation compatible sur cette réservation.",
                code="asset_operator_no_overlap",
            )
        return ApplicationValidationError(
            "La ressource du quart ne satisfait pas la qualification d'actif.",
            code="asset_operator_invalid",
        )


    def set_shift_asset(
        self,
        *,
        shift_id: str,
        asset_id: str | None,
        requirement_id: str | None,
        start_date: date | None,
        end_date: date | None,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "shift_id": str(shift_id or "").strip(),
            "asset_id": str(asset_id or "").strip() or None,
            "requirement_id": str(requirement_id or "").strip() or None,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "expected_version": expected_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return SqlCommandIdempotencyAdapter(
            self.session,
            actor_name=self.actor,
        ).replay_or_execute(
            scope="shift_asset_assignment",
            key=idempotency_key,
            request_fingerprint=fingerprint,
            action=lambda: self._set_shift_asset(**payload),
        )

    def _set_shift_asset(
        self,
        *,
        shift_id: str,
        asset_id: str | None,
        requirement_id: str | None,
        start_date: str | None,
        end_date: str | None,
        expected_version: int,
    ) -> dict:
        self.version.acquire(expected_version)
        shift, human_requirement = self._shift_context(shift_id)
        asset: Asset | None = None
        if asset_id is not None:
            asset, _asset_type = self._active_asset(asset_id)

        requirement = self._resolve_shift_asset_requirement(
            shift=shift,
            human_requirement=human_requirement,
            asset=asset,
            explicit_requirement_id=requirement_id,
        )
        created_requirement = False
        if requirement is None:
            if asset is None:
                raise ApplicationNotFoundError(
                    "Aucune affectation d'actif n'est reliée à ce quart.",
                    code="shift_asset_assignment_not_found",
                )
            requirement = AssetRequirement(
                id=new_id(),
                project_id=human_requirement.project_id,
                origin=AssetRequirementOrigin.SHIFT_AD_HOC.value,
                shift_id=shift.id,
                workforce_request_id=None,
                source_request_line_id=None,
                source_period_id=None,
                approval_revision_id=None,
                approved_entry_key=None,
                slot_index=0,
                asset_type_id=asset.asset_type_id,
                start_date=shift.work_date,
                end_date=shift.work_date,
                usage_hours=None,
                status="À affecter",
            )
            self.session.add(requirement)
            created_requirement = True

        origin = requirement.origin
        if origin == AssetRequirementOrigin.REQUEST.value:
            if not self._request_requirement_applies_to_shift(
                requirement=requirement,
                shift=shift,
                human_requirement=human_requirement,
            ):
                raise ApplicationConflictError(
                    "Le besoin REQUEST n'est pas applicable à ce quart.",
                    code="shift_asset_requirement_scope_conflict",
                )
            self._validate_request_authority(requirement)
        elif origin == AssetRequirementOrigin.SHIFT_AD_HOC.value:
            if requirement.shift_id != shift.id:
                raise ApplicationConflictError(
                    "Le besoin ad hoc appartient à un autre quart.",
                    code="shift_asset_requirement_owner_conflict",
                )
        else:
            raise ApplicationConflictError(
                "Origine de besoin d'actif non supportée.",
                code="shift_asset_requirement_origin_invalid",
            )

        if (start_date is None) != (end_date is None):
            raise ApplicationValidationError(
                "Les dates réelles de réservation doivent être fournies ensemble.",
                code="asset_reservation_dates_required",
            )
        previous = self._allocation_for_requirement(requirement.id)
        authority_proof = self._require_assignment_authorities(
            previous.asset_id if previous is not None else None,
            asset.id if asset is not None else None,
        )
        requirement_id_value = requirement.id
        before = (
            {
                "asset_id": previous.asset_id,
                "start_date": previous.start_date,
                "end_date": previous.end_date,
                "operator_resource_id": previous.operator_resource_id,
                "origin": origin,
                "assignment_authority": authority_proof,
            }
            if previous is not None
            else None
        )
        parent_reference = (
            requirement.workforce_request_id
            if origin == AssetRequirementOrigin.REQUEST.value
            else shift.id
        )

        shift_before = {
            "source": shift.source,
            "locked": bool(shift.locked),
        }

        if asset is None:
            if previous is None:
                raise ApplicationNotFoundError(
                    "Aucune affectation d'actif n'est reliée à ce quart.",
                    code="shift_asset_assignment_not_found",
                )
            allocation_id = previous.id
            self.session.delete(previous)
            if origin == AssetRequirementOrigin.SHIFT_AD_HOC.value:
                if shift.source == "AUTO":
                    shift.source = "MANUAL"
                shift.locked = True
                self.session.delete(requirement)
            else:
                requirement.status = "À affecter"

            self.audit.append(
                entity_type="ASSET_ALLOCATION",
                entity_id=allocation_id,
                entity_reference=requirement_id_value,
                parent_reference=parent_reference,
                action="Libération actif du quart",
                before=before,
                after=None,
            )
            shift_after = {
                "source": shift.source,
                "locked": bool(shift.locked),
            }
            if shift_before != shift_after:
                self.audit.append(
                    entity_type="SHIFT",
                    entity_id=shift.id,
                    entity_reference=shift.legacy_allocation_id or shift.id,
                    parent_reference=human_requirement.legacy_segment_id
                    or human_requirement.id,
                    action="Protection quart pour actif ad hoc",
                    before=shift_before,
                    after=shift_after,
                )
            self.session.flush()
            return {
                "operation": "RELEASE",
                "shift_id": shift.id,
                "requirement_id": requirement_id_value,
                "requirement_origin": origin,
                "allocation_id": None,
                "asset_id": None,
                "operator_resource_id": None,
                "qualification_state": None,
                "shift_source": shift.source,
                "shift_locked": bool(shift.locked),
                "planning_version": self.version.current_version(),
            }

        allocation_id = previous.id if previous is not None else new_id()
        if origin == AssetRequirementOrigin.REQUEST.value:
            begin, end = self._resolve_request_reservation_dates(
                requirement=requirement,
                previous=previous,
                start_date=start_date,
                end_date=end_date,
            )
            operator_resource_id = (
                previous.operator_resource_id
                if previous is not None
                else (
                    shift.resource_id
                    if required_competencies(
                        self.session,
                        requirement.asset_type_id,
                    )
                    else None
                )
            )
            qualification_state = self._validate_request_allocation_state(
                requirement=requirement,
                asset_id=asset.id,
                start_date=begin,
                end_date=end,
                operator_resource_id=operator_resource_id,
                allocation_id=allocation_id,
                source=previous.source if previous is not None else "MANUAL",
            )
        else:
            requirement.asset_type_id = asset.asset_type_id
            requirement.start_date = shift.work_date
            requirement.end_date = shift.work_date
            begin = shift.work_date
            end = shift.work_date
            operator_resource_id = shift.resource_id
            self._assert_asset_available(
                asset_id=asset.id,
                start_date=begin,
                end_date=end,
                allocation_id=allocation_id,
            )
            ad_hoc_candidate = AssetAllocation(
                id=allocation_id,
                asset_requirement_id=requirement.id,
                asset_id=asset.id,
                operator_resource_id=operator_resource_id,
                start_date=begin,
                end_date=end,
                locked=True,
                source=previous.source if previous is not None else "MANUAL",
            )
            qualification = evaluate_asset_qualification(
                self.session,
                requirement=requirement,
                allocation=ad_hoc_candidate,
            )
            if qualification.state != QUALIFICATION_SATISFIED:
                raise self._qualification_error(qualification.state)
            qualification_state = qualification.state

        candidate = AssetAllocation(
            id=allocation_id,
            asset_requirement_id=requirement.id,
            asset_id=asset.id,
            operator_resource_id=operator_resource_id,
            start_date=begin,
            end_date=end,
            locked=True,
            source=previous.source if previous is not None else "MANUAL",
        )
        if previous is None:
            self.session.add(candidate)
            allocation = candidate
        else:
            previous.asset_id = candidate.asset_id
            previous.operator_resource_id = candidate.operator_resource_id
            previous.start_date = candidate.start_date
            previous.end_date = candidate.end_date
            previous.locked = True
            allocation = previous
        requirement.status = "Planifié"

        if origin == AssetRequirementOrigin.SHIFT_AD_HOC.value:
            if shift.source == "AUTO":
                shift.source = "MANUAL"
            shift.locked = True

        operation = "ASSIGN" if previous is None else "CHANGE"
        action = (
            "Affectation actif au quart"
            if operation == "ASSIGN"
            else "Changement actif du quart"
        )
        after = {
            "asset_id": allocation.asset_id,
            "start_date": allocation.start_date,
            "end_date": allocation.end_date,
            "operator_resource_id": allocation.operator_resource_id,
            "origin": origin,
            "assignment_authority": authority_proof,
        }
        self.audit.append(
            entity_type="ASSET_ALLOCATION",
            entity_id=allocation.id,
            entity_reference=requirement.id,
            parent_reference=parent_reference,
            action=action,
            before=before,
            after=after,
        )
        shift_after = {
            "source": shift.source,
            "locked": bool(shift.locked),
        }
        if shift_before != shift_after:
            self.audit.append(
                entity_type="SHIFT",
                entity_id=shift.id,
                entity_reference=shift.legacy_allocation_id or shift.id,
                parent_reference=human_requirement.legacy_segment_id
                or human_requirement.id,
                action="Protection quart pour actif ad hoc",
                before=shift_before,
                after=shift_after,
            )
        self.session.flush()
        return {
            "operation": operation,
            "shift_id": shift.id,
            "requirement_id": requirement.id,
            "requirement_origin": origin,
            "allocation_id": allocation.id,
            "asset_id": allocation.asset_id,
            "operator_resource_id": allocation.operator_resource_id,
            "qualification_state": qualification_state,
            "shift_source": shift.source,
            "shift_locked": bool(shift.locked),
            "planning_version": self.version.current_version(),
        }
    def shift_ad_hoc_attachment(
        self,
        shift_id: str,
    ) -> tuple[AssetRequirement | None, AssetAllocation | None]:
        """Return the shift-owned ad-hoc requirement/allocation without changing it."""

        requirement = self.session.scalar(
            select(AssetRequirement).where(
                AssetRequirement.origin
                == AssetRequirementOrigin.SHIFT_AD_HOC.value,
                AssetRequirement.shift_id == shift_id,
            )
        )
        if requirement is None:
            return None, None
        return requirement, self._allocation_for_requirement(requirement.id)

    def synchronize_shift_ad_hoc_assignment(
        self,
        *,
        shift: Shift,
    ) -> dict | None:
        """Keep one SHIFT_AD_HOC reservation aligned with its owning Shift.

        The caller owns the transaction and planning CAS.  This method intentionally
        does not acquire another version and does not commit.  It mutates the existing
        requirement/allocation identities, then reuses the canonical #291/#292
        availability and qualification rules before the surrounding transaction can
        commit.
        """

        requirement, allocation = self.shift_ad_hoc_attachment(shift.id)
        if requirement is None:
            return None
        if allocation is None:
            raise ApplicationConflictError(
                "L'affectation d'actif ad hoc du quart est incomplète.",
                code="shift_asset_assignment_incomplete",
                context={
                    "shift_id": shift.id,
                    "requirement_id": requirement.id,
                },
            )

        asset, _asset_type = self._active_asset(allocation.asset_id)
        requirement.start_date = shift.work_date
        requirement.end_date = shift.work_date
        allocation.start_date = shift.work_date
        allocation.end_date = shift.work_date
        allocation.operator_resource_id = shift.resource_id
        self.session.flush()

        self._assert_asset_available(
            asset_id=asset.id,
            start_date=shift.work_date,
            end_date=shift.work_date,
            allocation_id=allocation.id,
        )
        qualification = evaluate_asset_qualification(
            self.session,
            requirement=requirement,
            allocation=allocation,
        )
        if qualification.state != QUALIFICATION_SATISFIED:
            raise self._qualification_error(qualification.state)

        return {
            "requirement_id": requirement.id,
            "allocation_id": allocation.id,
            "asset_id": allocation.asset_id,
            "operator_resource_id": allocation.operator_resource_id,
            "qualification_state": qualification.state,
            "start_date": allocation.start_date,
            "end_date": allocation.end_date,
        }

    def assert_shift_can_return_auto(self, shift: Shift) -> None:
        """Fail closed while a Shift-owned explicit decision is still attached."""

        if shift.operational_responsible_override_contact_id is not None:
            raise ApplicationConflictError(
                "Retire le responsable opérationnel propre au quart avant de retourner vers l'automatique.",
                code="shift_operational_responsibility_override_must_be_cleared",
                context={
                    "shift_id": shift.id,
                    "business_contact_id": (
                        shift.operational_responsible_override_contact_id
                    ),
                },
            )

        requirement, _allocation = self.shift_ad_hoc_attachment(shift.id)
        if requirement is None:
            return
        raise ApplicationConflictError(
            "Libère l'actif ad hoc avant de retourner le quart vers l'automatique.",
            code="asset_assignment_must_be_released",
            context={
                "shift_id": shift.id,
                "requirement_id": requirement.id,
            },
        )

    def delete_shift_ad_hoc_assignment(
        self,
        *,
        shift: Shift,
    ) -> dict | None:
        """Explicitly delete allocation then requirement before deleting a Shift."""

        requirement, allocation = self.shift_ad_hoc_attachment(shift.id)
        if requirement is None:
            return None

        result = {
            "requirement_id": requirement.id,
            "allocation_id": allocation.id if allocation is not None else None,
            "asset_id": allocation.asset_id if allocation is not None else None,
            "operator_resource_id": (
                allocation.operator_resource_id if allocation is not None else None
            ),
            "origin": requirement.origin,
        }
        if allocation is not None:
            self.session.delete(allocation)
            self.session.flush()
        self.session.delete(requirement)
        self.session.flush()
        return result

    def shift_asset_candidates(self, shift_id: str) -> dict:
        """Read candidate assets for one Shift without exposing hidden occupancies."""

        shift, _human_requirement = self._shift_context(shift_id)
        current_requirement, current_allocation = self.shift_ad_hoc_attachment(
            shift.id
        )

        asset_rows = tuple(
            self.session.execute(
                select(Asset, AssetType)
                .join(AssetType, Asset.asset_type_id == AssetType.id)
                .order_by(AssetType.code, Asset.code, Asset.id)
            ).all()
        )
        type_ids = tuple(dict.fromkeys(row.asset_type_id for row, _type in asset_rows))
        required_by_type: dict[str, set[str]] = {}
        if type_ids:
            for type_id, competency_id in self.session.execute(
                select(
                    AssetTypeCompetency.asset_type_id,
                    AssetTypeCompetency.competency_id,
                ).where(AssetTypeCompetency.asset_type_id.in_(type_ids))
            ).all():
                required_by_type.setdefault(type_id, set()).add(competency_id)

        operator = self.session.get(Resource, shift.resource_id)
        operator_competencies = set(
            self.session.scalars(
                select(ResourceCompetency.competency_id).where(
                    ResourceCompetency.resource_id == shift.resource_id
                )
            ).all()
        )

        overlapping_allocations = tuple(
            self.session.scalars(
                select(AssetAllocation).where(
                    AssetAllocation.start_date <= shift.work_date,
                    AssetAllocation.end_date >= shift.work_date,
                )
            ).all()
        )
        occupied_asset_ids = {
            row.asset_id
            for row in overlapping_allocations
            if current_allocation is None or row.id != current_allocation.id
        }
        unavailable_asset_ids = set(
            self.session.scalars(
                select(AssetUnavailability.asset_id).where(
                    AssetUnavailability.start_date <= shift.work_date,
                    AssetUnavailability.end_date >= shift.work_date,
                )
            ).all()
        )

        candidates: list[dict] = []
        for asset, asset_type in asset_rows:
            required = required_by_type.get(asset.asset_type_id, set())
            qualification_state = QUALIFICATION_SATISFIED
            if (
                operator is None
                or not operator.active
                or not required.issubset(operator_competencies)
            ):
                qualification_state = QUALIFICATION_SKILL_MISMATCH

            compatible = bool(asset.active and asset_type.active)
            available = bool(
                asset.id not in occupied_asset_ids
                and asset.id not in unavailable_asset_ids
            )
            reasons: list[str] = []
            if not compatible:
                reasons.append("asset_inactive")
            if qualification_state != QUALIFICATION_SATISFIED:
                reasons.append("operator_not_qualified")
            if not available:
                reasons.append("asset_unavailable")

            candidates.append(
                {
                    "id": asset.id,
                    "code": asset.code,
                    "label": asset.label,
                    "asset_type_id": asset.asset_type_id,
                    "asset_type_code": asset_type.code,
                    "asset_type_label": asset_type.label,
                    "active": bool(asset.active),
                    "compatible": compatible,
                    "available": available,
                    "qualification_state": qualification_state,
                    "allowed": not reasons,
                    "reason": reasons[0] if reasons else None,
                    "diagnostics": reasons,
                    "currently_assigned": bool(
                        current_allocation is not None
                        and current_allocation.asset_id == asset.id
                    ),
                }
            )
        return {
            "shift_id": shift.id,
            "work_date": shift.work_date,
            "operator_resource_id": shift.resource_id,
            "current_requirement_id": (
                current_requirement.id if current_requirement is not None else None
            ),
            "current_allocation_id": (
                current_allocation.id if current_allocation is not None else None
            ),
            "candidates": candidates,
            "planning_version": self.version.current_version(),
        }

    @staticmethod
    def _validate_direct_window(start_date: date, end_date: date) -> None:
        if start_date > end_date:
            raise ApplicationValidationError(
                "La date de début doit précéder ou égaler la date de fin.",
                code="asset_reservation_window_invalid",
            )

    def _direct_project(self, project_id: str | None) -> Project | None:
        if project_id is None:
            return None
        project = self.session.get(Project, project_id)
        if project is None:
            raise ApplicationNotFoundError(
                "Projet introuvable.",
                code="asset_project_not_found",
            )
        return project

    def _direct_resource(self, resource_id: str) -> Resource:
        resource = self.session.get(Resource, resource_id)
        if resource is None or not resource.active:
            raise ApplicationValidationError(
                "Ressource absente ou inactive.",
                code="asset_context_resource_unavailable",
            )
        return resource

    def _validate_direct_allocation_state(
        self,
        *,
        requirement: AssetRequirement,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str | None,
        allocation_id: str,
        source: str = "MANUAL",
    ) -> str:
        if requirement.origin not in {
            AssetRequirementOrigin.PROJECT_DIRECT.value,
            AssetRequirementOrigin.RESOURCE_PERIOD.value,
        }:
            raise ApplicationConflictError(
                "Le besoin d'actif n'est pas une réservation directe.",
                code="asset_direct_origin_conflict",
            )
        self._validate_direct_window(start_date, end_date)
        if (
            requirement.origin == AssetRequirementOrigin.PROJECT_DIRECT.value
            and not requirement.project_id
        ):
            raise ApplicationConflictError(
                "La réservation projet n'a plus de projet propriétaire.",
                code="asset_direct_context_conflict",
            )
        if requirement.origin == AssetRequirementOrigin.RESOURCE_PERIOD.value:
            if (
                not requirement.context_resource_id
                or operator_resource_id != requirement.context_resource_id
            ):
                raise ApplicationConflictError(
                    "La ressource bénéficiaire doit rester l'opérateur de la période.",
                    code="asset_resource_period_operator_conflict",
                )
        asset, _asset_type = self._active_asset(asset_id)
        if asset.asset_type_id != requirement.asset_type_id:
            raise ApplicationValidationError(
                "Actif inactif ou incompatible.",
                code="asset_incompatible",
            )
        self._assert_asset_available(
            asset_id=asset.id,
            start_date=start_date,
            end_date=end_date,
            allocation_id=allocation_id,
        )
        candidate = AssetAllocation(
            id=allocation_id,
            asset_requirement_id=requirement.id,
            asset_id=asset.id,
            operator_resource_id=operator_resource_id,
            start_date=start_date,
            end_date=end_date,
            locked=True,
            source=source,
        )
        qualification = evaluate_asset_qualification(
            self.session,
            requirement=requirement,
            allocation=candidate,
        )
        if (
            operator_resource_id is not None
            and qualification.state != QUALIFICATION_SATISFIED
        ):
            raise self._request_operator_error(qualification.state)
        if (
            requirement.origin == AssetRequirementOrigin.RESOURCE_PERIOD.value
            and qualification.state != QUALIFICATION_SATISFIED
        ):
            raise self._request_operator_error(qualification.state)
        return qualification.state

    def _direct_result(
        self,
        *,
        requirement: AssetRequirement,
        allocation: AssetAllocation,
        qualification_state: str,
        operation: str,
    ) -> dict:
        return {
            "operation": operation,
            "requirement_id": requirement.id,
            "requirement_origin": requirement.origin,
            "allocation_id": allocation.id,
            "asset_id": allocation.asset_id,
            "project_id": requirement.project_id,
            "resource_requirement_id": requirement.resource_requirement_id,
            "context_resource_id": requirement.context_resource_id,
            "operator_resource_id": allocation.operator_resource_id,
            "start_date": allocation.start_date.isoformat(),
            "end_date": allocation.end_date.isoformat(),
            "qualification_state": qualification_state,
            "planning_version": self.version.current_version(),
        }

    def _create_direct_reservation(
        self,
        *,
        origin: AssetRequirementOrigin,
        project_id: str | None,
        context_resource_id: str | None,
        asset_type_id: str,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str | None,
        expected_version: int,
    ) -> dict:
        self.version.acquire(expected_version)
        authority_proof = self._require_assignment_authorities(asset_id)
        self._validate_direct_window(start_date, end_date)
        if origin == AssetRequirementOrigin.PROJECT_DIRECT:
            if not project_id:
                raise ApplicationValidationError(
                    "Le projet est requis.",
                    code="asset_project_required",
                )
            self._direct_project(project_id)
            context_resource_id = None
        elif origin == AssetRequirementOrigin.RESOURCE_PERIOD:
            if not context_resource_id:
                raise ApplicationValidationError(
                    "La ressource est requise.",
                    code="asset_context_resource_required",
                )
            self._direct_resource(context_resource_id)
            if operator_resource_id != context_resource_id:
                raise ApplicationConflictError(
                    "La ressource bénéficiaire doit être l'opérateur de la période.",
                    code="asset_resource_period_operator_conflict",
                )
            self._direct_project(project_id)
        else:
            raise ApplicationConflictError(
                "Origine de réservation directe invalide.",
                code="asset_direct_origin_conflict",
            )

        requirement = AssetRequirement(
            id=new_id(),
            project_id=project_id,
            origin=origin.value,
            context_resource_id=context_resource_id,
            asset_type_id=asset_type_id,
            start_date=start_date,
            end_date=end_date,
            status="Planifié",
        )
        allocation = AssetAllocation(
            id=new_id(),
            asset_requirement_id=requirement.id,
            asset_id=asset_id,
            operator_resource_id=operator_resource_id,
            start_date=start_date,
            end_date=end_date,
            locked=True,
            source="MANUAL",
        )
        qualification_state = self._validate_direct_allocation_state(
            requirement=requirement,
            asset_id=asset_id,
            start_date=start_date,
            end_date=end_date,
            operator_resource_id=operator_resource_id,
            allocation_id=allocation.id,
            source=allocation.source,
        )
        self.session.add(requirement)
        # AssetAllocation has an FK to AssetRequirement but no ORM relationship.
        # Flush the parent first so SQLite/SQL Server never observe the child first.
        self.session.flush()
        self.session.add(allocation)
        self.audit.append(
            entity_type="ASSET_ALLOCATION",
            entity_id=allocation.id,
            entity_reference=requirement.id,
            parent_reference=project_id or context_resource_id,
            action="Réservation directe d'actif",
            after={
                "origin": origin.value,
                "project_id": project_id,
                "context_resource_id": context_resource_id,
                "asset_type_id": asset_type_id,
                "asset_id": asset_id,
                "operator_resource_id": operator_resource_id,
                "start_date": start_date,
                "end_date": end_date,
                "assignment_authority": authority_proof,
            },
        )
        self.session.flush()
        return self._direct_result(
            requirement=requirement,
            allocation=allocation,
            qualification_state=qualification_state,
            operation="CREATE",
        )

    def create_project_direct_reservation(
        self,
        *,
        project_id: str,
        asset_type_id: str,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str | None,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "project_id": project_id,
            "asset_type_id": asset_type_id,
            "asset_id": asset_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "operator_resource_id": operator_resource_id,
            "expected_version": expected_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return SqlCommandIdempotencyAdapter(
            self.session,
            actor_name=self.actor,
        ).replay_or_execute(
            scope="asset_project_direct_create",
            key=idempotency_key,
            request_fingerprint=fingerprint,
            action=lambda: self._create_direct_reservation(
                origin=AssetRequirementOrigin.PROJECT_DIRECT,
                project_id=project_id,
                context_resource_id=None,
                asset_type_id=asset_type_id,
                asset_id=asset_id,
                start_date=start_date,
                end_date=end_date,
                operator_resource_id=operator_resource_id,
                expected_version=expected_version,
            ),
        )

    def create_resource_period_reservation(
        self,
        *,
        resource_id: str,
        project_id: str | None,
        asset_type_id: str,
        asset_id: str,
        start_date: date,
        end_date: date,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "resource_id": resource_id,
            "project_id": project_id,
            "asset_type_id": asset_type_id,
            "asset_id": asset_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "expected_version": expected_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return SqlCommandIdempotencyAdapter(
            self.session,
            actor_name=self.actor,
        ).replay_or_execute(
            scope="asset_resource_period_create",
            key=idempotency_key,
            request_fingerprint=fingerprint,
            action=lambda: self._create_direct_reservation(
                origin=AssetRequirementOrigin.RESOURCE_PERIOD,
                project_id=project_id,
                context_resource_id=resource_id,
                asset_type_id=asset_type_id,
                asset_id=asset_id,
                start_date=start_date,
                end_date=end_date,
                operator_resource_id=resource_id,
                expected_version=expected_version,
            ),
        )

    def _update_direct_reservation(
        self,
        *,
        requirement_id: str,
        expected_origin: AssetRequirementOrigin,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str | None,
        project_id: str | None,
        expected_version: int,
    ) -> dict:
        self.version.acquire(expected_version)
        requirement = self.session.get(AssetRequirement, requirement_id)
        if requirement is None or requirement.status == "Annulé":
            raise ApplicationNotFoundError(
                "Réservation directe introuvable.",
                code="asset_direct_reservation_not_found",
            )
        if requirement.origin != expected_origin.value:
            raise ApplicationConflictError(
                "Origine de réservation directe incompatible.",
                code="asset_direct_origin_conflict",
            )
        allocation = self._allocation_for_requirement(requirement.id)
        if allocation is None:
            raise ApplicationConflictError(
                "La réservation directe n'a plus d'allocation physique.",
                code="asset_direct_allocation_missing",
            )
        authority_proof = self._require_assignment_authorities(
            allocation.asset_id,
            asset_id,
        )

        if expected_origin == AssetRequirementOrigin.PROJECT_DIRECT:
            project_id = requirement.project_id
        else:
            self._direct_project(project_id)
            operator_resource_id = requirement.context_resource_id
            if not operator_resource_id:
                raise ApplicationConflictError(
                    "La réservation ressource-période n'a plus de bénéficiaire.",
                    code="asset_direct_context_conflict",
                )
            self._direct_resource(operator_resource_id)

        before = {
            "origin": requirement.origin,
            "project_id": requirement.project_id,
            "context_resource_id": requirement.context_resource_id,
            "asset_id": allocation.asset_id,
            "operator_resource_id": allocation.operator_resource_id,
            "start_date": allocation.start_date,
            "end_date": allocation.end_date,
        }
        if expected_origin == AssetRequirementOrigin.RESOURCE_PERIOD:
            requirement.project_id = project_id
        qualification_state = self._validate_direct_allocation_state(
            requirement=requirement,
            asset_id=asset_id,
            start_date=start_date,
            end_date=end_date,
            operator_resource_id=operator_resource_id,
            allocation_id=allocation.id,
            source=allocation.source,
        )
        requirement.start_date = start_date
        requirement.end_date = end_date
        requirement.status = "Planifié"
        allocation.asset_id = asset_id
        allocation.operator_resource_id = operator_resource_id
        allocation.start_date = start_date
        allocation.end_date = end_date
        allocation.locked = True
        self.audit.append(
            entity_type="ASSET_ALLOCATION",
            entity_id=allocation.id,
            entity_reference=requirement.id,
            parent_reference=(
                requirement.resource_requirement_id
                if expected_origin == AssetRequirementOrigin.SEGMENT
                else requirement.project_id or requirement.context_resource_id
            ),
            action="Modification réservation directe",
            before=before,
            after={
                "origin": requirement.origin,
                "project_id": requirement.project_id,
                "context_resource_id": requirement.context_resource_id,
                "asset_id": allocation.asset_id,
                "operator_resource_id": allocation.operator_resource_id,
                "start_date": allocation.start_date,
                "end_date": allocation.end_date,
                "assignment_authority": authority_proof,
            },
        )
        self.session.flush()
        return self._direct_result(
            requirement=requirement,
            allocation=allocation,
            qualification_state=qualification_state,
            operation="UPDATE",
        )

    def update_project_direct_reservation(
        self,
        *,
        requirement_id: str,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str | None,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "requirement_id": requirement_id,
            "asset_id": asset_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "operator_resource_id": operator_resource_id,
            "expected_version": expected_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return SqlCommandIdempotencyAdapter(
            self.session,
            actor_name=self.actor,
        ).replay_or_execute(
            scope="asset_project_direct_update",
            key=idempotency_key,
            request_fingerprint=fingerprint,
            action=lambda: self._update_direct_reservation(
                requirement_id=requirement_id,
                expected_origin=AssetRequirementOrigin.PROJECT_DIRECT,
                asset_id=asset_id,
                start_date=start_date,
                end_date=end_date,
                operator_resource_id=operator_resource_id,
                project_id=None,
                expected_version=expected_version,
            ),
        )

    def update_resource_period_reservation(
        self,
        *,
        requirement_id: str,
        project_id: str | None,
        asset_id: str,
        start_date: date,
        end_date: date,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "requirement_id": requirement_id,
            "project_id": project_id,
            "asset_id": asset_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "expected_version": expected_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return SqlCommandIdempotencyAdapter(
            self.session,
            actor_name=self.actor,
        ).replay_or_execute(
            scope="asset_resource_period_update",
            key=idempotency_key,
            request_fingerprint=fingerprint,
            action=lambda: self._update_direct_reservation(
                requirement_id=requirement_id,
                expected_origin=AssetRequirementOrigin.RESOURCE_PERIOD,
                asset_id=asset_id,
                start_date=start_date,
                end_date=end_date,
                operator_resource_id=None,
                project_id=project_id,
                expected_version=expected_version,
            ),
        )

    def _release_direct_reservation(
        self,
        *,
        requirement_id: str,
        expected_origin: AssetRequirementOrigin,
        expected_version: int,
    ) -> dict:
        self.version.acquire(expected_version)
        requirement = self.session.get(AssetRequirement, requirement_id)
        if requirement is None or requirement.status == "Annulé":
            raise ApplicationNotFoundError(
                "Réservation directe introuvable.",
                code="asset_direct_reservation_not_found",
            )
        if requirement.origin != expected_origin.value:
            raise ApplicationConflictError(
                "Origine de réservation directe incompatible.",
                code="asset_direct_origin_conflict",
            )
        allocation = self._allocation_for_requirement(requirement.id)
        allocation_id = allocation.id if allocation is not None else None
        authority_proof = self._require_assignment_authorities(
            allocation.asset_id if allocation is not None else None
        )
        before = (
            {
                "origin": requirement.origin,
                "project_id": requirement.project_id,
                "context_resource_id": requirement.context_resource_id,
                "asset_id": allocation.asset_id,
                "operator_resource_id": allocation.operator_resource_id,
                "start_date": allocation.start_date,
                "end_date": allocation.end_date,
                "assignment_authority": authority_proof,
            }
            if allocation is not None
            else {
                "origin": requirement.origin,
                "project_id": requirement.project_id,
                "context_resource_id": requirement.context_resource_id,
            }
        )
        self.audit.append(
            entity_type="ASSET_ALLOCATION",
            entity_id=allocation_id or requirement.id,
            entity_reference=requirement.id,
            parent_reference=requirement.project_id or requirement.context_resource_id,
            action=(
                "Libération réservation segment"
                if expected_origin == AssetRequirementOrigin.SEGMENT
                else "Libération réservation directe"
            ),
            before=before,
        )
        if allocation is not None:
            self.session.delete(allocation)
            self.session.flush()
        self.session.delete(requirement)
        self.session.flush()
        return {
            "operation": "RELEASE",
            "requirement_id": requirement_id,
            "requirement_origin": expected_origin.value,
            "allocation_id": allocation_id,
            "planning_version": self.version.current_version(),
        }

    def _release_direct(
        self,
        *,
        requirement_id: str,
        expected_origin: AssetRequirementOrigin,
        expected_version: int,
        idempotency_key: str,
        scope: str,
    ) -> dict:
        payload = {
            "requirement_id": requirement_id,
            "expected_version": expected_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return SqlCommandIdempotencyAdapter(
            self.session,
            actor_name=self.actor,
        ).replay_or_execute(
            scope=scope,
            key=idempotency_key,
            request_fingerprint=fingerprint,
            action=lambda: self._release_direct_reservation(
                requirement_id=requirement_id,
                expected_origin=expected_origin,
                expected_version=expected_version,
            ),
        )

    def release_project_direct_reservation(
        self,
        *,
        requirement_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        return self._release_direct(
            requirement_id=requirement_id,
            expected_origin=AssetRequirementOrigin.PROJECT_DIRECT,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            scope="asset_project_direct_release",
        )

    def release_resource_period_reservation(
        self,
        *,
        requirement_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        return self._release_direct(
            requirement_id=requirement_id,
            expected_origin=AssetRequirementOrigin.RESOURCE_PERIOD,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            scope="asset_resource_period_release",
        )

    def _segment_context(self, identifier: str) -> ResourceRequirement:
        wanted = str(identifier or "").strip()
        segment = self.session.scalar(
            select(ResourceRequirement).where(
                (ResourceRequirement.id == wanted)
                | (ResourceRequirement.legacy_segment_id == wanted)
            )
        )
        if segment is None or segment.status == "Annulé":
            raise ApplicationNotFoundError(
                "Segment introuvable ou annulé.",
                code="asset_segment_not_found",
            )
        if not segment.project_id:
            raise ApplicationConflictError(
                "Le segment n'a pas de projet propriétaire.",
                code="asset_segment_context_conflict",
            )
        return segment

    def _validate_segment_allocation_state(
        self,
        *,
        requirement: AssetRequirement,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str | None,
        allocation_id: str,
        source: str = "MANUAL",
    ) -> str:
        if (
            requirement.origin != AssetRequirementOrigin.SEGMENT.value
            or not requirement.resource_requirement_id
        ):
            raise ApplicationConflictError(
                "Le besoin d'actif n'est pas une réservation de segment.",
                code="asset_segment_origin_conflict",
            )
        segment = self._segment_context(requirement.resource_requirement_id)
        if requirement.project_id != segment.project_id:
            raise ApplicationConflictError(
                "Le projet du besoin d'actif ne correspond plus au segment.",
                code="asset_segment_project_conflict",
            )
        self._validate_direct_window(start_date, end_date)
        if not (
            segment.start_date
            <= start_date
            <= end_date
            <= segment.end_date
        ):
            raise ApplicationValidationError(
                "Réservation hors fenêtre du segment.",
                code="asset_outside_segment_window",
            )
        operator_id = str(operator_resource_id or "").strip()
        if not operator_id:
            raise ApplicationValidationError(
                "Un opérateur explicite est requis pour une réservation de segment.",
                code="asset_segment_operator_required",
            )
        self._direct_resource(operator_id)
        asset, _asset_type = self._active_asset(asset_id)
        if asset.asset_type_id != requirement.asset_type_id:
            raise ApplicationValidationError(
                "Actif inactif ou incompatible.",
                code="asset_incompatible",
            )
        self._assert_asset_available(
            asset_id=asset.id,
            start_date=start_date,
            end_date=end_date,
            allocation_id=allocation_id,
        )
        candidate = AssetAllocation(
            id=allocation_id,
            asset_requirement_id=requirement.id,
            asset_id=asset.id,
            operator_resource_id=operator_id,
            start_date=start_date,
            end_date=end_date,
            locked=True,
            source=source,
        )
        qualification = evaluate_asset_qualification(
            self.session,
            requirement=requirement,
            allocation=candidate,
        )
        if qualification.state != QUALIFICATION_SATISFIED:
            raise self._request_operator_error(qualification.state)
        return qualification.state

    def _create_segment_reservation(
        self,
        *,
        segment_id: str,
        asset_type_id: str,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str,
        expected_version: int,
    ) -> dict:
        self.version.acquire(expected_version)
        authority_proof = self._require_assignment_authorities(asset_id)
        segment = self._segment_context(segment_id)
        requirement = AssetRequirement(
            id=new_id(),
            project_id=segment.project_id,
            origin=AssetRequirementOrigin.SEGMENT.value,
            resource_requirement_id=segment.id,
            asset_type_id=asset_type_id,
            start_date=start_date,
            end_date=end_date,
            status="Planifié",
        )
        allocation = AssetAllocation(
            id=new_id(),
            asset_requirement_id=requirement.id,
            asset_id=asset_id,
            operator_resource_id=operator_resource_id,
            start_date=start_date,
            end_date=end_date,
            locked=True,
            source="MANUAL",
        )
        qualification_state = self._validate_segment_allocation_state(
            requirement=requirement,
            asset_id=asset_id,
            start_date=start_date,
            end_date=end_date,
            operator_resource_id=operator_resource_id,
            allocation_id=allocation.id,
            source=allocation.source,
        )
        self.session.add(requirement)
        self.session.flush()
        self.session.add(allocation)
        self.audit.append(
            entity_type="ASSET_ALLOCATION",
            entity_id=allocation.id,
            entity_reference=requirement.id,
            parent_reference=segment.id,
            action="Réservation d'actif sur segment",
            after={
                "origin": requirement.origin,
                "resource_requirement_id": segment.id,
                "project_id": segment.project_id,
                "asset_type_id": asset_type_id,
                "asset_id": asset_id,
                "operator_resource_id": operator_resource_id,
                "start_date": start_date,
                "end_date": end_date,
                "assignment_authority": authority_proof,
            },
        )
        self.session.flush()
        return self._direct_result(
            requirement=requirement,
            allocation=allocation,
            qualification_state=qualification_state,
            operation="CREATE",
        )

    def create_segment_reservation(
        self,
        *,
        segment_id: str,
        asset_type_id: str,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "segment_id": segment_id,
            "asset_type_id": asset_type_id,
            "asset_id": asset_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "operator_resource_id": operator_resource_id,
            "expected_version": expected_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return SqlCommandIdempotencyAdapter(
            self.session,
            actor_name=self.actor,
        ).replay_or_execute(
            scope="asset_segment_create",
            key=idempotency_key,
            request_fingerprint=fingerprint,
            action=lambda: self._create_segment_reservation(
                segment_id=segment_id,
                asset_type_id=asset_type_id,
                asset_id=asset_id,
                start_date=start_date,
                end_date=end_date,
                operator_resource_id=operator_resource_id,
                expected_version=expected_version,
            ),
        )

    def _update_segment_reservation(
        self,
        *,
        requirement_id: str,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str,
        expected_version: int,
    ) -> dict:
        self.version.acquire(expected_version)
        requirement = self.session.get(AssetRequirement, requirement_id)
        if (
            requirement is None
            or requirement.status == "Annulé"
            or requirement.origin != AssetRequirementOrigin.SEGMENT.value
        ):
            raise ApplicationNotFoundError(
                "Réservation de segment introuvable.",
                code="asset_segment_reservation_not_found",
            )
        allocation = self._allocation_for_requirement(requirement.id)
        if allocation is None:
            raise ApplicationConflictError(
                "La réservation physique du segment est introuvable.",
                code="asset_segment_allocation_missing",
            )
        authority_proof = self._require_assignment_authorities(
            allocation.asset_id,
            asset_id,
        )
        before = {
            "asset_id": allocation.asset_id,
            "operator_resource_id": allocation.operator_resource_id,
            "start_date": allocation.start_date,
            "end_date": allocation.end_date,
        }
        qualification_state = self._validate_segment_allocation_state(
            requirement=requirement,
            asset_id=asset_id,
            start_date=start_date,
            end_date=end_date,
            operator_resource_id=operator_resource_id,
            allocation_id=allocation.id,
            source=allocation.source,
        )
        requirement.start_date = start_date
        requirement.end_date = end_date
        allocation.asset_id = asset_id
        allocation.operator_resource_id = operator_resource_id
        allocation.start_date = start_date
        allocation.end_date = end_date
        allocation.locked = True
        self.audit.append(
            entity_type="ASSET_ALLOCATION",
            entity_id=allocation.id,
            entity_reference=requirement.id,
            parent_reference=requirement.resource_requirement_id,
            action="Modification réservation d'actif sur segment",
            before=before,
            after={
                "asset_id": asset_id,
                "operator_resource_id": operator_resource_id,
                "start_date": start_date,
                "end_date": end_date,
                "assignment_authority": authority_proof,
            },
        )
        self.session.flush()
        return self._direct_result(
            requirement=requirement,
            allocation=allocation,
            qualification_state=qualification_state,
            operation="UPDATE",
        )

    def update_segment_reservation(
        self,
        *,
        requirement_id: str,
        asset_id: str,
        start_date: date,
        end_date: date,
        operator_resource_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "requirement_id": requirement_id,
            "asset_id": asset_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "operator_resource_id": operator_resource_id,
            "expected_version": expected_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return SqlCommandIdempotencyAdapter(
            self.session,
            actor_name=self.actor,
        ).replay_or_execute(
            scope="asset_segment_update",
            key=idempotency_key,
            request_fingerprint=fingerprint,
            action=lambda: self._update_segment_reservation(
                requirement_id=requirement_id,
                asset_id=asset_id,
                start_date=start_date,
                end_date=end_date,
                operator_resource_id=operator_resource_id,
                expected_version=expected_version,
            ),
        )

    def release_segment_reservation(
        self,
        *,
        requirement_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        return self._release_direct(
            requirement_id=requirement_id,
            expected_origin=AssetRequirementOrigin.SEGMENT,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            scope="asset_segment_release",
        )

    def add_unavailability(self, *, asset_id: str, start_date: date, end_date: date,
                           reason: str | None, expected_version: int) -> dict:
        self.version.acquire(expected_version)
        if end_date < start_date:
            raise ApplicationValidationError("Fenêtre invalide.", code="asset_window_invalid")
        asset = self.session.get(Asset, asset_id)
        if asset is None:
            raise ApplicationNotFoundError("Actif introuvable.", code="asset_not_found")
        row = AssetUnavailability(id=new_id(), asset_id=asset_id, start_date=start_date, end_date=end_date, reason=reason)
        self.session.add(row)
        self.audit.append(entity_type="ASSET_UNAVAILABILITY", entity_id=row.id, entity_reference=asset.code,
                          action="Indisponibilité", after={"start_date": start_date, "end_date": end_date, "reason": reason})
        self.session.flush()
        return {"id": row.id, "planning_version": self.version.current_version()}

    def remove_unavailability(self, *, asset_id: str, identifier: str, expected_version: int) -> dict:
        self.version.acquire(expected_version)
        row = self.session.get(AssetUnavailability, identifier)
        if row is None or row.asset_id != asset_id:
            raise ApplicationNotFoundError("Indisponibilité introuvable.", code="asset_unavailability_not_found")
        self.audit.append(entity_type="ASSET_UNAVAILABILITY", entity_id=row.id,
                          entity_reference=asset_id, action="Retrait indisponibilité",
                          before={"start_date": row.start_date, "end_date": row.end_date, "reason": row.reason})
        self.session.delete(row)
        self.session.flush()
        return {"id": identifier, "planning_version": self.version.current_version()}

    def reserve(self, *, requirement_id: str, asset_id: str | None, start_date: date | None,
                end_date: date | None, expected_version: int, idempotency_key: str) -> dict:
        payload = {"requirement_id": requirement_id, "asset_id": asset_id, "start_date": start_date.isoformat() if start_date else None,
                   "end_date": end_date.isoformat() if end_date else None, "expected_version": expected_version}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return SqlCommandIdempotencyAdapter(self.session, actor_name=self.actor).replay_or_execute(
            scope="asset_reservation", key=idempotency_key, request_fingerprint=fingerprint,
            action=lambda: self._reserve(**payload),
        )


    def _reserve(self, *, requirement_id: str, asset_id: str | None, start_date: str | None,
                 end_date: str | None, expected_version: int) -> dict:
        self.version.acquire(expected_version)
        requirement = self.session.get(AssetRequirement, requirement_id)
        if requirement is None or requirement.status == "Annulé":
            raise ApplicationNotFoundError("Besoin d'actif introuvable.", code="asset_requirement_not_found")
        self._validate_request_authority(requirement)
        if (start_date is None) != (end_date is None):
            raise ApplicationValidationError(
                "Les dates réelles de réservation doivent être fournies ensemble.",
                code="asset_reservation_dates_required",
            )
        previous = self._allocation_for_requirement(requirement.id)
        authority_proof = self._require_assignment_authorities(
            previous.asset_id if previous is not None else None,
            asset_id,
        )
        before = (
            {
                "asset_id": previous.asset_id,
                "start_date": previous.start_date,
                "end_date": previous.end_date,
                "operator_resource_id": previous.operator_resource_id,
                "assignment_authority": authority_proof,
            }
            if previous
            else None
        )
        if asset_id is None:
            if previous is not None:
                self.session.delete(previous)
            requirement.status = "À affecter"
            result_id = None
            after = None
        else:
            begin, end = self._resolve_request_reservation_dates(
                requirement=requirement,
                previous=previous,
                start_date=start_date,
                end_date=end_date,
            )
            allocation_id = previous.id if previous else new_id()
            operator_resource_id = (
                previous.operator_resource_id if previous is not None else None
            )
            self._validate_request_allocation_state(
                requirement=requirement,
                asset_id=asset_id,
                start_date=begin,
                end_date=end,
                operator_resource_id=operator_resource_id,
                allocation_id=allocation_id,
                source=previous.source if previous is not None else "MANUAL",
            )
            row = previous or AssetAllocation(
                id=allocation_id,
                asset_requirement_id=requirement.id,
            )
            row.asset_id = asset_id
            row.start_date = begin
            row.end_date = end
            row.operator_resource_id = operator_resource_id
            row.locked = True
            self.session.add(row)
            requirement.status = "Planifié"
            result_id = row.id
            after = {
                "asset_id": asset_id,
                "start_date": begin,
                "end_date": end,
                "operator_resource_id": operator_resource_id,
                "assignment_authority": authority_proof,
            }
        self.audit.append(
            entity_type="ASSET_ALLOCATION",
            entity_id=result_id or (previous.id if previous else requirement.id),
            entity_reference=requirement.id,
            parent_reference=requirement.workforce_request_id,
            action="Réservation d'actif",
            before=before,
            after=after,
        )
        self.session.flush()
        return {
            "allocation_id": result_id,
            "requirement_id": requirement.id,
            "planning_version": self.version.current_version(),
        }