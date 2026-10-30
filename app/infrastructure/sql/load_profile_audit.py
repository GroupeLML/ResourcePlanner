from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...application.read_models import SegmentReadModel
from ...application.repository_ports import SegmentRepositoryPort
from ...domain.load_profiles import normalize_load_profile
from .planning_audit import ENTITY_SEGMENT, SqlPlanningAuditJournal


class LoadProfileAuditedSegmentRepository(SegmentRepositoryPort):
    """Journal explicit load-profile decisions without duplicating unrelated updates."""

    def __init__(self, delegate: SegmentRepositoryPort, journal: SqlPlanningAuditJournal) -> None:
        self._delegate = delegate
        self._journal = journal

    def list(self, *, include_cancelled: bool = True) -> Sequence[SegmentReadModel]:
        return self._delegate.list(include_cancelled=include_cancelled)

    def get(self, segment_id: str) -> SegmentReadModel | None:
        return self._delegate.get(segment_id)

    def create(self, values: Mapping[str, Any]) -> str:
        return self._delegate.create(values)

    def planned_hours_overallocation_context(
        self,
        segment_id: str,
        proposed_base_hours: float,
    ) -> Mapping[str, Any] | None:
        action = getattr(self._delegate, "planned_hours_overallocation_context", None)
        return (
            action(segment_id, proposed_base_hours)
            if callable(action)
            else None
        )

    def update(self, segment_id: str, updates: Mapping[str, Any]) -> None:
        before = self._delegate.get(segment_id)
        self._delegate.update(segment_id, updates)
        after = self._delegate.get(segment_id)
        if before is None or after is None:
            return
        before_profile = normalize_load_profile(before.load_profile)
        after_profile = normalize_load_profile(after.load_profile)
        if before_profile == after_profile:
            return
        identity = self._journal.requirement_snapshot(segment_id)
        if identity is None:
            return
        entity_id, entity_reference, _snapshot = identity
        self._journal.append(
            entity_type=ENTITY_SEGMENT,
            entity_id=entity_id,
            entity_reference=entity_reference,
            action="Modification profil de charge",
            before={"load_profile": before_profile},
            after={"load_profile": after_profile},
        )
