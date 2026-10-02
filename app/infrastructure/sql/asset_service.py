"""Catalog and globally serialized physical asset reservations."""

from __future__ import annotations

from datetime import date
import hashlib
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...application.errors import ApplicationConflictError, ApplicationNotFoundError, ApplicationValidationError
from ...application.security import PERMISSION_APPROVE_DEMANDS, normalize_roles, permissions_for_roles
from ...domain.reservable_assets import (
    AssetOccupation,
    AssetRequirementOrigin,
    overlapping_asset_occupations,
)
from ...domain.approval_envelope import approval_envelope_from_snapshot_payload
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
from .models import Competency, ResourceRequirement, Shift
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
        elif "asset_type_id" in updates:
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

        before = {"operator_resource_id": allocation.operator_resource_id}
        previous = allocation.operator_resource_id
        allocation.operator_resource_id = (
            str(operator_resource_id or "").strip() or None
        )
        qualification = evaluate_asset_qualification(
            self.session,
            requirement=requirement,
            allocation=allocation,
        )
        if allocation.operator_resource_id is not None:
            if qualification.state == QUALIFICATION_SKILL_MISMATCH:
                allocation.operator_resource_id = previous
                raise ApplicationValidationError(
                    "La ressource choisie n'est pas active ou ne possède pas les compétences requises.",
                    code="asset_operator_skill_mismatch",
                )
            if qualification.state == QUALIFICATION_NO_OVERLAP:
                allocation.operator_resource_id = previous
                raise ApplicationValidationError(
                    "La ressource choisie n'a aucune affectation compatible sur cette réservation.",
                    code="asset_operator_no_overlap",
                )
            if qualification.state != QUALIFICATION_SATISFIED:
                allocation.operator_resource_id = previous
                raise ApplicationValidationError(
                    "La ressource choisie ne satisfait pas la qualification d'actif.",
                    code="asset_operator_invalid",
                )

        self.audit.append(
            entity_type="ASSET_ALLOCATION",
            entity_id=allocation.id,
            entity_reference=requirement.id,
            parent_reference=requirement.workforce_request_id,
            action="Opérateur qualifiant",
            before=before,
            after={"operator_resource_id": allocation.operator_resource_id},
        )
        self.session.flush()
        qualification = evaluate_asset_qualification(
            self.session,
            requirement=requirement,
            allocation=allocation,
        )
        return {
            "allocation_id": allocation.id,
            "requirement_id": requirement.id,
            "operator_resource_id": allocation.operator_resource_id,
            "qualification_state": qualification.state,
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
        expected_version: int,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "shift_id": str(shift_id or "").strip(),
            "asset_id": str(asset_id or "").strip() or None,
            "requirement_id": str(requirement_id or "").strip() or None,
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

        previous = self._allocation_for_requirement(requirement.id)
        requirement_id_value = requirement.id
        before = (
            {
                "asset_id": previous.asset_id,
                "start_date": previous.start_date,
                "end_date": previous.end_date,
                "operator_resource_id": previous.operator_resource_id,
                "origin": origin,
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

        if origin == AssetRequirementOrigin.REQUEST.value:
            if asset.asset_type_id != requirement.asset_type_id:
                raise ApplicationValidationError(
                    "Actif inactif ou incompatible.",
                    code="asset_incompatible",
                )
            begin = requirement.start_date
            end = requirement.end_date
            operator_resource_id = (
                shift.resource_id
                if required_competencies(
                    self.session,
                    requirement.asset_type_id,
                )
                else (
                    previous.operator_resource_id
                    if previous is not None
                    else None
                )
            )
        else:
            requirement.asset_type_id = asset.asset_type_id
            requirement.start_date = shift.work_date
            requirement.end_date = shift.work_date
            begin = shift.work_date
            end = shift.work_date
            operator_resource_id = shift.resource_id

        allocation_id = previous.id if previous is not None else new_id()
        self._assert_asset_available(
            asset_id=asset.id,
            start_date=begin,
            end_date=end,
            allocation_id=allocation_id,
        )
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
        qualification = evaluate_asset_qualification(
            self.session,
            requirement=requirement,
            allocation=candidate,
        )
        if qualification.state != QUALIFICATION_SATISFIED:
            raise self._qualification_error(qualification.state)

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
            "qualification_state": qualification.state,
            "shift_source": shift.source,
            "shift_locked": bool(shift.locked),
            "planning_version": self.version.current_version(),
        }

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
        reference = self.session.get(RequestApprovalReference, requirement.workforce_request_id)
        if reference is None or requirement.approval_revision_id != reference.active_revision_id:
            raise ApplicationConflictError("Révision approuvée obsolète.", code="asset_approval_revision_conflict")
        revision = self.session.get(RequestApprovalRevision, reference.active_revision_id)
        choices = SqlRequestOperationalChoiceRepository(self.session).state_for_request_id(requirement.workforce_request_id)
        if revision is None or choices is None or choices.approval_revision_id != revision.id:
            raise ApplicationConflictError("Autorisation opérationnelle absente.", code="asset_approval_revision_conflict")
        envelope = approval_envelope_from_snapshot_payload(json.loads(revision.payload_text)["authorization"])
        approved = next((entry for entry in envelope.entries
                         if entry.identity.stable_key == requirement.approved_entry_key and entry.line_kind == "ASSET"), None)
        if (approved is None or approved.asset_type_id != requirement.asset_type_id
                or approved.project_id != requirement.project_id
                or requirement.start_date < approved.start_date or requirement.end_date > approved.end_date
                or (approved.group is not None and choices.selections.get(approved.group.stable_key) != approved.identity.stable_key)):
            raise ApplicationConflictError("Le besoin ne correspond plus à l'autorisation active.", code="asset_approval_entry_conflict")
        current = self.session.scalars(select(AssetAllocation).where(AssetAllocation.asset_requirement_id == requirement.id)).all()
        if len(current) > 1:
            raise ApplicationConflictError("Plusieurs allocations pour un slot.", code="asset_slot_conflict")
        previous = current[0] if current else None
        before = ({"asset_id": previous.asset_id, "start_date": previous.start_date, "end_date": previous.end_date}
                  if previous else None)
        if asset_id is None:
            if previous is not None:
                self.session.delete(previous)
            requirement.status = "À affecter"
            result_id = None
            after = None
        else:
            asset = self.session.get(Asset, asset_id)
            asset_type = self.session.get(AssetType, requirement.asset_type_id)
            if asset is None or not asset.active or asset_type is None or not asset_type.active or asset.asset_type_id != asset_type.id:
                raise ApplicationValidationError("Actif inactif ou incompatible.", code="asset_incompatible")
            begin = date.fromisoformat(start_date) if start_date else requirement.start_date
            end = date.fromisoformat(end_date) if end_date else requirement.end_date
            if not (requirement.start_date <= begin <= end <= requirement.end_date):
                raise ApplicationValidationError("Réservation hors fenêtre approuvée.", code="asset_outside_approved_window")
            candidate = AssetOccupation(previous.id if previous else new_id(), asset_id, begin, end)
            existing = self.session.scalars(select(AssetAllocation).where(
                AssetAllocation.asset_id == asset_id, AssetAllocation.start_date <= end, AssetAllocation.end_date >= begin
            )).all()
            conflicts = overlapping_asset_occupations(candidate, (
                AssetOccupation(row.id, row.asset_id, row.start_date, row.end_date, row.locked) for row in existing
            ))
            if conflicts:
                raise ApplicationConflictError("Actif déjà réservé.", code="asset_double_booking", context={"allocation_ids": conflicts})
            unavailable = self.session.scalar(select(AssetUnavailability.id).where(
                AssetUnavailability.asset_id == asset_id, AssetUnavailability.start_date <= end,
                AssetUnavailability.end_date >= begin,
            ))
            if unavailable:
                raise ApplicationConflictError("Actif indisponible.", code="asset_unavailable")
            row = previous or AssetAllocation(id=candidate.allocation_id, asset_requirement_id=requirement.id)
            row.asset_id, row.start_date, row.end_date = asset_id, begin, end
            row.locked = True  # An explicit coordinator decision survives rebuild/reapproval.
            self.session.add(row)
            requirement.status = "Planifié"
            result_id = row.id
            after = {"asset_id": asset_id, "start_date": begin, "end_date": end}
        self.audit.append(entity_type="ASSET_ALLOCATION", entity_id=result_id or (previous.id if previous else requirement.id),
                          entity_reference=requirement.id, parent_reference=requirement.workforce_request_id,
                          action="Réservation d'actif", before=before, after=after)
        self.session.flush()
        return {"allocation_id": result_id, "requirement_id": requirement.id,
                "planning_version": self.version.current_version()}
