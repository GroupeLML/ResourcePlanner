from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .erp_sync_evidence_models import ErpSyncEvidenceReservation


@dataclass(frozen=True, slots=True)
class ErpSyncEvidenceStorageAccounting:
    active_reserved_bytes: int
    active_used_bytes: int
    retained_used_bytes: int
    charged_bytes: int
    budget_bytes: int | None
    remaining_bytes: int | None

    def can_admit(self, requested_bytes: int) -> bool:
        if self.budget_bytes is None:
            return False
        return self.charged_bytes + max(0, int(requested_bytes)) <= self.budget_bytes


def read_erp_sync_evidence_storage_accounting(
    session: Session,
    *,
    budget_bytes: int | None,
) -> ErpSyncEvidenceStorageAccounting:
    active_reserved = int(
        session.scalar(
            select(
                func.coalesce(
                    func.sum(ErpSyncEvidenceReservation.reserved_bytes),
                    0,
                )
            ).where(ErpSyncEvidenceReservation.released_at.is_(None))
        )
        or 0
    )
    active_used = int(
        session.scalar(
            select(
                func.coalesce(
                    func.sum(ErpSyncEvidenceReservation.used_bytes),
                    0,
                )
            ).where(ErpSyncEvidenceReservation.released_at.is_(None))
        )
        or 0
    )
    retained_used = int(
        session.scalar(
            select(
                func.coalesce(
                    func.sum(ErpSyncEvidenceReservation.used_bytes),
                    0,
                )
            ).where(ErpSyncEvidenceReservation.released_at.is_not(None))
        )
        or 0
    )
    charged = retained_used + active_reserved
    normalized_budget = (
        int(budget_bytes)
        if budget_bytes is not None and int(budget_bytes) >= 0
        else None
    )
    remaining = (
        max(0, normalized_budget - charged)
        if normalized_budget is not None
        else None
    )
    return ErpSyncEvidenceStorageAccounting(
        active_reserved_bytes=active_reserved,
        active_used_bytes=active_used,
        retained_used_bytes=retained_used,
        charged_bytes=charged,
        budget_bytes=normalized_budget,
        remaining_bytes=remaining,
    )
