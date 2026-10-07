from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from ...application.operational_contacts import OperationalContactService
from ...domain.operational_contacts import (
    PROVENANCE_APPROVAL_CAPTURE,
    RESPONSIBILITY_CONTEXT_VERSION,
)
from ...domain.demand_periods import resolve_period_authority
from ...domain.approval_envelope import (
    EnvelopeEntryIdentity,
    EnvelopeLineDefinition,
    EnvelopePeriodDefinition,
    normalize_approval_envelope,
)
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    APPROVAL_REFERENCE_LEGACY_UNKNOWN,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from .base import utc_now
from .demand_period_models import (
    WorkforceRequestPeriod,
    WorkforceRequestPeriodRequirement,
    WorkforceRequestPeriodSelection,
)
from .operational_choice_repository import SqlRequestOperationalChoiceRepository
from .operational_contact_repository import SqlOperationalContactRepository
from .models import (
    ORIGIN_REQUEST,
    Project,
    RequestLine,
    RequestLineCompetency,
    ResourceRequirement,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestCompetency,
    WorkPackage,
)


APPROVAL_SNAPSHOT_FORMAT_VERSION = 3
APPROVAL_PROVENANCE_STANDARD = "APPROVAL"


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    value_text = _text(value)
    return value_text or None


class SqlRequestApprovalRevisionRepository:
    """Capture immutable approved authorization independently from active planning."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _active_lines(self, request: WorkforceRequest) -> list[RequestLine]:
        return list(
            self._session.scalars(
                select(RequestLine)
                .where(
                    RequestLine.workforce_request_id == request.id,
                    RequestLine.active == true(),
                )
                .order_by(RequestLine.position, RequestLine.id)
            ).all()
        )

    def _line_competencies(
        self,
        line_ids: set[str],
    ) -> dict[str, tuple[str, ...]]:
        if not line_ids:
            return {}
        rows = self._session.execute(
            select(
                RequestLineCompetency.request_line_id,
                RequestLineCompetency.competency_id,
            )
            .where(RequestLineCompetency.request_line_id.in_(line_ids))
            .order_by(
                RequestLineCompetency.request_line_id,
                RequestLineCompetency.competency_id,
            )
        ).all()
        grouped: dict[str, list[str]] = defaultdict(list)
        for line_id, competency_id in rows:
            grouped[line_id].append(competency_id)
        return {
            line_id: tuple(values)
            for line_id, values in grouped.items()
        }

    def _request_competencies(self, request_id: str) -> tuple[str, ...]:
        return tuple(
            self._session.scalars(
                select(WorkforceRequestCompetency.competency_id)
                .where(
                    WorkforceRequestCompetency.workforce_request_id == request_id
                )
                .order_by(WorkforceRequestCompetency.competency_id)
            ).all()
        )

    def _active_periods(
        self,
        request_id: str,
    ) -> list[WorkforceRequestPeriod]:
        return list(
            self._session.scalars(
                select(WorkforceRequestPeriod)
                .where(
                    WorkforceRequestPeriod.workforce_request_id == request_id,
                    WorkforceRequestPeriod.active == true(),
                )
                .order_by(
                    WorkforceRequestPeriod.request_line_id,
                    WorkforceRequestPeriod.sequence,
                    WorkforceRequestPeriod.created_at,
                    WorkforceRequestPeriod.id,
                )
            ).all()
        )

    def _selections(
        self,
        request_id: str,
    ) -> dict[tuple[str, str], str]:
        rows = self._session.scalars(
            select(WorkforceRequestPeriodSelection).where(
                WorkforceRequestPeriodSelection.workforce_request_id == request_id
            )
        ).all()
        return {
            (row.request_line_id, row.alternative_group): row.period_id
            for row in rows
        }

    def _legacy_task_ref(self, request: WorkforceRequest) -> str | None:
        if request.work_package_id:
            work_package = self._session.get(WorkPackage, request.work_package_id)
            if work_package is not None and work_package.task_catalog_item_id:
                return work_package.task_catalog_item_id
        code = _optional_text(request.erp_task_code)
        if not code:
            return None
        project = self._session.get(Project, request.project_id)
        if project is None:
            return code
        identifier = self._session.scalar(
            select(TaskCatalogEntry.id).where(
                TaskCatalogEntry.project_number == project.number,
                TaskCatalogEntry.task_code == code,
            )
        )
        return identifier or code

    def _line_task_ref(self, line: RequestLine) -> str | None:
        package_task_id = None
        if line.work_package_id:
            work_package = self._session.get(WorkPackage, line.work_package_id)
            if work_package is not None:
                package_task_id = _optional_text(work_package.task_catalog_item_id)
        line_task_id = _optional_text(line.task_catalog_item_id)
        if package_task_id and line_task_id and package_task_id != line_task_id:
            raise ValueError(
                "La ligne contredit la tâche ERP classée du WorkPackage."
            )
        return (
            package_task_id
            or line_task_id
            or _optional_text(line.erp_task_code)
        )

    def _envelope_lines(
        self,
        request: WorkforceRequest,
    ) -> tuple[EnvelopeLineDefinition, ...]:
        lines = self._active_lines(request)
        periods = self._active_periods(request.id)
        selections = self._selections(request.id)
        periods_by_line: dict[str, list[WorkforceRequestPeriod]] = defaultdict(list)
        for period in periods:
            line_id = _text(period.request_line_id) or request.id
            periods_by_line[line_id].append(period)

        if not lines:
            return (
                EnvelopeLineDefinition(
                    line_id=request.id,
                    project_id=request.project_id,
                    site_id=_optional_text(request.site_client),
                    location=_optional_text(request.location),
                    slot_count=max(int(request.resource_count or 1), 1),
                    line_kind="WORKFORCE",
                    competency_ids=self._request_competencies(request.id),
                    task_ref=self._legacy_task_ref(request),
                    work_package_ref=_optional_text(request.work_package_id),
                    start_date=request.desired_start,
                    end_date=request.desired_end,
                    hours=request.estimated_hours,
                    confirmation=_text(request.confirmation) or "Confirmée",
                    proposed_resource_id=_optional_text(request.proposed_resource_id),
                    periods=tuple(
                        self._period_definition(
                            request.id,
                            period,
                            selections=selections,
                            master_resource_count=request.resource_count,
                            master_confirmation=request.confirmation,
                            master_proposed_resource_id=request.proposed_resource_id,
                        )
                        for period in periods_by_line.get(request.id, [])
                    ),
                ),
            )

        line_competencies = self._line_competencies({line.id for line in lines})
        normalized: list[EnvelopeLineDefinition] = []
        for line in lines:
            normalized.append(
                EnvelopeLineDefinition(
                    line_id=line.id,
                    project_id=request.project_id,
                    site_id=_optional_text(request.site_client),
                    location=_optional_text(request.location),
                    slot_count=max(int(line.slot_count or 1), 1),
                    line_kind=_text(line.kind) or "WORKFORCE",
                    required_resource_class=_optional_text(
                        line.required_resource_class
                    ),
                    competency_ids=line_competencies.get(line.id, ()),
                    task_ref=self._line_task_ref(line),
                    work_package_ref=_optional_text(line.work_package_id),
                    start_date=line.desired_start,
                    end_date=line.desired_end,
                    hours=line.estimated_hours,
                    confirmation=_text(line.confirmation) or "Confirmée",
                    proposed_resource_id=_optional_text(
                        line.proposed_resource_id
                    ),
                    asset_type_id=_optional_text(line.asset_type_id),
                    occupancy_policy=("EXCLUSIVE_DAY" if line.kind == "ASSET" else None),
                    proposed_asset_id=_optional_text(line.proposed_asset_id),
                    desired_active_days=(
                        int(line.desired_active_days)
                        if line.desired_active_days is not None
                        else None
                    ),
                    periods=tuple(
                        self._period_definition(
                            line.id,
                            period,
                            selections=selections,
                            master_resource_count=line.slot_count,
                            master_confirmation=line.confirmation,
                            master_proposed_resource_id=line.proposed_resource_id,
                        )
                        for period in periods_by_line.get(line.id, [])
                    ),
                )
            )
        return tuple(normalized)

    @staticmethod
    def _period_definition(
        line_id: str,
        period: WorkforceRequestPeriod,
        *,
        selections: dict[tuple[str, str], str],
        master_resource_count: int,
        master_confirmation: str,
        master_proposed_resource_id: str | None,
    ) -> EnvelopePeriodDefinition:
        group = _optional_text(period.alternative_group)
        selected = bool(
            group
            and selections.get((line_id, group)) == period.id
        )
        resolved = resolve_period_authority(
            contract_version=period.inheritance_contract_version,
            stored_resource_count=period.resource_count,
            stored_confirmation=period.confirmation,
            stored_proposed_resource=period.proposed_resource_id,
            confirmation_mode=period.confirmation_mode,
            proposed_resource_mode=period.proposed_resource_mode,
            same_as_period_id=period.same_as_period_key,
            master_resource_count=master_resource_count,
            master_confirmation=master_confirmation,
            master_proposed_resource=master_proposed_resource_id,
        )
        return EnvelopePeriodDefinition(
            period_key=period.period_key,
            source_period_id=period.id,
            start_date=period.start_date,
            end_date=period.end_date,
            hours=period.hours,
            resource_count=resolved.resource_count,
            kind=period.kind,
            group_key=group,
            confirmation=resolved.confirmation,
            confirmation_mode=resolved.confirmation_mode,
            selected=selected,
            proposed_resource_id=resolved.proposed_resource,
            proposed_resource_mode=resolved.proposed_resource_mode,
            same_as_period_key=resolved.same_as_period_id,
            desired_active_days=period.desired_active_days,
        )

    def candidate_envelope(
        self,
        request: WorkforceRequest,
    ):
        """Return the canonical current candidate envelope for policy evaluation."""

        return normalize_approval_envelope(self._envelope_lines(request))

    def _operational_responsibility_context_payload(
        self,
        request: WorkforceRequest,
        authorization: dict[str, object],
    ) -> dict[str, object]:
        entries = authorization.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("Le snapshot d'autorisation ne contient pas d'entrées valides.")

        line_ids: list[str] = []
        entry_line_ids: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identity = _text(entry.get("identity"))
            if not identity:
                continue
            try:
                identity_parts = json.loads(identity)
            except (TypeError, ValueError):
                continue
            if not isinstance(identity_parts, list) or len(identity_parts) != 3:
                continue
            line_id = _text(identity_parts[1])
            if not line_id:
                continue
            entry_line_ids[identity] = line_id
            if line_id not in line_ids:
                line_ids.append(line_id)

        service = OperationalContactService(
            SqlOperationalContactRepository(self._session)
        )
        line_resolutions = service.resolve_request_lines(line_ids)
        by_line = {row.line_id: row for row in line_resolutions}
        if request.id in line_ids and request.id not in by_line:
            legacy = service.resolve_workforce_request(request.id)
            by_line[request.id] = legacy

        contexts: dict[str, object] = {}
        for identity, line_id in entry_line_ids.items():
            row = by_line.get(line_id)
            if row is None:
                raise ValueError(
                    "Le contexte de responsabilité opérationnelle d'une entrée "
                    f"approuvée est introuvable: {identity}"
                )
            responsible = row.operational_responsible
            diagnostics = tuple(
                dict.fromkeys(
                    (
                        *row.diagnostics,
                        *responsible.diagnostics,
                    )
                )
            )
            contexts[identity] = {
                "approved_task_catalog_item_id": row.task_id,
                "approved_request_override_contact_id": (
                    request.operational_responsible_override_contact_id
                ),
                "contact_id": responsible.contact_id,
                "source_type": responsible.source_type,
                "source_entity_id": responsible.source_entity_id,
                "status": responsible.status,
                "diagnostics": list(diagnostics),
            }

        return {
            "mechanism_version": RESPONSIBILITY_CONTEXT_VERSION,
            "provenance": PROVENANCE_APPROVAL_CAPTURE,
            "entries": contexts,
        }

    def create_revision(
        self,
        request: WorkforceRequest,
        *,
        provenance: str = APPROVAL_PROVENANCE_STANDARD,
        request_version: int | None = None,
    ) -> RequestApprovalRevision:
        """Create a revision without activating it.

        The caller must materialize/validate the plan, bind requirements and only then
        switch the active reference in the same database transaction.
        """

        envelope = self.candidate_envelope(request)
        approved_at = request.approved_at or utc_now()
        approved_request_version = max(
            int(
                request.aggregate_version
                if request_version is None
                else request_version
            ),
            1,
        )
        reference = self._session.get(RequestApprovalReference, request.id)
        previous_revision_id = (
            reference.active_revision_id
            if reference is not None
            and reference.status == APPROVAL_REFERENCE_CAPTURED
            else None
        )
        authorization = envelope.to_snapshot_payload()
        payload = {
            "format_version": authorization["format_version"],
            "request": {
                "request_id": request.id,
                "request_version": approved_request_version,
                "project_id": request.project_id,
                "priority": _optional_text(request.priority),
                "site_client": _optional_text(request.site_client),
                "location": _optional_text(request.location),
                "line_mode": bool(request.line_mode),
            },
            "authorization": authorization,
            "operational_responsibility_context": (
                self._operational_responsibility_context_payload(
                    request,
                    authorization,
                )
            ),
        }
        revision = RequestApprovalRevision(
            workforce_request_id=request.id,
            previous_revision_id=previous_revision_id,
            request_version=approved_request_version,
            approved_by_external_id=_optional_text(
                request.approved_by_external_id
            ),
            approved_by_name=_optional_text(request.approved_by_name),
            approved_at=approved_at,
            provenance=_text(provenance) or APPROVAL_PROVENANCE_STANDARD,
            payload_format_version=int(authorization["format_version"]),
            payload_text=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            authorization_fingerprint=envelope.authorization_fingerprint,
        )
        self._session.add(revision)
        self._session.flush()
        return revision

    def _requirement_entry_keys(
        self,
        request_id: str,
        requirements: list[ResourceRequirement],
    ) -> dict[str, str]:
        requirement_ids = {row.id for row in requirements}
        links = (
            self._session.execute(
                select(
                    WorkforceRequestPeriodRequirement.resource_requirement_id,
                    WorkforceRequestPeriodRequirement.period_id,
                ).where(
                    WorkforceRequestPeriodRequirement.resource_requirement_id.in_(
                        requirement_ids
                    )
                )
            ).all()
            if requirement_ids
            else []
        )
        period_ids = {period_id for _, period_id in links}
        periods = (
            self._session.scalars(
                select(WorkforceRequestPeriod).where(
                    WorkforceRequestPeriod.id.in_(period_ids)
                )
            ).all()
            if period_ids
            else []
        )
        period_by_id = {row.id: row for row in periods}
        period_id_by_requirement = {
            requirement_id: period_id
            for requirement_id, period_id in links
        }

        keys: dict[str, str] = {}
        for requirement in requirements:
            line_id = _text(requirement.source_request_line_id) or request_id
            period = period_by_id.get(
                period_id_by_requirement.get(requirement.id, "")
            )
            keys[requirement.id] = EnvelopeEntryIdentity(
                line_id=line_id,
                period_key=period.period_key if period is not None else None,
            ).stable_key
        return keys

    def bind_materialized_requirements(
        self,
        request: WorkforceRequest,
        revision: RequestApprovalRevision,
    ) -> None:
        payload = json.loads(revision.payload_text)
        approved_entries = {
            str(entry["identity"])
            for entry in payload["authorization"]["entries"]
        }
        requirements = list(
            self._session.scalars(
                select(ResourceRequirement)
                .where(
                    ResourceRequirement.workforce_request_id == request.id,
                    ResourceRequirement.origin == ORIGIN_REQUEST,
                    ResourceRequirement.status != "Annulé",
                )
                .order_by(ResourceRequirement.id)
            ).all()
        )
        entry_keys = self._requirement_entry_keys(request.id, requirements)
        for requirement in requirements:
            entry_key = entry_keys[requirement.id]
            if entry_key not in approved_entries:
                raise ValueError(
                    "Le besoin matérialisé ne correspond à aucune entrée de "
                    f"l'autorisation approuvée: {requirement.id} -> {entry_key}"
                )
            requirement.approval_revision_id = revision.id
            requirement.approved_entry_key = entry_key
            requirement.approval_reference_status = APPROVAL_REFERENCE_CAPTURED
        self._session.flush()

    def activate_revision(
        self,
        request: WorkforceRequest,
        revision: RequestApprovalRevision,
    ) -> None:
        reference = self._session.get(RequestApprovalReference, request.id)
        previous_revision_id = (
            reference.active_revision_id
            if reference is not None
            and reference.status == APPROVAL_REFERENCE_CAPTURED
            else None
        )
        if reference is None:
            reference = RequestApprovalReference(
                workforce_request_id=request.id,
                active_revision_id=revision.id,
                status=APPROVAL_REFERENCE_CAPTURED,
            )
            self._session.add(reference)
        else:
            reference.active_revision_id = revision.id
            reference.status = APPROVAL_REFERENCE_CAPTURED
        self._session.flush()
        if previous_revision_id and previous_revision_id != revision.id:
            from .planning_window_override_repository import (
                SqlPlanningWindowOverrideRepository,
            )

            SqlPlanningWindowOverrideRepository(
                self._session
            ).reconcile_for_new_revision(
                request.id,
                previous_revision_id=previous_revision_id,
                new_revision=revision,
            )
        SqlRequestOperationalChoiceRepository(
            self._session,
            actor_name=_text(request.approved_by_name),
        ).initialize_for_revision(
            request.id,
            revision,
            actor_name=request.approved_by_name,
        )

    def active_reference_status(
        self,
        request_id: str,
    ) -> str:
        reference = self._session.get(RequestApprovalReference, request_id)
        if reference is None:
            return APPROVAL_REFERENCE_LEGACY_UNKNOWN
        return reference.status
