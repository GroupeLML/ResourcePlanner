from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal
import unittest

from sqlalchemy import event

from app.infrastructure.sql import (
    AppUser,
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
    Base,
    BusinessContact,
    Competency,
    Project,
    ProjectCoManager,
    Resource,
    ResourceAvailabilityRule,
    ResourceCompetency,
    ResourceRequirement,
    Shift,
    SqlDemandRepository,
    SqlPlannerQueryRepository,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


D1 = date(2026, 8, 24)
D2 = date(2026, 8, 25)