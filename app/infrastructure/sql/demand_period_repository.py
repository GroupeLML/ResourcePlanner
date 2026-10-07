from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application.read_models import DemandPeriodReadModel
from ...application.repository_ports import DemandPeriodRepositoryPort
from ...domain.active_days import normalize_active_day_target
from ...domain.approval_envelope import EnvelopeEntryIdentity, EnvelopeGroupIdentity
from ...domain.confirmation import normalize_confirmation
from ...domain.demand_periods import (
    CONFIRMATION_MODE_EXPLICIT,
    CONFIRMATION_MODE_INHERIT_MASTER,
    PERIOD_INHERITANCE_CONTRACT_VERSION,
    PROPOSED_RESOURCE_MODE_EXPLICIT,
    PROPOSED_RESOURCE_MODE_INHERIT_MASTER,
    PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
    DemandPeriodDefinition,
    normalized_confirmation_mode,
    normalized_proposed_resource_mode,
    resolve_period_authority,
    validate_period_definitions,
)
from .base import utc_now
from .demand_period_models import (
    WorkforceRequestPeriod,
    WorkforceRequestPeriodSelection,
)
from .models import RequestLine, Resource, WorkforceRequest, WorkforceRequestHistory
from .operational_choice_repository import SqlRequestOperationalChoiceRepository


def _text(value: object) -> str:
    return str(value or "").strip()


