from __future__ import annotations

from sqlalchemy.orm import Session

from .emergency_demand_repository import SqlEmergencyDemandRepository
from .plan_delta_query_repository import SqlPlannerQueryRepositoryWithPlanDelta


class SqlPlannerQueryRepositoryWithEmergencyOverride(
    SqlPlannerQueryRepositoryWithPlanDelta
):
    """Canonical web reads enriched with emergency-override indicators."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self._emergency_session = session
        self._demands = SqlEmergencyDemandRepository(session)

    def list_shifts(self, **kwargs):
        # The base read already joins WorkforceRequest and projects the canonical
        # emergency flag, so no second request lookup is required here.
        return super().list_shifts(**kwargs)
