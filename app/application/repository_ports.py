from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Protocol

from ..domain.approval_envelope import EnvelopeDecision
from ..domain.demand_periods import DemandPeriodDefinition
from ..domain.planning_snapshot import PlanningSnapshot
from .query_models import WorkPackageReadModel
from .read_models import DemandPeriodReadModel, DemandReadModel, SegmentReadModel
from .work_package_load import WorkPackageLoadIntervalValue
from .work_package_weekly_load import WorkPackageWeeklyLoadState, WeeklyLoadValue


class WorkPackageRepositoryPort(Protocol):
    """Persistence contract for WorkPackage creation and editing."""

    def get(self, reference: str) -> WorkPackageReadModel | None: ...

    def create(self, values: Mapping[str, Any]) -> WorkPackageReadModel: ...

    def update(
        self,
        reference: str,
        updates: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> WorkPackageReadModel: ...

    def get_weekly_load_state(
        self,
        reference: str,
    ) -> WorkPackageWeeklyLoadState | None: ...

    def replace_weekly_loads(
        self,
        reference: str,
        loads: Sequence[WeeklyLoadValue],
        *,
        origin: str,
        expected_version: int,
    ) -> WorkPackageReadModel: ...

    def set_terminal_status(
        self,
        reference: str,
        *,
        terminal_status: str,
        expected_version: int,
    ) -> WorkPackageReadModel: ...


class DemandRepositoryPort(Protocol):
    """Persistence contract required by demand workflows.

    The contract deliberately exposes business-oriented records and generic commands;
    it does not mention Excel, SQLAlchemy or any physical schema.
    """

    def list(self) -> Sequence[DemandReadModel]: ...

    def get(self, number: str) -> DemandReadModel | None: ...

    def create(self, values: Mapping[str, Any], *, submit: bool = False) -> str: ...

    def update(
        self,
        number: str,
        updates: Mapping[str, Any],
        *,
        action: str,
        comment: str = "",
    ) -> None: ...

    def request_cancellation(
        self,
        number: str,
        *,
        cancellation_request_id: str,
        reason: str,
        expected_version: int,
    ) -> None: ...

    def reject_cancellation(
        self,
        number: str,
        *,
        cancellation_request_id: str,
        comment: str,
        expected_version: int,
    ) -> None: ...

    def accept_cancellation(
        self,
        number: str,
        *,
        cancellation_request_id: str,
        comment: str,
        expected_version: int,
        planning_version: int,
        correlation_id: str,
    ) -> Mapping[str, Any]: ...

    def extend_candidate_window(
        self,
        number: str,
        target_day: date,
        *,
        request_line_id: str | None = None,
        expected_version: int,
        action: str,
        comment: str = "",
    ) -> bool: ...


class DemandPeriodRepositoryPort(Protocol):
    """Persistence contract for requested periods and exclusive option selection."""

    def list_for_demand(
        self,
        demand_number: str,
        *,
        include_inactive: bool = False,
        request_line_id: str | None = None,
    ) -> Sequence[DemandPeriodReadModel]: ...

    def replace_for_demand(
        self,
        demand_number: str,
        periods: Sequence[DemandPeriodDefinition],
        *,
        request_line_id: str | None = None,
    ) -> Sequence[DemandPeriodReadModel]: ...

    def extend_window(
        self,
        demand_number: str,
        period_id: str,
        target_day: date,
        *,
        request_line_id: str | None = None,
    ) -> bool: ...

    def select_alternative(
        self,
        demand_number: str,
        alternative_group: str,
        period_id: str,
        *,
        request_line_id: str | None = None,
    ) -> None: ...

    def selections_for_demand(
        self,
        demand_number: str,
        *,
        request_line_id: str | None = None,
    ) -> Mapping[str, str]: ...


class DemandOperationalChoiceRepositoryPort(Protocol):
    """Versioned active choices applied to the current approved revision."""

    def state_for_demand(
        self,
        demand_number: str,
    ) -> DemandOperationalChoiceReadModel | None: ...

    def select_alternative(
        self,
        demand_number: str,
        alternative_group: str,
        period_id: str,
        *,
        request_line_id: str | None = None,
        expected_version: int | None = None,
    ) -> DemandOperationalChoiceReadModel: ...

    def set_confirmation(
        self,
        demand_number: str,
        confirmation: str,
        *,
        request_line_id: str | None = None,
        period_id: str | None = None,
        expected_version: int | None = None,
    ) -> DemandOperationalChoiceReadModel: ...

    def set_budget_override(
        self,
        demand_number: str,
        entry_key: str,
        hours: float,
        *,
        expected_version: int | None = None,
    ) -> DemandOperationalChoiceReadModel: ...


class DemandApprovalEnvelopePolicyPort(Protocol):
    """Common candidate-versus-approved authorization policy boundary."""

    def evaluate_candidate(
        self,
        demand_number: str,
        *,
        actor_role: str | None = None,
    ) -> EnvelopeDecision: ...

    def mark_reapproval_required(
        self,
        demand_number: str,
        decision: EnvelopeDecision,
    ) -> None: ...

    def record_candidate_decision(
        self,
        demand_number: str,
        decision: EnvelopeDecision,
    ) -> None: ...

    def stamp_direct_approval(
        self,
        demand_number: str,
        decision: EnvelopeDecision,
        *,
        actor_name: str,
    ) -> None: ...

    def record_direct_approval(
        self,
        demand_number: str,
        decision: EnvelopeDecision,
        *,
        actor_name: str,
    ) -> None: ...


class PlanningAuthorizationPort(Protocol):
    """Authorization guard for direct operational planning mutations."""

    def authorize_segment_create(
        self,
        values: Mapping[str, Any],
    ) -> None: ...

    def authorize_segment_update(
        self,
        segment_id: str,
        updates: Mapping[str, Any],
    ) -> None: ...

    def authorize_planned_hours(
        self,
        segment_id: str,
        planned_hours: float,
        *,
        explicit_increase: bool = False,
        expected_operational_version: int | None = None,
    ) -> None: ...

    def operational_window_authorization(
        self,
        segment_id: str,
        target_day: date,
        *,
        expected_approval_revision_id: str | None = None,
    ) -> Mapping[str, Any]: ...


class SegmentRepositoryPort(Protocol):
    """Persistence contract required by operational segment workflows."""

    def list(self, *, include_cancelled: bool = True) -> Sequence[SegmentReadModel]: ...

    def get(self, segment_id: str) -> SegmentReadModel | None: ...

    def create(self, values: Mapping[str, Any]) -> str: ...

    def update(self, segment_id: str, updates: Mapping[str, Any]) -> None: ...


class PlanningMutationVersionPort(Protocol):
    """Persistent concurrency token for globally rebuilt planning mutations."""

    def current_version(self) -> int: ...

    def acquire(self, expected_version: int | None = None) -> int: ...


class PlanningReadRepositoryPort(Protocol):
    """Atomic read contract for one pure-planning calculation cycle.

    Implementations may read Excel, SQL Server or another store, but callers receive
    one immutable ``PlanningSnapshot`` and never coordinate physical tables/sheets.
    """

    def capture(self) -> PlanningSnapshot: ...