class SqlDemandPeriodRepository(DemandPeriodRepositoryPort):
    """SQL storage for request periods without leaking SQLAlchemy into the core."""

    def __init__(self, session: Session, *, actor_name: str = "") -> None:
        self._session = session
        self._actor_name = _text(actor_name)

    def _request(self, number: str) -> WorkforceRequest:
        wanted = _text(number)
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        )
        if request is None:
            raise KeyError(f"Demande {wanted} introuvable")
        return request

    def _line(self, request: WorkforceRequest, line_id: str) -> RequestLine:
        wanted = _text(line_id)
        line = self._session.get(RequestLine, wanted)
        if line is None or line.workforce_request_id != request.id:
            raise KeyError(f"Ligne {wanted} introuvable pour la demande")
        return line

    def _scope_line_id(
        self,
        request: WorkforceRequest,
        request_line_id: str | None,
    ) -> str:
        if request_line_id is not None:
            return self._line(request, request_line_id).id

        # Migrated legacy data already owns the deterministic shadow line created
        # by 288B. A few supported test/import paths build ORM rows directly with
        # Base.metadata.create_all(), so reproduce the same invariant lazily when
        # that shadow line is absent instead of rejecting an otherwise valid
        # historical one-line demand.
        existing = self._session.get(RequestLine, request.id)
        if existing is not None:
            if existing.workforce_request_id != request.id:
                raise KeyError(f"Ligne {request.id} introuvable pour la demande")
            return existing.id
        if bool(request.line_mode):
            raise KeyError(f"Ligne {request.id} introuvable pour la demande")

        shadow = RequestLine(
            id=request.id,
            workforce_request_id=request.id,
            position=0,
            kind="WORKFORCE",
            slot_count=max(int(request.resource_count or 1), 1),
            required_competencies_snapshot=request.required_competencies,
            desired_start=request.desired_start,
            desired_end=request.desired_end,
            desired_active_days=request.estimated_days,
            estimated_hours=request.estimated_hours,
            estimated_hours_source=(
                "LEGACY" if request.estimated_hours is not None else None
            ),
            confirmation=request.confirmation,
            work_package_id=request.work_package_id,
            erp_task_code=request.erp_task_code,
            erp_task_label=request.erp_task_label,
            proposed_resource_id=request.proposed_resource_id,
            description=request.description,
            active=True,
        )
        self._session.add(shadow)
        self._session.flush()
        return shadow.id

    def _resource(self, name: str | None) -> Resource | None:
        wanted = _text(name)
        if not wanted:
            return None
        resource = self._session.scalar(select(Resource).where(Resource.name == wanted))
        if resource is None:
            raise KeyError(f"Ressource {wanted} introuvable")
        return resource

    def _selection_row_ids(
        self,
        request_id: str,
        *,
        request_line_id: str | None = None,
    ) -> dict[tuple[str, str], str]:
        statement = select(WorkforceRequestPeriodSelection).where(
            WorkforceRequestPeriodSelection.workforce_request_id == request_id
        )
        if request_line_id is not None:
            statement = statement.where(
                WorkforceRequestPeriodSelection.request_line_id == request_line_id
            )
        rows = self._session.scalars(statement).all()
        return {
            (row.request_line_id, row.alternative_group): row.period_id
            for row in rows
        }

    def list_for_demand(
        self,
        demand_number: str,
        *,
        include_inactive: bool = False,
        request_line_id: str | None = None,
    ) -> Sequence[DemandPeriodReadModel]:
        request = self._request(demand_number)
        scoped_line_id = (
            self._line(request, request_line_id).id
            if request_line_id is not None
            else None
        )
        statement = select(WorkforceRequestPeriod).where(
            WorkforceRequestPeriod.workforce_request_id == request.id
        )
        if scoped_line_id is not None:
            statement = statement.where(
                WorkforceRequestPeriod.request_line_id == scoped_line_id
            )
        if not include_inactive:
            statement = statement.where(WorkforceRequestPeriod.active == true())
        periods = self._session.scalars(
            statement.order_by(
                WorkforceRequestPeriod.sequence,
                WorkforceRequestPeriod.created_at,
                WorkforceRequestPeriod.id,
            )
        ).all()
        selections = self._selection_row_ids(
            request.id,
            request_line_id=scoped_line_id,
        )
        operational = (
            SqlRequestOperationalChoiceRepository(
                self._session,
                actor_name=self._actor_name,
            ).state_for_request_id(request.id)
            if request.status == "En planification"
            else None
        )

        line_ids = {row.request_line_id for row in periods if row.request_line_id}
        lines = {
            row.id: row
            for row in (
                self._session.scalars(
                    select(RequestLine).where(RequestLine.id.in_(line_ids))
                ).all()
                if line_ids
                else []
            )
        }
        resolved_rows = []
        resource_ids: set[str] = set()
        for row in periods:
            line = lines.get(row.request_line_id or "")
            resolved = resolve_period_authority(
                contract_version=row.inheritance_contract_version,
                stored_resource_count=row.resource_count,
                stored_confirmation=row.confirmation,
                stored_proposed_resource=row.proposed_resource_id,
                confirmation_mode=row.confirmation_mode,
                proposed_resource_mode=row.proposed_resource_mode,
                same_as_period_id=row.same_as_period_key,
                master_resource_count=(line.slot_count if line is not None else None),
                master_confirmation=(line.confirmation if line is not None else None),
                master_proposed_resource=(
                    line.proposed_resource_id if line is not None else None
                ),
            )
            resolved_rows.append((row, resolved))
            if resolved.proposed_resource:
                resource_ids.add(resolved.proposed_resource)
            if row.proposed_resource_id:
                resource_ids.add(row.proposed_resource_id)
        resources = (
            self._session.scalars(
                select(Resource).where(Resource.id.in_(resource_ids))
            ).all()
            if resource_ids
            else []
        )
        resource_names = {row.id: row.name for row in resources}
        business_number = _text(request.legacy_demand_number) or request.id
        result: list[DemandPeriodReadModel] = []
        for row, resolved in resolved_rows:
            identity = EnvelopeEntryIdentity(
                line_id=row.request_line_id or request.id,
                period_key=row.period_key,
            ).stable_key
            effective_confirmation = resolved.confirmation
            confirmation_provenance = resolved.confirmation_provenance
            if operational is not None and identity in operational.confirmations:
                effective_confirmation = operational.confirmations[identity]
                confirmation_provenance = "OPERATIONAL_OVERRIDE"
            result.append(
                DemandPeriodReadModel(
                    period_id=row.period_key,
                    demand_number=business_number,
                    request_line_id=row.request_line_id,
                    sequence=row.sequence,
                    kind=row.kind,
                    alternative_group=row.alternative_group,
                    start_date=row.start_date,
                    end_date=row.end_date,
                    hours=float(row.hours) if row.hours is not None else None,
                    inheritance_contract_version=row.inheritance_contract_version,
                    confirmation=effective_confirmation,
                    confirmation_mode=resolved.confirmation_mode,
                    confirmation_explicit=(
                        row.confirmation
                        if resolved.confirmation_mode == CONFIRMATION_MODE_EXPLICIT
                        else None
                    ),
                    confirmation_provenance=confirmation_provenance,
                    proposed_resource_id=resolved.proposed_resource,
                    proposed_resource=resource_names.get(resolved.proposed_resource),
                    proposed_resource_mode=resolved.proposed_resource_mode,
                    proposed_resource_explicit=(
                        resource_names.get(row.proposed_resource_id)
                        if resolved.proposed_resource_mode == PROPOSED_RESOURCE_MODE_EXPLICIT
                        else None
                    ),
                    proposed_resource_provenance=resolved.proposed_resource_provenance,
                    same_as_period_id=resolved.same_as_period_id,
                    same_as_root_period_id=resolved.same_as_root_period_id,
                    same_as_state=resolved.same_as_state,
                    resource_count=resolved.resource_count,
                    resource_count_provenance=resolved.resource_count_provenance,
                    desired_active_days=row.desired_active_days,
                    note=row.note,
                    selected=(
                        (
                            bool(row.alternative_group)
                            and bool(row.request_line_id)
                            and operational is not None
                            and operational.selections.get(
                                EnvelopeGroupIdentity(
                                    line_id=row.request_line_id,
                                    group_key=_text(row.alternative_group),
                                ).stable_key
                            )
                            == identity
                        )
                        if operational is not None
                        else (
                            bool(row.alternative_group)
                            and bool(row.request_line_id)
                            and selections.get(
                                (row.request_line_id, _text(row.alternative_group))
                            )
                            == row.id
                        )
                    ),
                )
            )
        return tuple(result)

    def extend_window(
        self,
        demand_number: str,
        period_id: str,
        target_day: date,
        *,
        request_line_id: str | None = None,
    ) -> bool:
        request = self._request(demand_number)
        scoped_line_id = self._scope_line_id(request, request_line_id)
        wanted = _text(period_id)
        period = self._session.scalar(
            select(WorkforceRequestPeriod).where(
                WorkforceRequestPeriod.period_key == wanted,
                WorkforceRequestPeriod.workforce_request_id == request.id,
                WorkforceRequestPeriod.request_line_id == scoped_line_id,
                WorkforceRequestPeriod.active == true(),
            )
        )
        if period is None:
            raise KeyError(f"Période {wanted} introuvable pour la ligne")
        proposed_start = min(period.start_date, target_day)
        proposed_end = max(period.end_date, target_day)
        if proposed_start == period.start_date and proposed_end == period.end_date:
            return False
        period.start_date = proposed_start
        period.end_date = proposed_end
        self._session.flush()
        return True

    def replace_for_demand(
        self,
        demand_number: str,
        periods: Sequence[DemandPeriodDefinition],
        *,
        request_line_id: str | None = None,
    ) -> Sequence[DemandPeriodReadModel]:
        request = self._request(demand_number)
        scoped_line_id = self._scope_line_id(request, request_line_id)
        line = self._line(request, scoped_line_id)
        validate_period_definitions(
            periods,
            allow_unbudgeted=_text(line.kind).upper() == "ASSET",
        )
        has_same_as = any(
            normalized_proposed_resource_mode(period.proposed_resource_mode)
            == PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD
            for period in periods
        )
        if has_same_as and _text(line.kind).upper() != "WORKFORCE":
            raise ValueError("SAME_AS_PERIOD est limité aux périodes humaines.")
        if has_same_as and max(int(line.slot_count or 1), 1) != 1:
            raise ValueError(
                "SAME_AS_PERIOD exige une quantité effective de 1 sur la ligne."
            )
        if _text(line.kind).upper() == "ASSET" and any(
            period.proposed_resource for period in periods
        ):
            raise ValueError("Une période matérielle ne peut pas proposer un technicien.")
        current = self._session.scalars(
            select(WorkforceRequestPeriod).where(
                WorkforceRequestPeriod.workforce_request_id == request.id,
                WorkforceRequestPeriod.request_line_id == scoped_line_id,
                WorkforceRequestPeriod.active == true(),
            )
        ).all()
        for row in current:
            row.active = False

        old_selections = self._session.scalars(
            select(WorkforceRequestPeriodSelection).where(
                WorkforceRequestPeriodSelection.workforce_request_id == request.id,
                WorkforceRequestPeriodSelection.request_line_id == scoped_line_id,
            )
        ).all()
        for selection in old_selections:
            self._session.delete(selection)
        self._session.flush()

        for sequence, period in enumerate(periods, start=1):
            confirmation_mode = normalized_confirmation_mode(period.confirmation_mode)
            resource_mode = normalized_proposed_resource_mode(period.proposed_resource_mode)
            explicit_resource = (
                self._resource(period.proposed_resource)
                if resource_mode == PROPOSED_RESOURCE_MODE_EXPLICIT
                else None
            )
            effective_confirmation = (
                line.confirmation
                if confirmation_mode == CONFIRMATION_MODE_INHERIT_MASTER
                else period.confirmation
            )
            effective_resource_id = (
                line.proposed_resource_id
                if resource_mode == PROPOSED_RESOURCE_MODE_INHERIT_MASTER
                else explicit_resource.id if explicit_resource is not None else None
            )
            self._session.add(
                WorkforceRequestPeriod(
                    period_key=_text(period.period_id),
                    workforce_request_id=request.id,
                    request_line_id=scoped_line_id,
                    sequence=sequence,
                    kind=_text(period.kind).upper(),
                    alternative_group=_text(period.alternative_group) or None,
                    start_date=period.start_date,
                    end_date=period.end_date,
                    hours=Decimal(str(period.hours)) if period.hours is not None else None,
                    inheritance_contract_version=PERIOD_INHERITANCE_CONTRACT_VERSION,
                    confirmation_mode=confirmation_mode,
                    confirmation=normalize_confirmation(effective_confirmation),
                    proposed_resource_mode=resource_mode,
                    proposed_resource_id=effective_resource_id,
                    same_as_period_key=(
                        _text(period.same_as_period_id) or None
                        if resource_mode == PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD
                        else None
                    ),
                    resource_count=max(int(line.slot_count or 1), 1),
                    desired_active_days=normalize_active_day_target(
                        period.desired_active_days,
                        start=period.start_date,
                        end=period.end_date,
                        field=f"Les jours actifs de la période {period.period_id}",
                    ),
                    note=_text(period.note) or None,
                    active=True,
                )
            )
        self._session.flush()
        return self.list_for_demand(
            demand_number,
            request_line_id=scoped_line_id,
        )

    def select_alternative(
        self,
        demand_number: str,
        alternative_group: str,
        period_id: str,
        *,
        request_line_id: str | None = None,
    ) -> None:
        request = self._request(demand_number)
        scoped_line_id = self._scope_line_id(request, request_line_id)
        group = _text(alternative_group)
        wanted = _text(period_id)
        period = self._session.scalar(
            select(WorkforceRequestPeriod).where(
                WorkforceRequestPeriod.period_key == wanted,
                WorkforceRequestPeriod.workforce_request_id == request.id,
                WorkforceRequestPeriod.request_line_id == scoped_line_id,
                WorkforceRequestPeriod.active == true(),
            )
        )
        if period is None:
            raise KeyError(f"Période {wanted} introuvable pour la ligne")
        if period.kind != "ALTERNATIVE" or _text(period.alternative_group) != group:
            raise ValueError(
                f"La période {wanted} n'appartient pas au groupe alternatif {group}."
            )

        selection = self._session.scalar(
            select(WorkforceRequestPeriodSelection).where(
                WorkforceRequestPeriodSelection.request_line_id == scoped_line_id,
                WorkforceRequestPeriodSelection.alternative_group == group,
            )
        )
        previous_period = (
            self._session.get(WorkforceRequestPeriod, selection.period_id)
            if selection is not None
            else None
        )
        if selection is not None and selection.period_id == period.id:
            return

        selected_at = utc_now()
        if selection is None:
            selection = WorkforceRequestPeriodSelection(
                workforce_request_id=request.id,
                request_line_id=scoped_line_id,
                alternative_group=group,
                period_id=period.id,
                selected_at=selected_at,
                selected_by_name=self._actor_name or None,
            )
            self._session.add(selection)
        else:
            selection.period_id = period.id
            selection.selected_at = selected_at
            selection.selected_by_name = self._actor_name or None

        previous_key = _text(previous_period.period_key) if previous_period is not None else ""
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action="Sélection alternative",
                status=request.status,
                comment=(
                    f"Ligne {scoped_line_id} · groupe {group}: "
                    f"{previous_key or 'aucune'} -> {period.period_key}"
                ),
                actor_name=self._actor_name or None,
                occurred_at=selected_at,
            )
        )
        self._session.flush()

    def selections_for_demand(
        self,
        demand_number: str,
        *,
        request_line_id: str | None = None,
    ) -> Mapping[str, str]:
        request = self._request(demand_number)
        scoped_line_id = (
            self._scope_line_id(request, request_line_id)
            if request_line_id is not None
            else None
        )
        statement = (
            select(WorkforceRequestPeriodSelection, WorkforceRequestPeriod)
            .join(
                WorkforceRequestPeriod,
                WorkforceRequestPeriodSelection.period_id == WorkforceRequestPeriod.id,
            )
            .where(
                WorkforceRequestPeriodSelection.workforce_request_id == request.id
            )
        )
        if scoped_line_id is not None:
            statement = statement.where(
                WorkforceRequestPeriodSelection.request_line_id == scoped_line_id
            )
        rows = self._session.execute(statement).all()
        return {
            selection.alternative_group: period.period_key
            for selection, period in rows
        }
