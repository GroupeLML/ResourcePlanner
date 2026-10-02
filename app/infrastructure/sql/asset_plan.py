"""Materialize asset needs from the same immutable authorization as human needs."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...domain.approval_envelope import ApprovalEnvelope, approval_envelope_from_snapshot_payload
from ...domain.reservable_assets import AssetRequirementOrigin
from .asset_models import Asset, AssetAllocation, AssetRequirement, AssetType
from .approval_revision_models import RequestApprovalRevision
from .base import new_id
from .models import WorkforceRequest
from .operational_choice_repository import SqlRequestOperationalChoiceRepository


class SqlAssetPlanSynchronizer:
    def __init__(self, session: Session) -> None:
        self.session = session

    def from_active_revision(self, request: WorkforceRequest) -> None:
        choices = SqlRequestOperationalChoiceRepository(self.session).state_for_request_id(request.id)
        if choices is None:
            raise ValueError("La révision approuvée active est absente.")
        revision = self.session.get(RequestApprovalRevision, choices.approval_revision_id)
        if revision is None:
            raise ValueError("La révision approuvée active est introuvable.")
        payload = json.loads(revision.payload_text)
        envelope = approval_envelope_from_snapshot_payload(payload["authorization"])
        self.sync(request, envelope, revision, selections=choices.selections)

    def sync(
        self,
        request: WorkforceRequest,
        envelope: ApprovalEnvelope,
        revision: RequestApprovalRevision,
        *,
        selections: dict[str, str] | None = None,
    ) -> None:
        current = self.session.scalars(
            select(AssetRequirement).where(
                AssetRequirement.workforce_request_id == request.id,
                AssetRequirement.origin == AssetRequirementOrigin.REQUEST.value,
            )
        ).all()
        by_key = {(row.approved_entry_key, row.slot_index): row for row in current}
        retained: set[str] = set()
        for entry in envelope.entries:
            if entry.line_kind != "ASSET":
                continue
            if entry.group is not None:
                selected = (selections.get(entry.group.stable_key) == entry.identity.stable_key
                            if selections is not None else entry.selected)
                if not selected:
                    continue
            asset_type = self.session.get(AssetType, entry.asset_type_id)
            if asset_type is None or not asset_type.active or asset_type.occupancy_policy != entry.occupancy_policy:
                raise ValueError("Le type d'actif approuvé est inactif ou incompatible.")
            for slot in range(entry.slot_count):
                key = (entry.identity.stable_key, slot)
                row = by_key.get(key)
                if row is None:
                    row = AssetRequirement(
                        id=new_id(), project_id=entry.project_id,
                        origin=AssetRequirementOrigin.REQUEST.value,
                        workforce_request_id=request.id,
                        source_request_line_id=entry.identity.line_id,
                        approved_entry_key=entry.identity.stable_key,
                        slot_index=slot, asset_type_id=asset_type.id,
                        start_date=entry.start_date, end_date=entry.end_date,
                    )
                    self.session.add(row)
                    self.session.flush()
                allocations = self.session.scalars(
                    select(AssetAllocation).where(AssetAllocation.asset_requirement_id == row.id)
                ).all()
                kept = []
                for allocation in allocations:
                    asset = self.session.get(Asset, allocation.asset_id)
                    compatible = (asset is not None and asset.asset_type_id == asset_type.id
                                  and entry.start_date <= allocation.start_date
                                  and allocation.end_date <= entry.end_date)
                    if not compatible:
                        if allocation.locked:
                            raise ValueError("La réapprobation rendrait une allocation verrouillée incompatible.")
                        self.session.delete(allocation)
                    else:
                        kept.append(allocation)
                row.project_id = entry.project_id
                row.asset_type_id = asset_type.id
                row.approval_revision_id = revision.id
                row.source_period_id = entry.source_period_id
                row.start_date = entry.start_date
                row.end_date = entry.end_date
                row.usage_hours = entry.hours
                row.status = "Planifié" if kept else "À affecter"
                retained.add(row.id)
        for row in current:
            if row.id in retained:
                continue
            allocations = self.session.scalars(select(AssetAllocation).where(AssetAllocation.asset_requirement_id == row.id)).all()
            if any(item.locked for item in allocations):
                raise ValueError("Une allocation verrouillée empêche de retirer le besoin d'actif.")
            for allocation in allocations:
                self.session.delete(allocation)
            self.session.delete(row)
        self.session.flush()

    def cancel(self, request: WorkforceRequest) -> None:
        rows = self.session.scalars(
            select(AssetRequirement).where(
                AssetRequirement.workforce_request_id == request.id,
                AssetRequirement.origin == AssetRequirementOrigin.REQUEST.value,
            )
        ).all()
        for row in rows:
            allocations = self.session.scalars(select(AssetAllocation).where(AssetAllocation.asset_requirement_id == row.id)).all()
            if any(item.locked for item in allocations):
                raise ValueError("Une allocation verrouillée empêche l'annulation.")
            for item in allocations:
                self.session.delete(item)
            row.status = "Annulé"
        self.session.flush()
